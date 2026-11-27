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
        "meta_template": g("whatsapp_meta_template"), "meta_lang": g("whatsapp_meta_lang") or "en_US",
        "meta_api_version": g("whatsapp_meta_api_version"),
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


# ---------------------------------------------------------------------------
# Meta WhatsApp Cloud API: the two functions (text alert, image / PDF with caption) and the webhook
# ---------------------------------------------------------------------------
DEFAULT_VERIFY_TOKEN = "meltice_whatsapp_2026"
NUMBER_RE = re.compile(r"[1-9][0-9]{6,14}")


def clean_number(value) -> str:
    """'+971 50-786 4838' -> '971507864838' (Meta wants digits only, country code first)."""
    n = re.sub(r"[\s+\-().]", "", str(value or ""))
    return n[2:] if n.startswith("00") else n


def verify_token() -> str:
    return integrations.get("whatsapp_meta_verify_token") or DEFAULT_VERIFY_TOKEN


def _meta_url(cfg) -> str:
    version = (cfg.get("meta_api_version") or META_API_VERSION).strip() or META_API_VERSION
    return f"https://graph.facebook.com/{version}/{urllib.parse.quote(cfg['meta_phone_id'])}/messages"


def meta_post(payload: dict, cfg: dict | None = None, source: str = "", timeout: float = 30) -> tuple[dict, int]:
    """POST one message to the Cloud API. Returns ({status, status_code, request_payload, meta_response, ...}, code)
    like Bishar's sendCertificate(), and logs it to WhatsAppMessage."""
    cfg = cfg or config()
    if not cfg["meta_token"] or not cfg["meta_phone_id"]:
        return {"status": "error", "message": "WhatsApp API credentials missing (Access token / Phone number ID)."}, 500
    req = urllib.request.Request(_meta_url(cfg), data=json.dumps(payload).encode(), method="POST", headers={
        "Authorization": f"Bearer {cfg['meta_token']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code, raw = r.status, r.read(20000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        code, raw = exc.code, exc.read(20000).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        _log("out", source, payload, "failed", 502, "Unable to connect to WhatsApp API")
        return {"status": "error", "message": "Unable to connect to WhatsApp API"}, 502
    try:
        meta_response = json.loads(raw)
    except ValueError:
        meta_response = raw
    ok = 200 <= code < 300
    wamid = ""
    if ok and isinstance(meta_response, dict):
        wamid = ((meta_response.get("messages") or [{}])[0] or {}).get("id", "")
    detail = "" if ok else (_error_text(raw) or f"HTTP {code}")
    _log("out", source, payload, "accepted" if ok else "failed", code, detail, wamid)
    result = {"status": "success" if ok else "error", "status_code": code, "request_payload": payload,
              "meta_response": meta_response}
    if not ok:
        result["message"] = detail
    return result, code


def _log(direction, source, payload, status, code=None, detail="", wamid="", number="", kind=""):
    try:
        from .models import WhatsAppMessage
        WhatsAppMessage.objects.create(
            direction=direction, source=source[:12], number=(number or payload.get("to", ""))[:20],
            kind=(kind or payload.get("type", ""))[:12], wamid=wamid[:200], status=status[:12], status_code=code,
            detail=str(detail)[:500], payload=payload)
    except Exception:  # noqa: BLE001 - logging must never break sending
        log.exception("could not log WhatsApp message")


def _greeting(customer_name: str, body: str) -> str:
    return (f"Dear {customer_name},\n\n{body}" if customer_name else body).strip()


def send_text(to_number, customer_name: str = "", subject: str = "", text: str = "", cfg=None,
              source: str = "api") -> tuple[dict, int]:
    """Text alert. Body is "*subject*" + "Dear customer," + text. WhatsApp delivers free text only within 24 h of the
    customer's last message; outside that use a template (Settings → WhatsApp → template name)."""
    to = clean_number(to_number)
    if not NUMBER_RE.fullmatch(to):
        return {"status": "error", "message": "Valid international mobile number required"}, 400
    body = _greeting(customer_name, text)
    if subject:
        body = f"*{subject.strip()}*\n\n{body}"
    if not body:
        return {"status": "error", "message": "Message text required"}, 400
    if len(body) > 4096:
        return {"status": "error", "message": "Message exceeds 4096 characters"}, 400
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "text",
               "text": {"preview_url": True, "body": body}}
    return meta_post(payload, cfg, source)


def send_media(to_number, customer_name: str = "", file_url: str = "", message: str = "", file_type: str = "image",
               cfg=None, source: str = "api", filename: str = "") -> tuple[dict, int]:
    """Image or PDF from a public https URL with a caption (Bishar's sendCertificate)."""
    to = clean_number(to_number)
    if not NUMBER_RE.fullmatch(to):
        return {"status": "error", "message": "Valid international mobile number required"}, 400
    file_url = str(file_url or "").strip()
    if not file_url.startswith("https://"):
        return {"status": "error", "message": "Public HTTPS file URL required"}, 400
    caption = _greeting(customer_name, message)
    if len(caption) > 1024:
        return {"status": "error", "message": "Caption exceeds 1024 characters"}, 400
    if file_type == "image":
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "image",
                   "image": {"link": file_url, "caption": caption}}
    elif file_type == "pdf":
        name = filename or f"{(customer_name or 'Document').replace(' ', '_')}_Certificate.pdf"
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": "document",
                   "document": {"link": file_url, "filename": name, "caption": caption}}
    else:
        return {"status": "error", "message": "file_type must be image or pdf"}, 400
    result, code = meta_post(payload, cfg, source)
    result["file_type"] = file_type
    return result, code


