"""
The on-screen AI assistant (the little robot on every page).

Three layers, so it is useful even with no AI key:
  1. Page guide: tips and suggested questions for the page the user is on (static, below).
  2. Health check: live findings from the app's own data (receiver heartbeat, vehicles with no signal,
     rejected IMEIs, CRC / parse errors, failed self-tests, missing keys) with what to do about each.
  3. AI answers: when an AI provider and key are set (Settings → AI assistant), questions go to Claude
     (default) or OpenAI together with a short snapshot of the fleet and the findings above. Without a
     key the robot answers from a built-in troubleshooting guide instead.

Behaviour (on/off, name, position, provider, greeting, extra instructions, who may use it) is stored in
the IntegrationSetting row "assistant_config" as JSON. API keys stay in tracking/integrations.py.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from . import integrations
from .models import Connection, Device, IntegrationSetting, RawPacket, TestRun

log = logging.getLogger("fleet.assistant")

CONFIG_KEY = "assistant_config"
CONFIG_CACHE = "assistant:config:v1"
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
POSITIONS = {"bottom-right": "Bottom right", "bottom-left": "Bottom left",
             "top-right": "Top right (under the header)", "top-left": "Top left (next to the menu)"}
PROVIDERS = {"claude": "Claude (Anthropic) - recommended", "openai": "OpenAI",
             "off": "No AI - page guide and health check only"}
AUDIENCES = {"all": "Everyone who can sign in", "staff": "Staff and admins only"}
DETAIL = {"short": "Short and direct", "detailed": "Detailed, step by step"}
DEFAULTS = {
    "enabled": True,
    "name": "Melo",
    "position": "bottom-right",
    "provider": "claude",
    "audience": "all",
    "detail": "short",
    "greeting": "Hi {user}! I'm {name}, your tracking assistant. Ask me anything about this page, "
                "or tap “Check for problems” and I'll look over the receiver and your vehicles.",
    "instructions": "",
    "proactive": True,
}
LIMITS = {"name": 30, "greeting": 400, "instructions": 2000}
CHAT_LIMIT = 30           # questions per user ...
CHAT_WINDOW = 10 * 60     # ... per 10 minutes
# Claude models that accept the server-side refusal fallback ("default" routing).
_FALLBACK_MODELS = ("claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
# Models that accept output_config.effort.
_EFFORT_PREFIXES = ("claude-fable", "claude-mythos", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
                    "claude-opus-4-6", "claude-opus-4-5", "claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-5")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def get_config() -> dict:
    cfg = cache.get(CONFIG_CACHE)
    if cfg is not None:
        return cfg
    cfg = dict(DEFAULTS)
    row = IntegrationSetting.objects.filter(key=CONFIG_KEY).first()
    if row and row.value:
        try:
            saved = json.loads(row.value)
            cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
        except ValueError:
            log.warning("assistant_config is not valid JSON; using defaults")
    cache.set(CONFIG_CACHE, cfg, 60)
    return cfg


def clean_config(data) -> tuple[dict, list[str]]:
    """Validate a POSTed settings form. Returns (config, errors)."""
    cfg, errors = dict(DEFAULTS), []
    cfg["enabled"] = bool(data.get("enabled"))
    cfg["proactive"] = bool(data.get("proactive"))
    for key, choices in (("position", POSITIONS), ("provider", PROVIDERS), ("audience", AUDIENCES), ("detail", DETAIL)):
        v = (data.get(key) or "").strip()
        if v in choices:
            cfg[key] = v
        else:
            errors.append(f"Unknown {key}: {v!r}.")
    for key, limit in LIMITS.items():
        v = (data.get(key) or "").strip()
        if len(v) > limit:
            errors.append(f"{key.capitalize()} is longer than {limit} characters.")
            v = v[:limit]
        cfg[key] = v
    if not cfg["name"]:
        cfg["name"] = DEFAULTS["name"]
    if not cfg["greeting"]:
        cfg["greeting"] = DEFAULTS["greeting"]
    return cfg, errors


def save_config(cfg: dict, user: str = "") -> None:
    IntegrationSetting.objects.update_or_create(key=CONFIG_KEY, defaults={"value": json.dumps(cfg), "updated_by": user})
    cache.delete(CONFIG_CACHE)


def can_use(user, cfg=None) -> bool:
    cfg = cfg or get_config()
    if not (cfg["enabled"] and user and user.is_authenticated):
        return False
    return cfg["audience"] == "all" or user.is_staff or user.is_superuser


def ai_status(cfg=None) -> dict:
    """Which AI backend answers questions, and whether it is ready."""
    cfg = cfg or get_config()
    p = cfg["provider"]
    if p == "claude":
        key, model = integrations.get("anthropic_api_key"), integrations.get("anthropic_model") or DEFAULT_CLAUDE_MODEL
        return {"provider": p, "ready": bool(key), "model": model,
                "label": f"Claude · {model}" if key else "Claude key not set - using the built-in guide"}
    if p == "openai":
        key, model = integrations.get("openai_api_key"), integrations.get("openai_model")
        ready = bool(key and model)
        why = "" if ready else (" key" if not key else " model")
        return {"provider": p, "ready": ready, "model": model,
                "label": f"OpenAI · {model}" if ready else f"OpenAI{why} not set - using the built-in guide"}
    return {"provider": "off", "ready": False, "model": "", "label": "Built-in guide (AI is off)"}


# ---------------------------------------------------------------------------
# Page guide
# ---------------------------------------------------------------------------
PAGES = {
    "dashboard": {
        "title": "Dashboard",
        "about": "The fleet at a glance: how many vehicles are moving, idling, parked or have no signal, "
                 "plus traffic in the last 24 hours and the receiver status.",
        "tips": ["Click a vehicle row to open its device page.",
                 "“No signal” means no data for over an hour (or never). The health check tells you why.",
                 "Rejected connections usually mean a tracker whose IMEI is not registered yet."],
        "ask": ["Why do my vehicles show No signal?", "What do the colours mean?", "Is the receiver working?"],
    },
    "live": {
        "title": "Live map",
        "about": "Every vehicle on the map, refreshed every few seconds. The list on the left filters by state; "
                 "clicking a vehicle opens its gauges panel on the right.",
        "tips": ["Use the layers button (top right of the map) to switch between Google, OpenStreetMap, CARTO and Esri maps.",
                 "If Google maps disappear, the Google key was rejected. Settings → Integrations → Test key says why.",
                 "A grey wifi-off icon is a vehicle with no signal; the panel shows the last values it sent."],
        "ask": ["Why did the Google map disappear?", "Why is this vehicle showing old data?", "How do I change the map?"],
    },
    "history": {
        "title": "History",
        "about": "Replay where a vehicle went: pick a vehicle and a time window to draw its route.",
        "tips": ["Only records with a valid GPS fix are drawn on the route.",
                 "Records that arrive late (from the tracker's memory) are marked as late.",
                 "Long windows can hold thousands of points; shorten the window if the page is slow."],
        "ask": ["Why is the route empty?", "What is a late record?"],
    },
    "places": {
        "title": "Saved places",
        "about": "Spots saved from the Car view (pin button or when you stop a drive), with export to Google Maps.",
        "tips": ["Export KML or CSV and import it into Google My Maps or Google Earth.",
                 "Navigate opens Google Maps directions to the place.",
                 "Import accepts files exported from here, to move places between systems."],
        "ask": ["How do I put these places on Google Maps?", "How do I save a place?"],
    },
    "drivers": {
        "title": "Driver score",
        "about": "A score from 0 to 100 for each vehicle and Car view driver, from harsh acceleration, braking and "
                 "cornering, over speeding, idling and crashes, with the trips behind it.",
        "tips": ["Pick a date range, then open a row for the full score sheet with the breakdown per parameter.",
                 "Excel, CSV and Print / PDF buttons export the sheet you are looking at.",
                 "Harsh events need Green Driving turned on in the FMC130 configurator; otherwise they are estimated from GPS.",
                 "Staff can change the weights, speed limit and rating bands under Scoring weights or on Settings."],
        "ask": ["How is the driver score calculated?", "Why are there no harsh events?"],
    },
    "records": {
        "title": "Records",
        "about": "Every decoded AVL record (position + I/O values) the trackers have sent, newest first.",
        "tips": ["Filter by vehicle, valid / invalid GPS fix, or late records.",
                 "Open a record to see every I/O parameter, decoded with the AVL dictionary."],
        "ask": ["What does an invalid fix mean?", "Where do I see ignition and battery values?"],
    },
    "raw": {
        "title": "Raw data",
        "about": "The exact bytes each tracker sent, with CRC, codec, record count and the ACK we answered.",
        "tips": ["Filter by status = error to find bad frames.",
                 "Open a packet to see it split into preamble, length, codec, records and CRC.",
                 "Export CSV gives up to 5,000 packets for offline analysis."],
        "ask": ["What does a CRC error mean?", "Why was a packet not acknowledged?"],
    },
    "devices": {
        "title": "Devices",
        "about": "The allow-list of trackers. The receiver only accepts IMEIs registered and enabled here.",
        "tips": ["Register a tracker with its 15-digit IMEI (printed on the label, or *getimei* by SMS).",
                 "Disabling a device makes the receiver refuse it.",
                 "Each vehicle has its own data retention in days."],
        "ask": ["How do I add a new tracker?", "Why is my tracker rejected?"],
    },
    "terminal": {
        "title": "Port terminal",
        "about": "A live view of everything that happens on the tracker port: connections, bytes in and out, "
                 "decoded frames and ACKs.",
        "tips": ["If nothing appears when the tracker should be sending, the traffic is not reaching the server: "
                 "check the device's server address and port, the APN, and the VPS firewall.",
                 "A connection that opens and closes at once with “rejected” is an unregistered IMEI."],
        "ask": ["Nothing shows up in the terminal, why?", "How do I read the terminal?"],
    },
    "connectivity": {
        "title": "Connectivity",
        "about": "Receiver status, recent connections, rejected handshakes and the built-in self-test.",
        "tips": ["Run the self-test to check the whole path: TCP connect, IMEI handshake, Codec 8E frame and ACK.",
                 "Rejected / invalid handshakes list IMEIs that tried to connect but are not registered."],
        "ask": ["The self-test failed, what now?", "How do I test port 5027 from outside?"],
    },
    "settings": {
        "title": "Settings",
        "about": "Environment values, integration keys (Google Maps, Claude, OpenAI), this assistant, "
                 "and the FMC130 bench configuration.",
        "tips": ["Keys are stored encrypted; a blank field keeps the saved value.",
                 "Use the Test buttons before saving a new key.",
                 "The AI assistant card sets my name, position, AI provider and behaviour."],
        "ask": ["How do I set up the Google Maps key?", "How do I connect Claude?"],
    },
    "account": {
        "title": "Account",
        "about": "Change your password.",
        "tips": ["Use at least 10 characters; avoid anything similar to your username."],
        "ask": ["Who can create users?"],
    },
}


def page_info(nav: str) -> dict:
    return PAGES.get(nav or "", {"title": "This page", "about": "", "tips": [], "ask": [
        "Why do my vehicles show No signal?", "Is the receiver working?", "How do I add a new tracker?"]})


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
def _hours(seconds) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def diagnostics(user=None, client: dict | None = None) -> list[dict]:
    """Findings, worst first. level: error | warn | info | ok. Never contains a full IMEI or secret."""
    from .views import device_status, receiver_status  # local import: views imports this module

    out = []
    now = timezone.now()
    cfg = settings.FLEET
    endpoint = f"{cfg['PUBLIC_GPS_HOST']}:{cfg['PUBLIC_GPS_PORT']}"
    add = lambda level, title, detail, fix="", link="": out.append(  # noqa: E731
        {"level": level, "title": title, "detail": detail, "fix": fix, "link": link})

    rx = receiver_status()
    if not rx["up"]:
        add("error", "The TCP receiver is offline",
            f"No heartbeat from the receiver ({rx['detail']}). Trackers cannot deliver data until it runs.",
            "On the VPS run `systemctl status fleet-receiver` (or your receiver service) and restart it. "
            "./deploy.sh restarts it too.", "/connectivity/")

    devices = list(Device.objects.all())
    if not devices:
        add("warn", "No trackers registered",
            "The receiver only accepts registered IMEIs, so nothing will appear yet.",
            "Open Devices and register each tracker's 15-digit IMEI.", "/devices/")
    st = [device_status(d, now) | {"_last": d.last_packet_utc} for d in devices]
    never = [s for s in st if s["link"] == "never"]
    offline = [s for s in st if s["link"] == "offline"]
    stale = [s for s in st if s["link"] == "stale"]
    disabled = [s for s in st if s["link"] == "disabled"]
    if offline:
        names = ", ".join(f"{s['name']} ({_hours((now - s['_last']).total_seconds())} ago)" for s in offline[:6])
        all_quiet = len(offline) == len([s for s in st if s["enabled"]]) and len(offline) > 1
        add("error" if all_quiet else "warn", f"{len(offline)} vehicle(s) with no signal",
            f"No data for over {_hours(cfg['OFFLINE_AFTER_SECONDS'])}: {names}{'…' if len(offline) > 6 else ''}.",
            ("Every vehicle stopped at about the same time, so look at the server side first: receiver running, "
             f"port {cfg['PUBLIC_GPS_PORT']} open in the VPS firewall, DNS for {cfg['PUBLIC_GPS_HOST']} pointing at the VPS "
             "(DNS only, not proxied by Cloudflare)." if all_quiet else
             "For a single vehicle: ignition off with sleep enabled, no GSM coverage or SIM data, "
             "power cut, or a changed server address. Check the Port terminal while the vehicle is running."),
            "/terminal/" if (user and user.is_staff) else "/connectivity/")
    if never:
        add("warn", f"{len(never)} tracker(s) have never connected",
            "Registered but no data yet: " + ", ".join(s["name"] for s in never[:6]) + ".",
            f"In the FMC130 Configurator set GPRS → server {endpoint}, protocol TCP, and the SIM's APN. "
            "Then watch the Port terminal for the first connection.", "/devices/")
    if stale:
        add("info", f"{len(stale)} vehicle(s) reporting slowly",
            "Last data between 15 and 60 minutes ago: " + ", ".join(s["name"] for s in stale[:6]) + ".",
            "Often normal when parked (longer send period). If moving, check GSM signal on the vehicle panel.")
    if disabled:
        add("info", f"{len(disabled)} tracker(s) disabled",
            ", ".join(s["name"] for s in disabled[:6]) + " - the receiver refuses them until enabled.", "", "/devices/")
    nofix = [s for s in st if s["link"] == "online" and (s["fix"] is None or (s["fix"]["sats"] or 0) < 4)]
    if nofix:
        add("warn", f"{len(nofix)} online vehicle(s) without a good GPS fix",
            "Data arrives but the position is missing or uses fewer than 4 satellites: "
            + ", ".join(s["name"] for s in nofix[:6]) + ".",
            "Check the GNSS antenna placement (clear view of the sky, not under metal) and its cable.")
    lowv = [s for s in st if s["link"] in ("online", "stale") and s["ext_v"] is not None and 0 < s["ext_v"] < 11.5]
    if lowv:
        add("warn", f"{len(lowv)} vehicle(s) with low supply voltage",
            ", ".join(f"{s['name']} {s['ext_v']:.1f} V" for s in lowv[:6]) + ".",
            "Below ~11.5 V on a 12 V vehicle the battery is weak or the tracker is wired to a switched line.")

    since = now - timedelta(hours=24)
    rejected = Connection.objects.filter(opened_utc__gte=since, handshake__in=["rejected", "invalid"])
    n_rej = rejected.count()
    if n_rej:
        from .models import mask_imei
        imeis = sorted({mask_imei(c.claimed_imei) for c in rejected[:200] if c.claimed_imei})
        add("warn", f"{n_rej} rejected connection(s) in 24 h",
            "Trackers tried to connect but were refused" + (f" (IMEIs {', '.join(imeis[:5])})" if imeis else "") + ".",
            "A rejected IMEI is not registered or is disabled: register / enable it on Devices. "
            "“invalid” means the first bytes were not a Teltonika IMEI handshake (port scanners, wrong protocol).",
            "/connectivity/")
    errs = RawPacket.objects.filter(received_utc__gte=since, parse_status="error")
    n_err = errs.count()
    if n_err:
        last = errs.first()
        add("warn", f"{n_err} packet(s) failed to decode in 24 h",
            f"Last error: {last.error[:160] if last else ''}",
            "CRC errors point to a bad link or a non-Teltonika sender; codec errors to the device's data protocol "
            "setting (use Codec 8 Extended).", "/raw/?status=error" if (user and can_raw(user)) else "")
    run = TestRun.objects.order_by("-started_utc").first()
    if run and not run.passed and run.started_utc >= now - timedelta(days=2):
        bad = next((s for s in run.steps if not s.get("ok")), {})
        add("warn", "The last connectivity self-test failed",
            f"{run.summary} against {run.target}. First failing step: {bad.get('name', '?')} - {bad.get('detail', '')}"[:300],
            "Fix that step, then run the self-test again on Connectivity.", "/connectivity/")

    client = client or {}
    gerr = str(client.get("google_error") or "")[:60]
    if gerr or client.get("google_failed"):
        add("error", "Google Maps was rejected in your browser",
            f"Google reported {gerr or 'a failure'}; the map fell back to OpenStreetMap.",
            "Settings → Integrations → Test key shows the exact reason. Most common: add https://gps.melticetech.com/* "
            "to the key's website restrictions, enable the Maps JavaScript API, and turn on billing.", "/settings/#integrations")
    if not integrations.get("google_maps_api_key"):
        add("info", "Google maps are not configured",
            "The maps use free OpenStreetMap / CARTO / Esri layers.",
            "Add a Google Maps key on Settings → Integrations for Google Road, Satellite and Hybrid.", "/settings/#integrations")
    js = [str(e)[:200] for e in (client.get("js_errors") or [])][:3]
    if js:
        add("warn", "This page reported script errors", " | ".join(js),
            "Reload the page (Ctrl+F5). If it persists after a deploy, run collectstatic and clear the Cloudflare cache.")
    ai = ai_status()
    if get_config()["provider"] != "off" and not ai["ready"] and user and user.is_staff:
        add("info", "AI answers are off", ai["label"] + ".",
            "Add an Anthropic API key on Settings → Integrations to let me answer free-form questions.",
            "/settings/#assistant")

    order = {"error": 0, "warn": 1, "info": 2}
    out.sort(key=lambda f: order.get(f["level"], 3))
    if not any(f["level"] in ("error", "warn") for f in out):
        out.insert(0, {"level": "ok", "title": "All good",
                       "detail": f"Receiver online, {len(st)} tracker(s), no errors in the last 24 h.", "fix": "", "link": ""})
    return out


def can_raw(user) -> bool:
    from .views import can_view_raw
    return can_view_raw(user)


def snapshot(user=None, nav: str = "", page_text: str = "", findings=None) -> str:
    """Compact plain-text picture of the app for the AI model."""
    from .views import device_status, receiver_status

    now = timezone.now()
    cfg = settings.FLEET
    rx = receiver_status()
    lines = [f"Time (UTC): {now:%Y-%m-%d %H:%M}. Display time zone: {settings.TIME_ZONE}.",
             f"Tracker endpoint configured on devices: {cfg['PUBLIC_GPS_HOST']}:{cfg['PUBLIC_GPS_PORT']} (TCP). "
             f"Receiver binds {cfg['RECEIVER_BIND_HOST']}:{cfg['RECEIVER_PORT']}.",
             f"Vehicle goes stale after {cfg['STALE_AFTER_SECONDS']} s and offline/'No signal' after {cfg['OFFLINE_AFTER_SECONDS']} s without data.",
             "Receiver: " + (f"online, {rx['active']} active connection(s), frames ok {rx['frames_ok']} / bad {rx['frames_bad']}"
                             if rx["up"] else f"OFFLINE ({rx['detail']})"),
             f"Google Maps key set: {'yes' if integrations.get('google_maps_api_key') else 'no'}.",
             f"User: {user.get_username() if user else '?'} ({'staff' if user and user.is_staff else 'viewer'}). Page: {nav or '?'}."]
    devices = list(Device.objects.all()[:60])
    lines.append(f"Vehicles ({Device.objects.count()}):")
    for d in devices:
        s = device_status(d, now)
        age = f"{_hours((now - d.last_packet_utc).total_seconds())} ago" if d.last_packet_utc else "never"
        fix = s["fix"]
        parts = [f"- {s['name']}", f"plate {s['plate'] or '-'}", f"group {s['group']}", f"model {s['model']}",
                 f"state {s['state']}", f"link {s['link']}", f"last data {age}"]
        if fix:
            parts.append(f"speed {fix['speed']} km/h, sats {fix['sats']}")
        if s["ignition"] is not None:
            parts.append(f"ignition {'on' if s['ignition'] else 'off'}")
        if s["gsm"] is not None:
            parts.append(f"GSM {s['gsm']}/5")
        if s["ext_v"] is not None:
            parts.append(f"ext {s['ext_v']:.1f} V")
        if s["errors"]:
            parts.append(f"{s['errors']} errors, last: {s['last_error'][:80]}")
        lines.append(", ".join(parts))
    findings = findings if findings is not None else diagnostics(user)
    lines.append("Health check findings:")
    for f in findings:
        lines.append(f"- [{f['level']}] {f['title']}: {f['detail']} {('Fix: ' + f['fix']) if f['fix'] else ''}".strip())
    if page_text:
        lines.append("Visible text on the user's current page (may be truncated):\n" + page_text[:1500])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Built-in troubleshooting guide (used when no AI key is set, or the AI call fails)
# ---------------------------------------------------------------------------
GUIDE = [
    (("no signal", "nosignal", "offline", "not updating", "old data", "last values", "not reporting", "stopped"),
     "**“No signal”** means the server has not received data from that tracker for over an hour (or ever).\n"
     "1. If **every** vehicle went quiet together, the problem is on the server side: is the receiver online "
     "(top bar)? Is TCP port 5027 open on the VPS firewall? Does the tracker hostname resolve to the VPS "
     "(DNS only, grey cloud in Cloudflare)?\n"
     "2. If it is **one** vehicle: ignition off with deep sleep, no GSM coverage, SIM out of data, power cut, "
     "or its server address was changed.\n"
     "3. Watch **Port terminal** while the vehicle runs: no connection at all means traffic is not reaching us; "
     "a connection marked *rejected* means the IMEI is not registered."),
    (("receiver", "port 5027", "5027", "tcp", "listening", "firewall"),
     "The **receiver** is the background service that listens on TCP 5027 for trackers. The pill in the top bar "
     "shows its heartbeat.\n- Offline: restart it on the VPS (`./deploy.sh` restarts all services).\n"
     "- Online but no trackers connect: check the VPS/hPanel firewall allows 5027, and that the device points at "
     "the DNS-only hostname (Cloudflare does not proxy raw TCP).\n"
     "- Test from any PC: `python tools/fmc_probe.py <host> 5027`, or run the self-test on **Connectivity**."),
    (("google", "map key", "maps key", "satellite", "map disappeared", "referer", "billing", "map layer", "layers"),
     "Google maps need a key on **Settings → Integrations**. If the Google layers disappear, Google rejected the "
     "key; **Test key** shows the reason:\n- *RefererNotAllowedMapError*: add `https://gps.melticetech.com/*` to the "
     "key's website restrictions.\n- *ApiNotActivatedMapError*: enable the Maps JavaScript API.\n"
     "- *BillingNotEnabledMapError*: turn on billing in Google Cloud.\nThe free OpenStreetMap, CARTO and Esri layers "
     "always work; switch with the layers button on the map."),
    (("rejected", "unknown imei", "register", "add tracker", "add a new tracker", "new device", "imei"),
     "To add a tracker open **Devices**, enter its 15-digit IMEI (on the label, or send the SMS `getimei`) and a "
     "name, then save. The receiver only accepts registered, enabled IMEIs, so an unregistered tracker shows up as "
     "a *rejected* connection on **Connectivity** and in the **Port terminal**."),
    (("configure", "configurator", "apn", "server address", "fmc130", "setup device", "codec"),
     "FMC130 settings (Teltonika Configurator):\n- GPRS → APN of the SIM operator.\n- GPRS → server domain and port: "
     "the tracker hostname and **5027**, protocol **TCP**, ACK type AVL.\n- System → data protocol **Codec 8 Extended**.\n"
     "- Records: open link timeout 300 s.\nThe full bench values are at the bottom of **Settings**."),
    (("crc", "parse", "decode", "error packet", "bad frame", "ack"),
     "A **CRC error** means the bytes we received don't match their checksum: a corrupted link or a sender that is "
     "not a Teltonika tracker. We don't ACK a bad frame, so the tracker resends it. Open **Raw data → status = error** "
     "to see the packets. Repeated codec errors usually mean the device's data protocol setting is wrong."),
    (("self-test", "selftest", "self test", "probe"),
     "The **self-test** on Connectivity plays a tracker: TCP connect, IMEI handshake, a Codec 8E frame and the ACK. "
     "If *connect* fails the receiver isn't listening; if the *handshake* fails the self-test IMEI isn't registered; "
     "if the *ACK* fails the receiver couldn't store the record (check the database)."),
    (("history", "route", "replay", "trip"),
     "On **History** pick a vehicle and a window. Only records with a valid GPS fix are drawn. An empty route "
     "usually means the vehicle sent nothing in that window or had no GPS fix (antenna)."),
    (("gps fix", "satellite count", "sats", "antenna", "invalid fix", "position wrong"),
     "No GPS fix or fewer than 4 satellites usually means the GNSS antenna can't see the sky (under metal, "
     "behind tinted glass) or its cable is loose. The tracker still sends data, but with the last known position."),
    (("battery", "voltage", "power"),
     "**External voltage** below ~11.5 V on a 12 V vehicle means a weak battery or the tracker is wired to a "
     "switched line. The tracker's own backup battery keeps it reporting for a while after power is cut."),
    (("claude", "anthropic", "openai", "ai key", "api key", "assistant", "robot", "configure you"),
     "An admin sets me up on **Settings → AI assistant**: my name, where I sit on the screen, who may use me and "
     "which AI answers questions. Add an Anthropic API key on **Settings → Integrations** for Claude (the default), "
     "or an OpenAI key and model."),
    (("password", "user", "login", "account", "sign in", "locked"),
     "Change your password from the user menu (top right). Admins create users from **Manage users** in the same menu. "
     "After several wrong passwords sign-in is locked for a few minutes."),
    (("deploy", "update", "upgrade", "collectstatic"),
     "To deploy the latest version on the VPS: `cd ~/projects/gpstracker && ./deploy.sh` (pulls, installs, migrates, "
     "collects static files and restarts the services)."),
]


def offline_answer(question: str, nav: str = "", findings=None) -> str:
    q = (question or "").lower()
    if re.search(r"\b(problem|issue|wrong|check|health|diagnos|broken|not working)\b", q):
        findings = findings if findings is not None else []
        bad = [f for f in findings if f["level"] in ("error", "warn")]
        if not bad:
            return "I checked the receiver, the vehicles and the last 24 hours of traffic: **everything looks fine**."
        return "Here's what I found:\n" + "\n".join(
            f"- **{f['title']}**: {f['detail']}" + (f" *Fix:* {f['fix']}" if f["fix"] else "") for f in bad[:6])
    scored = []
    for keys, answer in GUIDE:
        score = sum(len(k) for k in keys if k.lower() in q)
        if score:
            scored.append((score, answer))
    if scored:
        scored.sort(key=lambda x: -x[0])
        return scored[0][1]
    info = page_info(nav)
    return (f"I don't have an answer for that in my built-in guide. {info['about']}\n\nTry one of: "
            + "; ".join(f"“{a}”" for a in info["ask"]) + ". An admin can connect Claude on Settings → AI assistant "
            "so I can answer any question.")


# ---------------------------------------------------------------------------
# AI answers
# ---------------------------------------------------------------------------
SYSTEM = """You are the built-in assistant of the Meltice GPS Tracker web app, shown as a small robot on every page.
You help fleet operators and admins use the app and troubleshoot problems.

