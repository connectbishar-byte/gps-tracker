"""
Alert rule engine (Alerts page, /alerts/).

A rule says *when* (kind + settings), *which vehicles* (none = all), *who* (numbers; blank = the default WhatsApp
numbers) and *how* (channels: WhatsApp now; "sms" is accepted and logged as not available yet, so SMS can be added in
deliver() without changing the rules). Every new record is checked right after the frame is committed
(ingest.ingest_frame → process()), and geofence crossings arrive through on_geofence().

Kinds
  geofence_enter / geofence_exit   a vehicle enters / leaves a zone (one zone or any zone)
  overspeed     GPS speed above speed_limit for at least duration_s; one alert per episode, the episode ends when the
                speed drops 3 km/h below the limit
  idle          ignition on and standing still (< 3 km/h) for duration_s (default 10 min); one alert per stop
  crash         the tracker reports a crash (AVL 247)
  harsh         harsh acceleration, braking or cornering (AVL 253 green driving)
  ignition_on / ignition_off   ignition (AVL 239) changes

Every rule has a cooldown (minutes per vehicle). Events older than FLEET_GEOFENCE_ALERT_MAX_AGE_MIN (tracker backlog)
are logged but not sent. Sending runs in a background thread so the tracker's ACK is never delayed.
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from .models import AlertEvent, AlertRule, AlertRuleState, Device

log = logging.getLogger("fleet.rules")

CRASH, GREEN_DRIVING, IGNITION = 247, 253, 239
HARSH_TYPES = {1: "harsh acceleration", 2: "harsh braking", 3: "harsh cornering"}
OVERSPEED_RESET_KMH = 3
STILL_KMH = 3

DEFAULT_TEXT = {
    "geofence_enter": "🟢 {vehicle} entered {zone} at {time}, {speed} km/h. {map}",
    "geofence_exit": "🔴 {vehicle} left {zone} at {time}, {speed} km/h. {map}",
    "overspeed": "⚠️ {vehicle} is over speed: {speed} km/h (limit {limit} km/h) at {time}. {map}",
    "idle": "⏱️ {vehicle} has been idling for {duration} (ignition on, not moving) since {since}. {map}",
    "crash": "🚨 CRASH detected on {vehicle} at {time}, {speed} km/h. {map}",
    "harsh": "⚠️ {vehicle}: {what} at {time}, {speed} km/h. {map}",
    "ignition_on": "🔑 {vehicle} ignition ON at {time}. {map}",
    "ignition_off": "🅿️ {vehicle} ignition OFF at {time}. {map}",
}
PLACEHOLDERS = ["vehicle", "rule", "zone", "speed", "limit", "time", "since", "duration", "what", "map", "lat", "lon"]


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _fmt_time(ts) -> str:
    return timezone.localtime(ts, ZoneInfo(settings.TIME_ZONE)).strftime("%d %b %H:%M")


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} min"
    return f"{seconds // 3600} h {round(seconds % 3600 / 60)} min"


def render(rule: AlertRule, device_name: str, ts, lat=None, lon=None, speed=0, **extra) -> str:
    values = _Safe(vehicle=device_name, rule=rule.name, speed=speed or 0, limit=rule.speed_limit or "",
                   time=_fmt_time(ts), since=_fmt_time(extra.pop("since", ts)), lat=f"{lat:.6f}" if lat is not None else "",
                   lon=f"{lon:.6f}" if lon is not None else "",
                   map=f"https://maps.google.com/?q={lat:.6f},{lon:.6f}" if lat is not None else "", zone="", what="",
                   duration=_fmt_duration(rule.duration_s))
    values.update({k: v for k, v in extra.items() if v is not None})
    template = (rule.message or "").strip() or DEFAULT_TEXT.get(rule.kind, "{vehicle}: {rule} at {time}. {map}")
    try:
        return template.format_map(values).strip()[:1500]
    except (ValueError, IndexError):  # a stray { in a custom text
        return DEFAULT_TEXT.get(rule.kind, "{vehicle}: {rule}").format_map(values)


def rules_for(device_id: int, kinds) -> list[AlertRule]:
    qs = AlertRule.objects.filter(active=True, kind__in=list(kinds)).prefetch_related("devices")
    return [r for r in qs if not r.devices.all() or any(d.pk == device_id for d in r.devices.all())]


# ---------------------------------------------------------------------------
# Raising an alert
# ---------------------------------------------------------------------------
def _raise(rule, device, st, ts, lat, lon, speed, pending: list, **extra) -> AlertEvent:
    now = timezone.now()
    status, detail = "pending", ""
    max_age = timedelta(minutes=float(settings.FLEET.get("GEOFENCE_ALERT_MAX_AGE_MIN", 30)))
    if now - ts > max_age:
        status, detail = "skipped", f"Not sent: the data is older than {int(max_age.total_seconds() // 60)} min (from the tracker's memory)."
    elif st is not None and st.last_fired_utc and rule.cooldown_min and now - st.last_fired_utc < timedelta(minutes=rule.cooldown_min):
        status, detail = "skipped", f"Not sent: this rule already alerted for this vehicle in the last {rule.cooldown_min} min."
    elif not rule.channels:
        status, detail = "skipped", "Not sent: the rule has no channel ticked."
    if status == "pending" and st is not None:
        st.last_fired_utc = now
    ev = AlertEvent.objects.create(rule=rule, rule_name=rule.name, kind=rule.kind, device=device, timestamp_utc=ts,
                                   latitude=lat, longitude=lon, speed=speed or 0, status=status, detail=detail,
                                   text=render(rule, device.name, ts, lat, lon, speed, **extra))
    if status == "pending":
        pending.append(ev.pk)
    return ev


def deliver(event_id: int) -> None:
    ev = AlertEvent.objects.select_related("rule").filter(pk=event_id).first()
    if not ev:
        return
    rule = ev.rule
    channels = (rule.channels if rule else []) or []
    parts, ok_any, fail_any = [], False, False
    for ch in channels:
        if ch == "whatsapp":
            from . import whatsapp
            res = whatsapp.send(ev.text, rule.recipients if rule else None)
            ok_any |= bool(res.get("sent"))
            fail_any |= not res.get("ok")
            parts.append("WhatsApp: " + res["detail"])
        elif ch == "sms":
            parts.append("SMS: not available yet")
    status = "sent" if ok_any else ("failed" if fail_any else "skipped")
    AlertEvent.objects.filter(pk=ev.pk).update(status=status, detail=" · ".join(parts)[:500])
    if status == "failed":
        log.warning("alert %s not delivered: %s", ev.pk, parts)


def dispatch(event_ids: list[int]) -> None:
    if not event_ids:
        return
    if settings.FLEET.get("GEOFENCE_SYNC_ALERTS"):  # tests
        for pk in event_ids:
            deliver(pk)
        return

    def run():
        try:
            for pk in event_ids:
                deliver(pk)
        finally:
            close_old_connections()

    threading.Thread(target=run, name="alert-send", daemon=True).start()


def send_test(rule: AlertRule) -> dict:
    """Send a sample of this rule's message now (Alerts page → Test)."""
    from . import whatsapp
    dev = rule.devices.first() or Device.objects.exclude(model="SIMULATOR").first()
    lat, lon = (dev.last_fix_lat, dev.last_fix_lon) if dev and dev.last_fix_lat is not None else (24.4539, 54.3773)
    text = "TEST · " + render(rule, dev.name if dev else "Vehicle", timezone.now(), lat, lon, rule.speed_limit or 40,
                              zone=rule.geofence.name if rule.geofence else "Office", what="harsh braking")
    if "whatsapp" not in (rule.channels or []):
        return {"ok": False, "detail": "Tick WhatsApp on the rule first.", "text": text}
    return whatsapp.send(text, rule.recipients or None) | {"text": text}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