def handle_webhook(body: dict) -> int:
    """Store the delivery statuses and incoming messages Meta posts to the webhook. Returns how many were stored."""
    n = 0
    for entry in (body or {}).get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            for st in value.get("statuses", []) or []:
                errs = st.get("errors") or []
                detail = "; ".join(f"{e.get('code')}: {e.get('title') or e.get('message', '')}"
                                   f"{' - ' + (e.get('error_data') or {}).get('details', '') if (e.get('error_data') or {}).get('details') else ''}"
                                   for e in errs)
                if any(str(e.get("code")) == "131047" for e in errs):
                    detail += (" (More than 24 h since the customer last wrote: use an approved template for "
                               "messages outside that window.)")
                _log("status", "webhook", {"status": st.get("status"), "id": st.get("id")}, str(st.get("status", "")),
                     None, detail, st.get("id", ""), st.get("recipient_id", ""), "status")
                n += 1
            for m in value.get("messages", []) or []:
                text = (m.get("text") or {}).get("body", "") or m.get("type", "")
                _log("in", "webhook", {"type": m.get("type"), "text": text[:300]}, "received", None, text[:500],
                     m.get("id", ""), m.get("from", ""), m.get("type", ""))
                n += 1
    return n


def check_signature(raw: bytes, header: str) -> bool:
    """X-Hub-Signature-256 check, only when an app secret is saved (otherwise accept)."""
    import hashlib
    import hmac
    secret = integrations.get("whatsapp_meta_app_secret")
    if not secret:
        return True
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def _send_meta(cfg, phone, text):
    to = phone.lstrip("+")
    if cfg["meta_template"]:
        payload = {"messaging_product": "whatsapp", "to": to, "type": "template", "template": {
            "name": cfg["meta_template"], "language": {"code": cfg["meta_lang"] or "en_US"},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": _single_line(text)}]}]}}
        res, code = meta_post(payload, cfg, "alert")
    else:
        res, code = send_text(to, text=text, cfg=cfg, source="alert")
    if res["status"] == "success":
        return True, "accepted by Meta"
    body = res.get("message", f"HTTP {code}")
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
