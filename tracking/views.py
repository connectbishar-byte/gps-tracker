from __future__ import annotations

import csv
import logging
import math
from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.cache import cache
from django.urls import reverse, reverse_lazy
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST

import json

from . import assistant, codec, driverscore, integrations, io_dictionary
from .models import AVLRecord, Connection, Device, RawPacket, ReceiverHeartbeat, TestRun, mask_imei


def _asset_version():
    """Changes whenever our own JS/CSS changes, so browsers and Cloudflare fetch the new copy after a deploy."""
    import os
    folder = os.path.join(os.path.dirname(__file__), "static", "tracking")
    try:
        return str(int(max(os.path.getmtime(os.path.join(folder, f)) for f in os.listdir(folder))))
    except (OSError, ValueError):
        return "1"


ASSET_V = _asset_version()
log = logging.getLogger("fleet.web")


def fleet_context(request):
    ctx = {"FLEET": settings.FLEET, "FLEET_TZ": settings.TIME_ZONE, "can_view_raw": can_view_raw(request.user),
           "GOOGLE_MAPS_KEY": "", "ASSET_V": ASSET_V}
    if request.user.is_authenticated:  # the key is only handed to signed-in users
        try:
            ctx["GOOGLE_MAPS_KEY"] = integrations.get("google_maps_api_key")
        except Exception:  # noqa: BLE001 - a settings problem must never break page rendering
            pass
        try:
            acfg = assistant.get_config()
            if assistant.can_use(request.user, acfg):
                ctx["ASSISTANT"] = {"name": acfg["name"], "position": acfg["position"], "proactive": acfg["proactive"]}
        except Exception:  # noqa: BLE001
            log.exception("assistant config unreadable")
    return ctx


def can_view_raw(user) -> bool:
    return bool(user and user.is_authenticated and
                (user.is_superuser or user.is_staff or user.groups.filter(name="raw_data").exists()))


raw_required = user_passes_test(can_view_raw)


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------
def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    """Initial compass bearing from point 1 to point 2: 0 = north, 90 = east (clockwise)."""
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def distance_m(lat1, lon1, lat2, lon2) -> float:
    k = 111_320.0
    return math.hypot((lat2 - lat1) * k, (lon2 - lon1) * k * math.cos(math.radians((lat1 + lat2) / 2)))


def travel_course(d: Device, min_move_m: float = 15.0):
    """Bearing from an earlier valid fix to the latest one, or None when the vehicle hasn't moved far enough."""
    if d.last_fix_ts is None or d.last_fix_lat is None:
        return None
    earlier = (d.records.filter(valid_fix=True, timestamp_utc__lt=d.last_fix_ts,
                                timestamp_utc__gte=d.last_fix_ts - timedelta(minutes=5))
               .order_by("-timestamp_utc").values_list("latitude", "longitude")[:20])
    for lat, lon in earlier:
        if distance_m(lat, lon, d.last_fix_lat, d.last_fix_lon) >= min_move_m:
            return round(bearing_deg(lat, lon, d.last_fix_lat, d.last_fix_lon))
    return None


def device_status(d: Device, now=None) -> dict:
    now = now or timezone.now()
    cfg = settings.FLEET
    last = d.last_packet_utc
    if not d.enabled:
        link = "disabled"
    elif last is None:
        link = "never"
    elif (now - last).total_seconds() <= cfg["STALE_AFTER_SECONDS"]:
        link = "online"
    elif (now - last).total_seconds() <= cfg["OFFLINE_AFTER_SECONDS"]:
        link = "stale"
    else:
        link = "offline"

    if link in ("never", "offline", "disabled"):
        state = "nosignal"
    elif d.last_movement and (d.last_fix_speed or 0) > 3:
        state = "moving"
    elif d.last_ignition:
        state = "idling"
    else:
        state = "parked"
    fix_age = (now - d.last_fix_ts).total_seconds() if d.last_fix_ts else None
    course = travel_course(d) if state == "moving" else None
    return {
        "id": d.id, "name": d.name, "plate": d.plate, "group": d.group or "Default", "model": d.model,
        "imei": d.masked_imei, "enabled": d.enabled, "link": link, "state": state,
        "last_packet": d.last_packet_utc.isoformat() if d.last_packet_utc else None,
        "last_record": d.last_record_ts.isoformat() if d.last_record_ts else None,
        "fix": None if d.last_fix_ts is None else {
            "ts": d.last_fix_ts.isoformat(), "lat": d.last_fix_lat, "lon": d.last_fix_lon,
            "speed": d.last_fix_speed, "angle": d.last_fix_angle, "sats": d.last_fix_sats,
            "age_s": int(fix_age),
            # Direction of travel worked out from the last fixes; the map arrow uses it, so the arrow points
            # where the vehicle actually went even when the tracker's own heading field is off.
            "course": course if course is not None else d.last_fix_angle,
        },
        "ignition": d.last_ignition, "movement": d.last_movement, "gsm": d.last_gsm_signal,
        "ext_v": d.last_ext_voltage, "packets": d.packet_count, "records": d.record_count,
        "errors": d.error_count, "last_error": d.last_error,
    }