def on_geofence(ev, pending: list) -> None:
    """Called inside geofence.process_fixes for every crossing (same transaction)."""
    kind = "geofence_enter" if ev.kind == "enter" else "geofence_exit"
    for rule in rules_for(ev.device_id, [kind]):
        if rule.geofence_id and rule.geofence_id != ev.geofence_id:
            continue
        st, _ = AlertRuleState.objects.get_or_create(rule=rule, device_id=ev.device_id)
        _raise(rule, ev.device, st, ev.timestamp_utc, ev.latitude, ev.longitude, ev.speed, pending, zone=ev.geofence_name)
        st.save()


RECORD_KINDS = ["overspeed", "idle", "crash", "harsh", "ignition_on", "ignition_off"]


def process(device_id: int, recs: list[dict]) -> list[int]:
    """recs: [{"ts", "lat", "lon", "speed", "valid", "io": {avl_id: value}}] new records of one vehicle.
    Returns the AlertEvent ids created. Never raises."""
    if not recs:
        return []
    try:
        device = Device.objects.get(pk=device_id)
        if device.imei == settings.FLEET.get("SELFTEST_IMEI"):
            return []
        rules = rules_for(device_id, RECORD_KINDS)
        if not rules:
            return []
        recs = sorted(recs, key=lambda r: r["ts"])
        created, pending = [], []
        with transaction.atomic():
            states = {s.rule_id: s for s in AlertRuleState.objects.select_for_update().filter(device=device, rule__in=rules)}
            for rule in rules:
                st = states.get(rule.pk) or AlertRuleState.objects.create(rule=rule, device=device)
                for r in recs:
                    if st.last_ts and r["ts"] <= st.last_ts:
                        continue  # late record, already judged newer data
                    ev = _check(rule, device, st, r, pending)
                    st.last_ts = r["ts"]
                    if ev:
                        created.append(ev.pk)
                st.save()
            if pending:
                transaction.on_commit(lambda: dispatch(pending))
        return created
    except Exception:  # pragma: no cover - alerts must never break ingest
        log.exception("rule check failed for device %s", device_id)
        return []