About the app:
- Teltonika trackers (mainly FMC130, also FMC920) send Codec 8 / 8 Extended AVL data over TCP to a receiver
  service on port 5027. The receiver only accepts IMEIs registered and enabled on the Devices page, stores the raw
  packet, decodes records, and ACKs only after the database commit.
- Web pages: Dashboard (counts and 24 h traffic), Live map (vehicles, gauges panel, base map switcher with Google /
  OSM / CARTO / Esri layers), History (route replay), Records (decoded AVL records and I/O values), Raw data (hex
  packets, CRC, ACK; staff and raw_data group only), Car view (/drive/, full-screen phone view for driving with
  Start/Stop, locate me, and a pin to save the current spot), Saved places (list, Navigate, KML/CSV/GeoJSON export
  for Google My Maps / Earth, import), Devices (allow-list, retention per vehicle), Port terminal
  (live connection/byte log; staff/raw only), Connectivity (receiver status, recent and rejected connections,
  self-test), Settings (environment, integration keys for Google Maps / Anthropic / OpenAI, this assistant,
  FMC130 configurator values), REST API at /api/v1/ with docs at /api/docs/.
- Vehicle states: moving, idling (ignition on), parked, "No signal" (no data for longer than the offline threshold,
  never connected, or disabled). Stale means 15-60 min without data.