def receiver_status():
    hb = ReceiverHeartbeat.objects.order_by("-last_beat_utc").first()
    if not hb:
        return {"up": False, "detail": "No heartbeat yet - is run_receiver started?"}
    age = (timezone.now() - hb.last_beat_utc).total_seconds()
    return {"up": age < 20, "age_s": int(age), "host": hb.host, "port": hb.port, "pid": hb.pid,
            "started": hb.started_utc.isoformat(), "active": hb.active_connections,
            "total": hb.total_connections, "frames_ok": hb.frames_ok, "frames_bad": hb.frames_bad,
            "detail": "listening" if age < 20 else f"last heartbeat {int(age)} s ago"}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------
def _client_ip(request) -> str:
    if settings.FLEET_TRUST_X_FORWARDED_FOR:
        real = request.META.get("HTTP_X_REAL_IP", "").strip()  # set by our Nginx site, not spoofable through it
        if real:
            return real
    return request.META.get("REMOTE_ADDR", "")


class ThrottledLoginView(auth_views.LoginView):
    """Username + password sign-in with a lockout after repeated failures (per username and per IP)."""
    template_name = "tracking/login.html"
    redirect_authenticated_user = True

    def _keys(self):
        user = (self.request.POST.get("username") or "").strip().lower()[:150]
        ip = _client_ip(self.request)
        return [f"login-fail:user:{user}", f"login-fail:ip:{ip}"]

    def post(self, request, *args, **kwargs):
        limit = settings.LOGIN_MAX_FAILURES
        if any((cache.get(k) or 0) >= limit * (3 if ":ip:" in k else 1) for k in self._keys()):
            form = self.get_form()
            form.errors.clear()
            return self.render_to_response(self.get_context_data(
                form=form, locked=True, lockout_minutes=settings.LOGIN_LOCKOUT_MINUTES))
        return super().post(request, *args, **kwargs)

    def form_invalid(self, form):
        ttl = settings.LOGIN_LOCKOUT_MINUTES * 60
        for k in self._keys():
            try:
                cache.add(k, 0, ttl)
                cache.incr(k)
            except ValueError:
                cache.set(k, 1, ttl)
        return super().form_invalid(form)

    def form_valid(self, form):
        cache.delete_many(self._keys()[:1])
        return super().form_valid(form)


class PasswordChange(auth_views.PasswordChangeView):
    template_name = "tracking/password_change.html"
    success_url = reverse_lazy("dashboard")

    def get_context_data(self, **kwargs):
        return super().get_context_data(**kwargs) | {"nav": "account"}

    def form_valid(self, form):
        messages.success(self.request, "Password changed.")
        return super().form_valid(form)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@login_required