def _check(rule, device, st, r, pending):
    ts, lat, lon, io = r["ts"], r["lat"] if r["valid"] else None, r["lon"] if r["valid"] else None, r["io"]
    speed = r["speed"] if r["valid"] else 0
    k = rule.kind
    if k == "overspeed":
        if not r["valid"] or not rule.speed_limit:
            return None
        if speed > rule.speed_limit:
            if not st.active:
                st.active, st.since_utc, st.fired, st.peak = True, ts, False, speed
            st.peak = max(st.peak or 0, speed)
            if not st.fired and (ts - st.since_utc).total_seconds() >= rule.duration_s:
                st.fired = True
                return _raise(rule, device, st, ts, lat, lon, speed, pending)
        elif speed <= rule.speed_limit - OVERSPEED_RESET_KMH:
            st.active, st.fired = False, False
        return None
    if k == "idle":
        ign = io.get(IGNITION)
        if ign is None:
            ign = 1 if device.last_ignition else 0
        if ign and speed < STILL_KMH:
            if not st.active:
                st.active, st.since_utc, st.fired = True, ts, False
            if not st.fired and (ts - st.since_utc).total_seconds() >= (rule.duration_s or 600):
                st.fired = True
                return _raise(rule, device, st, ts, lat, lon, speed, pending, since=st.since_utc,
                              duration=_fmt_duration((ts - st.since_utc).total_seconds()))
        elif not ign or speed >= STILL_KMH + 2:
            st.active, st.fired = False, False
        return None
    if k == "crash":
        if io.get(CRASH) or r.get("event") == CRASH:
            return _raise(rule, device, st, ts, lat, lon, speed, pending)
        return None
    if k == "harsh":
        t = io.get(GREEN_DRIVING)
        if t in HARSH_TYPES:
            return _raise(rule, device, st, ts, lat, lon, speed, pending, what=HARSH_TYPES[int(t)])
        return None
    if k in ("ignition_on", "ignition_off"):
        ign = io.get(IGNITION)
        if ign is None:
            return None
        on = bool(ign)
        first = st.since_utc is None
        changed = not first and on != st.active
        st.active = on
        if first or changed:
            st.since_utc = ts
        if changed and on == (k == "ignition_on"):
            return _raise(rule, device, st, ts, lat, lon, speed, pending)
    return None
