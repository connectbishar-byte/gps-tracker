"""
WhatsApp alerts (used by geofences).

Three providers, chosen on Settings → WhatsApp alerts:

* "meta"      Meta WhatsApp Cloud API (the official business API). Needs a permanent access token and the
              phone number ID from developers.facebook.com → your app → WhatsApp → API setup. WhatsApp only lets
              a business start a conversation with an approved *template*; create one with a single body
              variable, e.g. "Meltice GPS alert: {{1}}", and enter its name. Without a template name a plain text
              message is sent, which WhatsApp delivers only within 24 h after the recipient last wrote to the
              business number.
* "twilio"    Twilio's WhatsApp API (the sandbox works for testing: each recipient first sends the sandbox's
              "join <code>" message to Twilio's number).
* "callmebot" CallMeBot, a free gateway for personal alerts to your own phone. Each recipient gets an API key
              by sending "I allow callmebot to send me messages" to CallMeBot's WhatsApp number.

Recipients are a list of phone numbers in international format (+9715…), one per line or comma separated.
For CallMeBot a number may carry its own key as "+971501234567:1234567"; otherwise the shared key is used.

Every value is stored encrypted through tracking/integrations.py (environment variables as fallback).
Nothing here raises: send() returns {"ok": bool, "detail": str, "results": [...]}.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from . import integrations

log = logging.getLogger("fleet.whatsapp")

PROVIDERS = {
    "": "Off (no WhatsApp alerts)",
    "meta": "Meta WhatsApp Cloud API",
    "twilio": "Twilio WhatsApp",
    "callmebot": "CallMeBot (free, personal)",
}
META_API_VERSION = os.environ.get("WHATSAPP_META_API_VERSION", "v22.0")
TIMEOUT = 15
MAX_RECIPIENTS = 20


def config() -> dict:
    g = integrations.get
    return {
        "provider": g("whatsapp_provider") if g("whatsapp_provider") in PROVIDERS else "",
        "recipients": g("whatsapp_recipients"),
        "meta_token": g("whatsapp_meta_token"), "meta_phone_id": g("whatsapp_meta_phone_id"),
        "meta_template": g("whatsapp_meta_template"), "meta_lang": g("whatsapp_meta_lang") or "en",
        "twilio_sid": g("whatsapp_twilio_sid"), "twilio_token": g("whatsapp_twilio_token"),
        "twilio_from": g("whatsapp_twilio_from"),
        "callmebot_key": g("whatsapp_callmebot_key"),
    }


def parse_recipients(text: str) -> tuple[list[dict], list[str]]:
    """'+971 50 123 4567, +97155…:key' -> ([{"phone": "+971501234567", "key": ""}, ...], [errors])."""
    out, errors = [], []
    for raw in re.split(r"[,;\n]+", text or ""):
        raw = raw.strip()
        if not raw:
            continue
        number, _, key = raw.partition(":")
        digits = re.sub(r"[\s\-().]", "", number)
        if digits.startswith("00"):
            digits = "+" + digits[2:]
        if not re.fullmatch(r"\+?\d{8,15}", digits):
            errors.append(f"“{raw}” is not a phone number. Use international format, e.g. +971501234567.")
            continue
        out.append({"phone": "+" + digits.lstrip("+"), "key": key.strip()})
    if len(out) > MAX_RECIPIENTS:
        errors.append(f"At most {MAX_RECIPIENTS} recipients.")
        out = out[:MAX_RECIPIENTS]
    return out, errors


def missing(cfg: dict | None = None) -> list[str]:
    """What still has to be filled in before alerts can go out (empty list = ready)."""
    cfg = cfg or config()
    p = cfg["provider"]
    if not p:
        return ["choose a provider"]
    need = []
    if p == "meta":
        need += [label for key, label in (("meta_token", "access token"), ("meta_phone_id", "phone number ID")) if not cfg[key]]
    elif p == "twilio":
        need += [label for key, label in (("twilio_sid", "Account SID"), ("twilio_token", "auth token"),
                                           ("twilio_from", "sender number")) if not cfg[key]]
    recips, _ = parse_recipients(cfg["recipients"])
    if not recips:
        need.append("at least one recipient number")
    elif p == "callmebot" and not cfg["callmebot_key"] and any(not r["key"] for r in recips):
        need.append("the CallMeBot API key")
    return need


def status() -> dict:
    cfg = config()
    need = missing(cfg)
    return {"provider": cfg["provider"], "label": PROVIDERS[cfg["provider"]], "ready": not need, "missing": need,
            "recipients": len(parse_recipients(cfg["recipients"])[0])}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
def _http(req: urllib.request.Request) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return True, r.read(2000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = exc.read(2000).decode("utf-8", "replace")
        return False, f"HTTP {exc.code}: {_error_text(body) or exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"Could not reach the provider from this server: {getattr(exc, 'reason', exc)}"


def _error_text(body: str) -> str:
    try:
        d = json.loads(body)
    except ValueError:
        return re.sub(r"<[^>]+>", " ", body).strip()[:300]
    err = d.get("error") if isinstance(d, dict) else None
    if isinstance(err, dict):  # Meta
        msg = err.get("message", "")
        extra = (err.get("error_data") or {}).get("details", "")
        return f"{msg} {extra}".strip()[:300]
    if isinstance(d, dict) and d.get("message"):  # Twilio
        return str(d["message"])[:300]
    return body[:300]


def _single_line(text: str) -> str:
    # Meta template parameters may not contain new lines, tabs or more than 4 spaces in a row
    return re.sub(r"\s+", " ", text).strip()[:1000]


def _send_meta(cfg, phone, text):
    url = f"https://graph.facebook.com/{META_API_VERSION}/{urllib.parse.quote(cfg['meta_phone_id'])}/messages"
    to = phone.lstrip("+")
    if cfg["meta_template"]:
        payload = {"messaging_product": "whatsapp", "to": to, "type": "template", "template": {
            "name": cfg["meta_template"], "language": {"code": cfg["meta_lang"] or "en"},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": _single_line(text)}]}]}}
    else:
        payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4096]}}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={
        "Authorization": f"Bearer {cfg['meta_token']}", "Content-Type": "application/json"})
    ok, body = _http(req)
    if ok:
        return True, "accepted by Meta"
    if "131047" in body or "24 hours" in body or "re-engagement" in body.lower():
        body += (" WhatsApp only delivers plain messages within 24 h of the recipient's last message to your number."
                 " Enter an approved template name to send alerts at any time.")
    return False, body


def _send_twilio(cfg, phone, text):
    sid = cfg["twilio_sid"]
    url = f"https://api.twilio.com/2010-04-01/Accounts/{urllib.parse.quote(sid)}/Messages.json"
    sender = cfg["twilio_from"].strip()
    if not sender.startswith("whatsapp:"):
        sender = "whatsapp:" + sender
    data = urllib.parse.urlencode({"From": sender, "To": "whatsapp:" + phone, "Body": text[:1600]}).encode()
    auth = base64.b64encode(f"{sid}:{cfg['twilio_token']}".encode()).decode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Authorization": "Basic " + auth, "Content-Type": "application/x-www-form-urlencoded"})
    ok, body = _http(req)
    if ok:
        return True, "accepted by Twilio"
    if "63015" in body or "sandbox" in body.lower():
        body += " (Sandbox: the recipient must first send the 'join <code>' message to the Twilio number.)"
    return False, body


def _send_callmebot(cfg, phone, text, key):
    key = key or cfg["callmebot_key"]
    url = "https://api.callmebot.com/whatsapp.php?" + urllib.parse.urlencode(
        {"phone": phone, "text": text[:1000], "apikey": key})
    ok, body = _http(urllib.request.Request(url, headers={"User-Agent": "MelticeGPSTracker/1.0"}))
    low = body.lower()
    if ok and ("message queued" in low or "message sent" in low or "will receive" in low):
        return True, "queued by CallMeBot"
    if ok:
        # CallMeBot answers 200 with an HTML page even for errors such as a wrong API key
        return False, "CallMeBot said: " + (re.sub(r"<[^>]+>", " ", body).split("  ")[0].strip()[:200] or "unknown reply")
    return False, body


def send(text: str, recipients: str | None = None, cfg: dict | None = None) -> dict:
    """Send `text` to every recipient (the zone's own list if given, else the default list)."""
    cfg = cfg or config()
    if not cfg["provider"]:
        return {"ok": False, "detail": "WhatsApp alerts are off (Settings → WhatsApp alerts).", "results": []}
    recips, errors = parse_recipients(recipients if (recipients or "").strip() else cfg["recipients"])
    need = missing(cfg | {"recipients": recipients if (recipients or "").strip() else cfg["recipients"]})
    if need:
        return {"ok": False, "detail": "WhatsApp is not set up: " + ", ".join(need) + ".", "results": []}
    results = []
    for r in recips:
        try:
            if cfg["provider"] == "meta":
                ok, detail = _send_meta(cfg, r["phone"], text)
            elif cfg["provider"] == "twilio":
                ok, detail = _send_twilio(cfg, r["phone"], text)
            else:
                ok, detail = _send_callmebot(cfg, r["phone"], text, r["key"])
        except Exception as exc:  # noqa: BLE001 - an alert must never crash the caller
            log.exception("WhatsApp send failed")
            ok, detail = False, f"error: {exc}"
        results.append({"phone": mask_phone(r["phone"]), "ok": ok, "detail": detail})
    sent = sum(x["ok"] for x in results)
    summary = "; ".join(f"{x['phone']} {'✓' if x['ok'] else '✗ ' + x['detail']}" for x in results)
    if errors:
        summary += " · " + " ".join(errors)
    return {"ok": sent == len(results) and sent > 0, "sent": sent, "detail": summary[:500], "results": results}


def mask_phone(phone: str) -> str:
    return phone if len(phone) < 8 else phone[:4] + "•" * (len(phone) - 7) + phone[-3:]