def dashboard(request):
    now = timezone.now()
    devices = [device_status(d, now) for d in Device.objects.all()]
    counts = {k: sum(1 for d in devices if d["state"] == k) for k in ("moving", "idling", "parked", "nosignal")}
    links = {k: sum(1 for d in devices if d["link"] == k) for k in ("online", "stale", "offline", "never")}
    since = now - timedelta(hours=24)
    stats = {
        "packets_24h": RawPacket.objects.filter(received_utc__gte=since).count(),
        "errors_24h": RawPacket.objects.filter(received_utc__gte=since, parse_status="error").count(),
        "records_24h": AVLRecord.objects.filter(received_utc__gte=since).count(),
        "connections_24h": Connection.objects.filter(opened_utc__gte=since).count(),
        "rejected_24h": Connection.objects.filter(opened_utc__gte=since, handshake="rejected").count(),
    }
    return render(request, "tracking/dashboard.html", {
        "nav": "dashboard", "devices": devices, "counts": counts, "links": links, "stats": stats,
        "receiver": receiver_status(), "total": len(devices),
    })


@login_required
@raw_required
def terminal(request):
    cfg = settings.FLEET
    return render(request, "tracking/terminal.html", {
        "nav": "terminal", "receiver": receiver_status(),
        "public_endpoint": f"{cfg['PUBLIC_GPS_HOST']}:{cfg['PUBLIC_GPS_PORT']}",
        "bind": f"{cfg['RECEIVER_BIND_HOST']}:{cfg['RECEIVER_PORT']}",
        "poll_ms": cfg["TERMINAL_POLL_MS"], "port_log": cfg["PORT_LOG"],
        "retention_h": cfg["PORT_LOG_RETENTION_HOURS"], "auto_register": cfg["AUTO_REGISTER_UNKNOWN"],
    })


@login_required
def live(request):
    return render(request, "tracking/live.html", {"nav": "live"})


@login_required
def history(request):
    devices = Device.objects.all()
    return render(request, "tracking/history.html", {"nav": "history", "devices": devices})


@login_required
def records(request):
    qs = AVLRecord.objects.select_related("device").annotate(io_count=Count("io")).order_by("-timestamp_utc", "-id")
    dev = request.GET.get("device")
    if dev:
        qs = qs.filter(device_id=dev)
    if request.GET.get("fix") == "valid":
        qs = qs.filter(valid_fix=True)
    elif request.GET.get("fix") == "invalid":
        qs = qs.filter(valid_fix=False)
    if request.GET.get("late"):
        qs = qs.filter(late=True)
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "tracking/records.html", {"nav": "records", "page": page,
                                                     "devices": Device.objects.all(), "q": request.GET})


@login_required
def record_detail(request, pk):
    rec = get_object_or_404(AVLRecord.objects.select_related("device", "packet"), pk=pk)
    return render(request, "tracking/io_inspector.html", {
        "nav": "records", "rec": rec, "io": rec.io.all(),
        "mapping_version": io_dictionary.MAPPING_VERSION,
    })


@login_required
def io_inspector(request):
    """Latest record per device with all I/O, or a specific record."""
    rec = AVLRecord.objects.select_related("device", "packet")
    dev = request.GET.get("device")
    if dev:
        rec = rec.filter(device_id=dev)
    rec = rec.order_by("-received_utc", "-id").first()
    if rec:
        return redirect("record_detail", pk=rec.pk)
    messages.info(request, "No decoded records yet. Send data with the simulator or a device first.")
    return redirect("records")


@login_required
@raw_required
def raw_packets(request):
    qs = RawPacket.objects.select_related("device", "connection")
    q = request.GET
    if q.get("device"):
        qs = qs.filter(device_id=q["device"])
    if q.get("status") == "error":
        qs = qs.filter(parse_status="error")
    elif q.get("status") == "ok":
        qs = qs.filter(parse_status="ok")
    if q.get("codec"):
        qs = qs.filter(codec=q["codec"])
    if q.get("search"):
        qs = qs.filter(Q(hex__icontains=q["search"]) | Q(error__icontains=q["search"]))
    if q.get("export") == "csv":
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="raw_packets.csv"'
        w = csv.writer(resp)
        w.writerow(["received_utc", "imei_masked", "source", "codec", "size", "crc_ok", "records", "new",
                    "duplicates", "parse_status", "error", "ack_status", "ack_value", "hex"])
        for p in qs[:5000]:
            w.writerow([p.received_utc.isoformat(), p.device.masked_imei if p.device else "",
                        p.connection.source if p.connection else "", p.codec, p.size, p.crc_ok, p.record_count,
                        p.new_records, p.duplicate_records, p.parse_status, p.error, p.ack_status, p.ack_value, p.hex])
        return resp
    page = Paginator(qs, 50).get_page(q.get("page"))
    return render(request, "tracking/raw.html", {"nav": "raw", "page": page, "devices": Device.objects.all(), "q": q})


