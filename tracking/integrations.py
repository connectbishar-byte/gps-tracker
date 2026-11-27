"""
Integration settings: Google Maps, Anthropic (Claude) and OpenAI.

An admin sets them on the Settings page; they are stored in the IntegrationSetting table, encrypted
with Fernet using a key derived from Django's SECRET_KEY. If a value has not been set in the UI, the
server environment is used instead (FLEET_GOOGLE_MAPS_API_KEY, ANTHROPIC_API_KEY, ANTHROPIC_MODEL,
OPENAI_API_KEY, OPENAI_MODEL), so
keys can also live in /etc/meltice-gps-tracker/fleet.env or .env.

Note: if SECRET_KEY changes, saved values can no longer be decrypted. They then read as "not set"
and the Settings page asks for them again.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import urllib.error
import urllib.request

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.cache import cache

from .models import IntegrationSetting

log = logging.getLogger("fleet.integrations")

# name -> (label, environment variable, secret?)
FIELDS = {
    "google_maps_api_key": ("Google Maps API key", "FLEET_GOOGLE_MAPS_API_KEY", True),
    "anthropic_api_key": ("Anthropic (Claude) API key", "ANTHROPIC_API_KEY", True),
    "anthropic_model": ("Claude model", "ANTHROPIC_MODEL", False),
    "openai_api_key": ("OpenAI API key", "OPENAI_API_KEY", True),
    "openai_model": ("OpenAI model", "OPENAI_MODEL", False),
    # WhatsApp alerts (geofences). Edited on their own card, Settings → WhatsApp alerts; see tracking/whatsapp.py.
    "whatsapp_provider": ("WhatsApp provider", "FLEET_WHATSAPP_PROVIDER", False),
    "whatsapp_recipients": ("Alert recipients", "FLEET_WHATSAPP_RECIPIENTS", False),
    "whatsapp_meta_token": ("Meta access token", "WHATSAPP_META_TOKEN", True),
    "whatsapp_meta_phone_id": ("Meta phone number ID", "WHATSAPP_META_PHONE_ID", False),
    "whatsapp_meta_template": ("Meta template name", "WHATSAPP_META_TEMPLATE", False),
    "whatsapp_meta_lang": ("Meta template language", "WHATSAPP_META_LANG", False),
    "whatsapp_meta_verify_token": ("Webhook verify token", "WHATSAPP_VERIFY_TOKEN", False),
    "whatsapp_meta_app_secret": ("Meta app secret", "WHATSAPP_APP_SECRET", True),
    "whatsapp_meta_api_version": ("Graph API version", "WHATSAPP_META_API_VERSION", False),
    "whatsapp_twilio_sid": ("Twilio Account SID", "TWILIO_ACCOUNT_SID", False),
    "whatsapp_twilio_token": ("Twilio auth token", "TWILIO_AUTH_TOKEN", True),
    "whatsapp_twilio_from": ("Twilio WhatsApp sender", "TWILIO_WHATSAPP_FROM", False),
    "whatsapp_callmebot_key": ("CallMeBot API key", "CALLMEBOT_API_KEY", True),
}
# fields that belong to the WhatsApp card, not the Integrations card
WHATSAPP_FIELDS = [n for n in FIELDS if n.startswith("whatsapp_")]
CACHE_KEY = "integrations:v3"
CACHE_SECONDS = 60


def _fernet() -> Fernet:
    digest = hashlib.sha256(("meltice-integrations:" + settings.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _decrypt(token: str) -> str | None:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def _load() -> dict:
    """{name: {"value": str, "source": "settings"|"environment"|"", "updated_at", "updated_by", "unreadable"}}"""
    data = cache.get(CACHE_KEY)
    if data is not None:
        return data
    rows = {r.key: r for r in IntegrationSetting.objects.filter(key__in=FIELDS)}
    data = {}
    for name, (_label, env_name, _secret) in FIELDS.items():
        r = rows.get(name)
        entry = {"value": "", "source": "", "updated_at": None, "updated_by": "", "unreadable": False}
        if r and r.value:
            plain = _decrypt(r.value)
            if plain is None:
                entry["unreadable"] = True
                log.warning("Integration setting %s cannot be decrypted (SECRET_KEY changed?)", name)
            else:
                entry.update(value=plain, source="settings",
                             updated_at=r.updated_at.isoformat() if r.updated_at else None, updated_by=r.updated_by)
        if not entry["value"] and os.environ.get(env_name, "").strip():
            entry.update(value=os.environ[env_name].strip(), source="environment")
        data[name] = entry
    cache.set(CACHE_KEY, data, CACHE_SECONDS)
    return data


def get(name: str) -> str:
    return _load()[name]["value"]


def save(name: str, value: str, user: str = "") -> None:
    if name not in FIELDS:
        raise KeyError(name)
    value = (value or "").strip()
    if value:
        IntegrationSetting.objects.update_or_create(key=name, defaults={"value": _encrypt(value), "updated_by": user})
    else:
        IntegrationSetting.objects.filter(key=name).delete()
    cache.delete(CACHE_KEY)


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]}"


def status() -> list[dict]:
    """Rows for the Settings page. Never contains a full secret."""
    out = []
    for name, (label, env_name, secret) in FIELDS.items():
        e = _load()[name]
        out.append({
            "name": name, "label": label, "env": env_name, "secret": secret,
            "set": bool(e["value"]), "source": e["source"], "unreadable": e["unreadable"],
            "display": mask(e["value"]) if secret else e["value"],
            "updated_at": e["updated_at"], "updated_by": e["updated_by"],
        })
    return out


def test_openai(api_key: str, model: str = "", timeout: float = 10) -> dict:
    """List the models the key can use. Sends nothing except the key; costs nothing."""
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    req = urllib.request.Request(base + "/models", headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode()).get("error", {}).get("message", "")
        except Exception:  # noqa: BLE001
            detail = ""
        hint = {401: "The key was rejected.", 403: "The key has no access.", 429: "Rate limit or no credit left."}
        return {"ok": False, "detail": f"HTTP {exc.code}. {hint.get(exc.code, '')} {detail}".strip()}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "detail": f"Could not reach OpenAI from this server: {getattr(exc, 'reason', exc)}"}
    ids = sorted(m.get("id", "") for m in body.get("data", []) if m.get("id"))
    res = {"ok": True, "models": ids, "detail": f"Key works. {len(ids)} models available."}
    if model:
        res["model_ok"] = model in ids
        if not res["model_ok"]:
            res["ok"] = False
            res["detail"] = f"Key works, but the model “{model}” is not available to it."
    return res


def test_anthropic(api_key: str, model: str = "", timeout: float = 10) -> dict:
    """List the Claude models the key can use (free, sends no conversation data)."""
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=0)
    try:
        ids = sorted(m.id for m in client.models.list(limit=100))
    except anthropic.AuthenticationError:
        return {"ok": False, "detail": "HTTP 401. The key was rejected."}
    except anthropic.PermissionDeniedError:
        return {"ok": False, "detail": "HTTP 403. The key has no access."}
    except anthropic.RateLimitError:
        return {"ok": False, "detail": "HTTP 429. Rate limit reached, try again in a minute."}
    except anthropic.APIStatusError as exc:
        return {"ok": False, "detail": f"HTTP {exc.status_code}. {exc.message}"}
    except anthropic.APIConnectionError as exc:
        return {"ok": False, "detail": f"Could not reach Anthropic from this server: {exc}"}
    res = {"ok": True, "models": ids, "detail": f"Key works. {len(ids)} Claude models available."}
    if model:
        res["model_ok"] = model in ids
        if not res["model_ok"]:
            res["ok"] = False
            res["detail"] = f"Key works, but the model “{model}” is not available to it."
    return res