- Production: web at https://gps.melticetech.com (behind Cloudflare proxy). Trackers must use a DNS-only
  hostname (e.g. gpsdevice.melticetech.com) on port 5027, because Cloudflare does not proxy raw TCP.
  The VPS is updated with `cd ~/projects/gpstracker && ./deploy.sh` (pip install, migrate, collectstatic, restart).
- Google Maps errors: RefererNotAllowedMapError (add the site to the key's website restrictions),
  ApiNotActivatedMapError (enable Maps JavaScript API), BillingNotEnabledMapError, InvalidKeyMapError.
- Diagnosis tools: tools/fmc_probe.py <host> 5027 from any PC, the Connectivity self-test, the Port terminal.

How to answer:
- Use the live snapshot below; quote real vehicle names and numbers from it. Never invent data that is not there.
- Be practical: say what is wrong, why, and the exact next step (which page, which setting, which command).
- Use short Markdown: **bold**, bullet or numbered lists, `code`. No tables, no headings. No emojis.
- If the user asks about something outside this app and fleet tracking, answer briefly and steer back.
- You cannot change settings or data yourself; tell the user where to click. Never reveal API keys.
"""


def _messages(history, question):
    msgs = []
    for h in (history or [])[-10:]:
        role = h.get("role") if isinstance(h, dict) else None
        text = str(h.get("text") or "")[:2000] if isinstance(h, dict) else ""
        if role in ("user", "assistant") and text.strip():
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"] += "\n\n" + text
            else:
                msgs.append({"role": role, "content": text})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] += "\n\n" + question
    else:
        msgs.append({"role": "user", "content": question})
    return msgs


def _dynamic_system(cfg, context: str) -> str:
    style = ("Keep answers short: a sentence or two, or up to 5 bullets." if cfg["detail"] == "short"
             else "Give complete step-by-step answers when the question needs it.")
    extra = f"\nExtra instructions from the administrator:\n{cfg['instructions']}" if cfg["instructions"] else ""
    return f"Your name is {cfg['name']}. {style}{extra}\n\nLive snapshot of the app right now:\n{context}"


def ask_claude(key, model, cfg, context, history, question) -> str:
    import anthropic

    client = anthropic.Anthropic(api_key=key, timeout=60, max_retries=1)
    kwargs = dict(
        model=model, max_tokens=4000,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": _dynamic_system(cfg, context)}],
        messages=_messages(history, question),
    )
    if model.startswith(_EFFORT_PREFIXES):
        kwargs["output_config"] = {"effort": "low" if cfg["detail"] == "short" else "medium"}
    if model in _FALLBACK_MODELS:
        # If a safety classifier declines, the API retries on a suitable fallback model in the same call.
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    else:
        resp = client.messages.create(**kwargs)
    if resp.stop_reason == "refusal":
        return "Sorry, I can't help with that one. Try asking about the tracker, the maps or a page in the app."
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if resp.stop_reason == "max_tokens":
        text += "\n\n*(answer cut short)*"
    return text or "I didn't get an answer back. Please try again."


def ask_openai(key, model, cfg, context, history, question) -> str:
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    msgs = [{"role": "system", "content": SYSTEM + "\n" + _dynamic_system(cfg, context)}] + _messages(history, question)
    body = json.dumps({"model": model, "messages": msgs, "max_completion_tokens": 2000}).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode())
    return (data["choices"][0]["message"].get("content") or "").strip() or "I didn't get an answer back."


def _provider_error(exc) -> str:
    try:
        import anthropic
        if isinstance(exc, anthropic.AuthenticationError):
            return "the Anthropic key was rejected (Settings → Integrations)"
        if isinstance(exc, anthropic.NotFoundError):
            return "the Claude model name is not available to this key"
        if isinstance(exc, anthropic.RateLimitError):
            return "the AI provider's rate limit was reached"
        if isinstance(exc, anthropic.APIConnectionError):
            return "the server could not reach Anthropic"
        if isinstance(exc, anthropic.APIStatusError):
            return f"Anthropic answered HTTP {exc.status_code}"
    except ImportError:
        return "the anthropic package is not installed on the server (run ./deploy.sh)"
    if isinstance(exc, urllib.error.HTTPError):
        return f"OpenAI answered HTTP {exc.code}"
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        return "the server could not reach the AI provider"
    return type(exc).__name__


def rate_limited(user) -> bool:
    k = f"assistant:rate:{user.pk}"
    try:
        cache.add(k, 0, CHAT_WINDOW)
        n = cache.incr(k)
    except ValueError:
        cache.set(k, 1, CHAT_WINDOW)
        n = 1
    return n > CHAT_LIMIT


def answer(user, question: str, history=None, nav: str = "", page_text: str = "", client=None) -> dict:
    cfg = get_config()
    findings = diagnostics(user, client)
    ai = ai_status(cfg)
    if not ai["ready"]:
        return {"reply": offline_answer(question, nav, findings), "source": "guide"}
    context = snapshot(user, nav, page_text, findings)
    try:
        if ai["provider"] == "claude":
            reply = ask_claude(integrations.get("anthropic_api_key"), ai["model"], cfg, context, history, question)
        else:
            reply = ask_openai(integrations.get("openai_api_key"), ai["model"], cfg, context, history, question)
        return {"reply": reply, "source": ai["provider"]}
    except Exception as exc:  # noqa: BLE001 - never break the chat; fall back to the guide
        log.warning("assistant AI call failed: %s", exc)
        why = _provider_error(exc)
        note = f"\n\n*(AI answer unavailable: {why}. This is from my built-in guide.)*"
        return {"reply": offline_answer(question, nav, findings) + note, "source": "guide", "error": why}