@login_required
@raw_required
def raw_detail(request, pk):
    p = get_object_or_404(RawPacket, pk=pk)
    decoded, err = None, ""
    if not p.truncated:
        try:
            decoded = codec.decode_frame(bytes.fromhex(p.hex))
        except Exception as exc:  # noqa: BLE001
            err = str(exc)
    hexs = p.hex
    parts = []
    if len(hexs) >= 24:
        parts = [("Preamble", hexs[:8]), ("Data length", hexs[8:16]), ("Codec", hexs[16:18]),
                 ("Record count", hexs[18:20]), ("Records … count 2", hexs[20:-8]), ("CRC", hexs[-8:])]
    return render(request, "tracking/raw_detail.html", {"nav": "raw", "p": p, "decoded": decoded,
                                                        "decode_error": err, "parts": parts,
                                                        "records": p.records.all()})


class DeviceForm(forms.ModelForm):
    class Meta:
        model = Device
        fields = ["imei", "name", "plate", "group", "model", "firmware", "config_revision", "enabled", "retention_days", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def clean_imei(self):
        imei = self.cleaned_data["imei"].strip()
        if not imei.isdigit() or not 14 <= len(imei) <= 17:
            raise forms.ValidationError("IMEI must be 14-17 digits (normally 15).")
        return imei


@login_required
def devices(request):
    form = DeviceForm(request.POST or None)
    if request.method == "POST":
        if not request.user.is_staff:
            messages.error(request, "Only staff users can register devices.")
        elif form.is_valid():
            d = form.save()
            messages.success(request, f"Device {d.name} registered. The receiver will now accept IMEI {d.masked_imei}.")
            return redirect("devices")
    now = timezone.now()
    rows = [device_status(d, now) | {"obj": d} for d in Device.objects.all()]
    return render(request, "tracking/devices.html", {"nav": "devices", "rows": rows, "form": form})


@login_required
def device_detail(request, pk):
    d = get_object_or_404(Device, pk=pk)
    if request.method == "POST":
        if not request.user.is_staff:
            messages.error(request, "Only staff users can edit devices.")
            return redirect("device_detail", pk=pk)
        form = DeviceForm(request.POST, instance=d)
        if form.is_valid():
            form.save()
            messages.success(request, "Device updated.")
            return redirect("device_detail", pk=pk)
    else:
        form = DeviceForm(instance=d)
    return render(request, "tracking/device_detail.html", {
        "nav": "devices", "d": d, "s": device_status(d), "form": form,
        "connections": d.connections.all()[:25],
        "errors": d.packets.filter(parse_status="error")[:15],
        "full_imei": request.user.is_staff,
    })


@login_required
def connectivity(request):
    cfg = settings.FLEET
    rejected = Connection.objects.filter(handshake__in=["rejected", "invalid"])[:20]
    return render(request, "tracking/connectivity.html", {
        "nav": "connectivity", "receiver": receiver_status(), "runs": TestRun.objects.all()[:10],
        "recent": Connection.objects.select_related("device")[:20], "rejected": rejected,
        "selftest_imei_registered": Device.objects.filter(imei=cfg["SELFTEST_IMEI"], enabled=True).exists(),
        "selftest_imei_masked": mask_imei(cfg["SELFTEST_IMEI"]),
    })


@login_required
@require_POST
def run_selftest(request):
    if not request.user.is_staff:
        messages.error(request, "Only staff users can run the connectivity self-test.")
        return redirect("connectivity")
    from .simulator import run_connectivity_suite
    cfg = settings.FLEET
    Device.objects.get_or_create(imei=cfg["SELFTEST_IMEI"],
                                 defaults={"name": "Self-test simulator", "model": "SIMULATOR", "group": "Test"})
    host = request.POST.get("host") or cfg["SELFTEST_HOST"]
    port = int(request.POST.get("port") or cfg["SELFTEST_PORT"])
    full = request.POST.get("mode", "full") == "full"
    steps = run_connectivity_suite(host, port, cfg["SELFTEST_IMEI"], full=full, timeout=8)
    ok = all(s["ok"] for s in steps)
    passed = sum(s["ok"] for s in steps)
    run = TestRun.objects.create(target=f"{host}:{port}", passed=ok, steps=steps,
                                 summary=f"{passed}/{len(steps)} steps passed")
    (messages.success if ok else messages.error)(request, f"Self-test against {host}:{port}: {run.summary}")
    return redirect("connectivity")


@login_required
def settings_page(request):
    from django.db import connection as dbc
    return render(request, "tracking/settings.html", {
        "nav": "settings", "db_vendor": dbc.vendor, "db_engine": settings.DB_ENGINE,
        "mapping_version": io_dictionary.MAPPING_VERSION,
        "io_map": sorted((k, v[0], v[3]) for k, v in io_dictionary.IO_MAP.items()),
        "tz": settings.TIME_ZONE,
        "integrations": integrations.status() if request.user.is_staff else None,
        "assistant": _assistant_settings() if request.user.is_staff else None,
        "last_purge": _last_purge(),
        **(driverscore.settings_context() if request.user.is_staff else {}),
    })


def _assistant_settings():
    return {"cfg": assistant.get_config(), "ai": assistant.ai_status(), "positions": assistant.POSITIONS,
            "providers": assistant.PROVIDERS, "audiences": assistant.AUDIENCES, "details": assistant.DETAIL,
            "limits": assistant.LIMITS}


def _last_purge():
    lp = cache.get("purge:last")
    if lp:
        lp = {**lp, "at": parse_datetime(lp["at"])}
    return lp


staff_required = user_passes_test(lambda u: u.is_active and u.is_staff)


@login_required
@staff_required
@require_POST
def settings_integrations(request):
    """Save integration settings. A blank field keeps the saved value; the Remove box clears it."""
    changed = []
    for name, (label, _env, _secret) in integrations.FIELDS.items():
        value = (request.POST.get(name) or "").strip()
        if request.POST.get("clear_" + name):
            integrations.save(name, "", request.user.get_username())
            changed.append(f"{label} removed")
        elif value:
            if len(value) > 500 or any(c.isspace() for c in value):
                messages.error(request, f"{label}: that does not look like a valid value.")
                continue
            integrations.save(name, value, request.user.get_username())
            changed.append(f"{label} saved")
    messages.success(request, "; ".join(changed) + "." if changed else "Nothing changed.")
    return redirect(reverse("settings") + "#integrations")


@login_required
@staff_required
@require_POST
def settings_test_openai(request):
    key = (request.POST.get("openai_api_key") or "").strip() or integrations.get("openai_api_key")
    model = (request.POST.get("openai_model") or "").strip() or integrations.get("openai_model")
    if not key:
        return JsonResponse({"ok": False, "detail": "Enter an OpenAI API key first."})
    return JsonResponse(integrations.test_openai(key, model))


@login_required
@staff_required
@require_POST
def settings_test_anthropic(request):
    key = (request.POST.get("anthropic_api_key") or "").strip() or integrations.get("anthropic_api_key")
    model = (request.POST.get("anthropic_model") or "").strip() or integrations.get("anthropic_model")
    if not key:
        return JsonResponse({"ok": False, "detail": "Enter an Anthropic API key first."})
    return JsonResponse(integrations.test_anthropic(key, model))


@login_required
@staff_required
@require_POST
def settings_assistant(request):
    cfg, errors = assistant.clean_config(request.POST)
    for e in errors:
        messages.error(request, e)
    if not errors:
        assistant.save_config(cfg, request.user.get_username())
        messages.success(request, f"AI assistant settings saved{'' if cfg['enabled'] else ' (assistant turned off)'}.")
    return redirect(reverse("settings") + "#assistant")


# ---------------------------------------------------------------------------
# AI assistant (the robot on every page)
# ---------------------------------------------------------------------------
def _assistant_allowed(request):
    return assistant.can_use(request.user)


@login_required
@require_POST
def api_assistant_context(request):
    """Greeting, page tips and the health check for the page the user is on."""
    if not _assistant_allowed(request):
        return JsonResponse({"error": "The assistant is turned off."}, status=404)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        body = {}
    nav = str(body.get("page") or "")[:30]
    cfg = assistant.get_config()
    user = request.user
    who = user.first_name or user.get_username()
    greeting = cfg["greeting"].replace("{user}", who).replace("{name}", cfg["name"])
    return JsonResponse({
        "name": cfg["name"], "greeting": greeting, "ai": assistant.ai_status(cfg)["label"],
        "ai_ready": assistant.ai_status(cfg)["ready"], "page": assistant.page_info(nav),
        "findings": assistant.diagnostics(user, body.get("client") if isinstance(body.get("client"), dict) else None),
        "can_configure": user.is_staff,
    })


@login_required
@require_POST
def api_assistant_chat(request):
    if not _assistant_allowed(request):
        return JsonResponse({"error": "The assistant is turned off."}, status=404)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "Bad request."}, status=400)
    question = str(body.get("message") or "").strip()[:2000]
    if not question:
        return JsonResponse({"error": "Type a question first."}, status=400)
    if assistant.rate_limited(request.user):
        return JsonResponse({"reply": "You've asked a lot of questions in a short time. Give me a few minutes, please.",
                             "source": "limit"}, status=429)
    history = body.get("history") if isinstance(body.get("history"), list) else []
    client = body.get("client") if isinstance(body.get("client"), dict) else None
    res = assistant.answer(request.user, question, history, str(body.get("page") or "")[:30],
                           str(body.get("page_text") or "")[:1500], client)
    return JsonResponse(res)


