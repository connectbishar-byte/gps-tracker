"""
Pages and JSON endpoints for geofences, and the Settings → WhatsApp alerts card.

  /geofences/                       map page: draw zones, see crossings (everyone signed in; staff edit)
  /geofences/api/                   GET list of zones (also drawn on the Live map) · POST create (staff)
  /geofences/api/<id>/              POST update (staff)
  /geofences/api/<id>/delete/       POST delete (staff)
  /geofences/events/                GET latest crossings (?after=<id> for new ones only)
  /settings/whatsapp/               POST save WhatsApp settings (staff)
  /settings/whatsapp/test/          POST send a test message (staff)
"""
from __future__ import annotations

import json

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import geofence, integrations, whatsapp
from .models import Device, Geofence, GeofenceEvent, GeofenceState

staff_required = user_passes_test(lambda u: u.is_active and u.is_staff)


def _json_body(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _events(after: int = 0, limit: int = 100):
    qs = GeofenceEvent.objects.select_related("device")
    if after:
        qs = qs.filter(pk__gt=after)
    return [geofence.event_dict(e) for e in qs.order_by("-id")[:limit]]


@login_required
def geofences_page(request):
    fences = [geofence.as_dict(f) for f in Geofence.objects.prefetch_related("devices")]
    inside = {}
    for s in GeofenceState.objects.filter(inside=True).select_related("device"):
        inside.setdefault(s.geofence_id, []).append(s.device.name)
    for f in fences:
        f["inside"] = inside.get(f["id"], [])
    vehicles = [{"id": d.pk, "name": d.name, "lat": d.last_fix_lat, "lon": d.last_fix_lon,
                 "fix": d.last_fix_ts.isoformat() if d.last_fix_ts else None}
                for d in Device.objects.filter(enabled=True).exclude(model="SIMULATOR")]
    return render(request, "tracking/geofences.html", {
        "nav": "geofences", "can_edit": request.user.is_staff, "wa": whatsapp.status(),
        "page_data": {"fences": fences, "vehicles": vehicles, "events": _events(),
                      "can_edit": request.user.is_staff},
    })


def _apply(fence: Geofence, data: dict, user) -> tuple[Geofence | None, list[str]]:
    clean, errors = geofence.validate(data)
    if errors:
        return None, errors
    ids = clean.pop("device_ids")
    known = set(Device.objects.filter(pk__in=ids).values_list("pk", flat=True))
    if set(ids) - known:
        return None, ["Unknown vehicle."]
    with transaction.atomic():
        before = (fence.shape, fence.center_lat, fence.center_lon, fence.radius_m, fence.points) if fence.pk else None
        before_devices = set(fence.devices.values_list("pk", flat=True)) if fence.pk else None
        for k, v in clean.items():
            setattr(fence, k, v)
        if not fence.pk:
            fence.created_by = user.get_username()
        fence.save()
        fence.devices.set(ids)
        after = (fence.shape, fence.center_lat, fence.center_lon, fence.radius_m, fence.points)
        if before is not None and (before != after or before_devices != set(ids)):
            # the zone moved: start over, so the next fix only records where each vehicle is (no false alert)
            GeofenceState.objects.filter(geofence=fence).delete()
    return fence, []


@login_required
def api_geofences(request):
    if request.method == "GET":
        return JsonResponse({"fences": [geofence.as_dict(f) for f in Geofence.objects.prefetch_related("devices")]})
    if request.method != "POST":
        return JsonResponse({"detail": "Method not allowed."}, status=405)
    if not request.user.is_staff:
        return JsonResponse({"detail": "Only admins can add zones."}, status=403)
    fence, errors = _apply(Geofence(), _json_body(request), request.user)
    if errors:
        return JsonResponse({"ok": False, "errors": errors}, status=400)
    return JsonResponse({"ok": True, "fence": geofence.as_dict(fence)}, status=201)


@login_required
@staff_required
@require_POST
def api_geofence_update(request, pk):
    fence, errors = _apply(get_object_or_404(Geofence, pk=pk), _json_body(request), request.user)
    if errors:
        return JsonResponse({"ok": False, "errors": errors}, status=400)
    return JsonResponse({"ok": True, "fence": geofence.as_dict(fence)})


@login_required
@staff_required
@require_POST
def api_geofence_delete(request, pk):
    get_object_or_404(Geofence, pk=pk).delete()
    return JsonResponse({"ok": True})


@login_required
@require_GET
def api_geofence_events(request):
    try:
        after = int(request.GET.get("after") or 0)
    except ValueError:
        after = 0
    return JsonResponse({"events": _events(after)})


# ---------------------------------------------------------------------------
# Settings → WhatsApp alerts
# ---------------------------------------------------------------------------
SECRET = {"whatsapp_meta_token", "whatsapp_twilio_token", "whatsapp_callmebot_key", "whatsapp_meta_app_secret"}


def settings_context() -> dict:
    rows = {r["name"]: r for r in integrations.status() if r["name"] in integrations.WHATSAPP_FIELDS}
    for name, r in rows.items():
        r["value"] = "" if r["secret"] else integrations.get(name)
    import json
    from .models import WhatsAppMessage
    msgs = list(WhatsAppMessage.objects.all()[:40])
    for m in msgs:
        m.payload_json = json.dumps(m.payload, indent=2, ensure_ascii=False)
        m.response_json = json.dumps(m.response, indent=2, ensure_ascii=False) if m.response else ""
        m.meta_json = " · ".join(f"{k}: {v}" for k, v in (m.meta or {}).items())
        m.template = ((m.payload or {}).get("template") or {}).get("name", "") if isinstance(m.payload, dict) else ""
    return {"wa_fields": rows, "wa_providers": whatsapp.PROVIDERS, "wa_status": whatsapp.status(),
            "wa_verify_default": whatsapp.DEFAULT_VERIFY_TOKEN, "wa_messages": msgs,
            "wa_modes": whatsapp.META_MODES, "wa_mode": whatsapp.config()["meta_mode"],
            "wa_text_template": whatsapp.DEFAULT_TEXT_TEMPLATE, "wa_doc_template": whatsapp.DEFAULT_DOC_TEMPLATE}


@login_required
@staff_required
@require_POST
def settings_whatsapp(request):
    user = request.user.get_username()
    provider = request.POST.get("whatsapp_provider", "")
    if provider not in whatsapp.PROVIDERS:
        messages.error(request, "Unknown WhatsApp provider.")
        return redirect(reverse("settings") + "#whatsapp")
    recips = request.POST.get("whatsapp_recipients", "")
    _, errors = whatsapp.parse_recipients(recips)
    if len(recips) > 2000:
        errors.append("The recipient list is too long.")
    for e in errors:
        messages.error(request, e)
    if errors:
        return redirect(reverse("settings") + "#whatsapp")
    for name in integrations.WHATSAPP_FIELDS:
        value = (request.POST.get(name) or "").strip()
        if name in SECRET:
            if request.POST.get("clear_" + name):
                integrations.save(name, "", user)
            elif value:
                if len(value) > 1000 or any(c.isspace() for c in value):
                    messages.error(request, f"{integrations.FIELDS[name][0]}: that does not look like a valid value.")
                    continue
                integrations.save(name, value, user)
        elif name in request.POST:
            if name != "whatsapp_recipients" and (len(value) > 200 or any(c.isspace() for c in value)):
                messages.error(request, f"{integrations.FIELDS[name][0]}: that does not look like a valid value.")
                continue
            integrations.save(name, value, user)
    st = whatsapp.status()
    if not st["provider"]:
        messages.success(request, "WhatsApp settings saved. Alerts are off.")
    elif st["ready"]:
        messages.success(request, f"WhatsApp settings saved. Alerts go out through {st['label']} to "
                                  f"{st['recipients']} number(s). Use Send test message to check.")
    else:
        messages.warning(request, "WhatsApp settings saved, but still missing: " + ", ".join(st["missing"]) + ".")
    return redirect(reverse("settings") + "#whatsapp")


@login_required
@staff_required
@require_POST
def settings_whatsapp_test(request):
    now = timezone.localtime().strftime("%d %b %H:%M")
    text = (f"✅ Test message from {settings.FLEET.get('SITE_NAME', 'GPS Tracker')} at {now}. "
            "Geofence alerts will arrive here.")
    to = (request.POST.get("to") or "").strip()
    return JsonResponse(whatsapp.send(text, to or None))