# ---------------------------------------------------------------------------
# JSON API (polled every 5 s by the UI)
# ---------------------------------------------------------------------------
@login_required
def api_fleet(request):
    now = timezone.now()
    data = [device_status(d, now) for d in Device.objects.all()]
    return JsonResponse({"server_time": now.isoformat(), "devices": data, "receiver": receiver_status()})


@login_required
def api_vehicle(request, pk):
    """Graphical vehicle panel data (same as /api/v1/devices/<id>/telemetry/, for the session-based UI)."""
    from . import telemetry
    d = get_object_or_404(Device, pk=pk)
    try:
        minutes = max(5, min(1440, int(request.GET.get("minutes") or 30)))
    except ValueError:
        minutes = 30
    return JsonResponse(telemetry.vehicle(d, minutes))


@login_required
def api_vehicle_param(request, pk, key):
    from . import telemetry
    d = get_object_or_404(Device, pk=pk)
    if not (key in telemetry.GPS_PARAMS or key.isdigit()):
        return JsonResponse({"error": "Unknown parameter"}, status=400)
    try:
        hours = max(0.1, min(2160.0, float(request.GET.get("hours") or 24)))
    except ValueError:
        hours = 24
    return JsonResponse(telemetry.parameter_history(d, key, hours))


@login_required
def api_history(request):
    dev = get_object_or_404(Device, pk=request.GET.get("device"))
    now = timezone.now()
    start = parse_datetime(request.GET.get("from") or "") or now - timedelta(hours=24)
    end = parse_datetime(request.GET.get("to") or "") or now
    if timezone.is_naive(start):
        start = timezone.make_aware(start)
    if timezone.is_naive(end):
        end = timezone.make_aware(end)
    qs = dev.records.filter(timestamp_utc__gte=start, timestamp_utc__lte=end).order_by("timestamp_utc", "id")[:20000]
    pts = [{
        "id": r.id, "ts": r.timestamp_utc.isoformat(), "rx": r.received_utc.isoformat(),
        "lat": r.latitude, "lon": r.longitude, "speed": r.speed, "angle": r.angle, "alt": r.altitude,
        "sats": r.satellites, "valid": r.valid_fix, "late": r.late, "event": r.event_io_id,
    } for r in qs]
    valid = [p for p in pts if p["valid"]]
    return JsonResponse({"device": dev.name, "from": start.isoformat(), "to": end.isoformat(),
                         "count": len(pts), "valid": len(valid), "late": sum(p["late"] for p in pts),
                         "max_speed": max((p["speed"] for p in valid), default=0), "points": pts})


@login_required
def api_receiver(request):
    return JsonResponse(receiver_status())
