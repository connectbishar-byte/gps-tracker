"""
Geofences: zones on the map that raise an alert when a vehicle enters or leaves them.

* Geometry: a circle (centre + radius in metres) or a polygon ([[lat, lon], ...]).
* process_fixes() is called by the ingest pipeline after every committed frame with that frame's new GPS
  fixes, oldest first. It keeps one GeofenceState per zone and vehicle and logs a GeofenceEvent per crossing.
* Jitter: a vehicle counts as *inside* as soon as a fix is inside the zone, but only as *outside* again
  once a fix is at least FLEET_GEOFENCE_HYSTERESIS_M (default 30 m) beyond the edge, so GPS noise at the
  boundary does not produce a stream of enter / leave alerts.
* The first fix after a zone is created only records where the vehicle is (no alert), so drawing a zone
  around a parked vehicle does not alert.
* Alerts go out by WhatsApp in a background thread so the tracker's ACK is never delayed. A crossing older
  than FLEET_GEOFENCE_ALERT_MAX_AGE_MIN (default 30 min, e.g. replayed from the tracker's memory after a
  coverage gap) or within FLEET_GEOFENCE_ALERT_COOLDOWN_S (default 60 s) of the same alert (same zone, vehicle and
  direction) is logged but not sent.
"""
from __future__ import annotations

import logging
import math
import threading
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from .models import Device, Geofence, GeofenceEvent, GeofenceState

log = logging.getLogger("fleet.geofence")
EARTH_R = 6371008.8


def _cfg(name: str, default):
    return settings.FLEET.get(name, default)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def haversine_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def _xy(lat, lon, lat0, lon0):
    """Local flat projection in metres around (lat0, lon0); fine for zones up to a few hundred km."""
    return (math.radians(lon - lon0) * EARTH_R * math.cos(math.radians(lat0)), math.radians(lat - lat0) * EARTH_R)


def _seg_dist(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def locate(fence: Geofence, lat: float, lon: float) -> tuple[bool, float]:
    """(inside?, distance in metres from the point to the zone's edge)."""
    if fence.shape == "circle":
        d = haversine_m(fence.center_lat, fence.center_lon, lat, lon)
        return d <= fence.radius_m, abs(d - fence.radius_m)
    pts = [_xy(a, b, lat, lon) for a, b in fence.points]
    inside = False
    n = len(pts)
    for i in range(n):
        (x1, y1), (x2, y2) = pts[i], pts[i - 1]
        if (y1 > 0) != (y2 > 0) and 0 < (x2 - x1) * (0 - y1) / (y2 - y1) + x1:
            inside = not inside
    edge = min(_seg_dist(0, 0, *pts[i], *pts[i - 1]) for i in range(n)) if n else float("inf")
    return inside, edge


def validate(data: dict) -> tuple[dict, list[str]]:
    """Clean a zone posted from the page. Returns (fields, errors)."""
    errors, out = [], {}
    out["name"] = str(data.get("name") or "").strip()[:100]
    if not out["name"]:
        errors.append("Give the zone a name.")
    shape = data.get("shape")
    if shape not in ("circle", "polygon"):
        errors.append("Draw a circle or a polygon on the map.")
        return out, errors
    out["shape"] = shape
    try:
        if shape == "circle":
            lat, lon, r = float(data["center_lat"]), float(data["center_lon"]), float(data["radius_m"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError
            if not 10 <= r <= 500000:
                errors.append("The radius must be between 10 m and 500 km.")
            out.update(center_lat=lat, center_lon=lon, radius_m=round(r, 1), points=[])
        else:
            pts = [[round(float(a), 7), round(float(b), 7)] for a, b in data.get("points") or []]
            if len(pts) < 3:
                errors.append("A polygon needs at least 3 corners.")
            if len(pts) > 500:
                errors.append("A polygon can have at most 500 corners.")
            if any(not (-90 <= a <= 90 and -180 <= b <= 180) for a, b in pts):
                raise ValueError
            out.update(points=pts, center_lat=sum(p[0] for p in pts) / len(pts) if pts else None,
                       center_lon=sum(p[1] for p in pts) / len(pts) if pts else None, radius_m=None)
    except (KeyError, TypeError, ValueError):
        errors.append("The shape's coordinates are not valid. Draw it again.")
    color = str(data.get("color") or "#2563eb")
    out["color"] = color if len(color) in (4, 7) and color.startswith("#") else "#2563eb"
    for k in ("active", "alert_enter", "alert_exit"):
        out[k] = bool(data.get(k))
    out["recipients"] = str(data.get("recipients") or "").strip()[:2000]
    if out["recipients"]:
        from . import whatsapp
        _, rerr = whatsapp.parse_recipients(out["recipients"])
        errors += rerr
    out["notes"] = str(data.get("notes") or "")[:2000]
    try:
        out["device_ids"] = sorted({int(x) for x in data.get("device_ids") or []})
    except (TypeError, ValueError):
        errors.append("Unknown vehicle.")
    return out, errors


# ---------------------------------------------------------------------------
# Crossing detection (called from ingest)
# ---------------------------------------------------------------------------
def _fences_for(device_id: int) -> list[Geofence]:
    fences = Geofence.objects.filter(active=True).prefetch_related("devices")
    return [f for f in fences if not f.devices.all() or any(d.pk == device_id for d in f.devices.all())]


def process_fixes(device_id: int, fixes: list[tuple]) -> list[int]:
    """
    fixes: [(timestamp_utc, lat, lon, speed), ...] new valid fixes of one vehicle. Returns the ids of the
    GeofenceEvents created. Alerts are queued in a background thread. Never raises.
    """
    if not fixes:
        return []
    try:
        device = Device.objects.get(pk=device_id)
        if device.imei == settings.FLEET.get("SELFTEST_IMEI"):
            return []  # the connectivity self-test's simulated tracker
        fences = _fences_for(device_id)
        if not fences:
            return []
        fixes = sorted(fixes, key=lambda f: f[0])
        hyst = float(_cfg("GEOFENCE_HYSTERESIS_M", 30))
        created, pending = [], []
        with transaction.atomic():
            states = {s.geofence_id: s for s in GeofenceState.objects.select_for_update().filter(
                device_id=device_id, geofence__in=fences)}
            for fence in fences:
                st = states.get(fence.pk)
                for ts, lat, lon, speed in fixes:
                    if st and st.last_fix_utc and ts <= st.last_fix_utc:
                        continue  # older than what we already judged (late record)
                    inside, edge = locate(fence, lat, lon)
                    if st is None:  # first look at this vehicle for this zone: just remember where it is
                        st = GeofenceState.objects.create(geofence=fence, device_id=device_id, inside=inside,
                                                          since_utc=ts, last_fix_utc=ts)
                        continue
                    st.last_fix_utc = ts
                    kind = None
                    if not st.inside and inside:
                        kind = "enter"
                    elif st.inside and not inside and edge >= hyst:
                        kind = "exit"
                    if kind:
                        st.inside, st.since_utc = kind == "enter", ts
                        ev = _log_event(fence, device, st, kind, ts, lat, lon, speed)
                        created.append(ev.pk)
                        if ev.alert_status == "pending":
                            pending.append(ev.pk)
                    st.save()
            if pending:
                transaction.on_commit(lambda: _dispatch(pending))
        return created
    except Exception:  # pragma: no cover - geofencing must never break ingest
        log.exception("geofence check failed for device %s", device_id)
        return []


def _log_event(fence, device, st, kind, ts, lat, lon, speed) -> GeofenceEvent:
    wanted = fence.alert_enter if kind == "enter" else fence.alert_exit
    status, detail = ("pending", "") if wanted else ("off", "")
    now = timezone.now()
    if wanted:
        max_age = timedelta(minutes=float(_cfg("GEOFENCE_ALERT_MAX_AGE_MIN", 30)))
        cooldown = timedelta(seconds=float(_cfg("GEOFENCE_ALERT_COOLDOWN_S", 60)))
        if now - ts > max_age:
            status, detail = "skipped", f"Not sent: the crossing is older than {int(max_age.total_seconds() // 60)} min (data from the tracker's memory)."
        elif cooldown and GeofenceEvent.objects.filter(geofence=fence, device=device, kind=kind, created_utc__gte=now - cooldown,
                                                       alert_status__in=("pending", "sent")).exists():
            status, detail = "skipped", "Not sent: the same alert for this zone and vehicle went out moments ago."
        else:
            st.last_alert_utc = now
    return GeofenceEvent.objects.create(geofence=fence, geofence_name=fence.name, device=device, kind=kind,
                                        timestamp_utc=ts, latitude=lat, longitude=lon, speed=speed or 0,
                                        alert_status=status, alert_detail=detail)


def alert_text(ev: GeofenceEvent) -> str:
    tz = ZoneInfo(settings.TIME_ZONE)
    when = timezone.localtime(ev.timestamp_utc, tz).strftime("%d %b %H:%M")
    verb = "entered" if ev.kind == "enter" else "left"
    icon = "🟢" if ev.kind == "enter" else "🔴"
    site = settings.FLEET.get("SITE_NAME", "GPS Tracker")
    return (f"{icon} {ev.device.name} {verb} {ev.geofence_name} at {when}, {ev.speed} km/h. "
            f"Map: https://maps.google.com/?q={ev.latitude:.6f},{ev.longitude:.6f} ({site})")


def send_alert(event_id: int) -> None:
    ev = GeofenceEvent.objects.select_related("device", "geofence").filter(pk=event_id).first()
    if not ev:
        return
    from . import whatsapp
    res = whatsapp.send(alert_text(ev), ev.geofence.recipients if ev.geofence else None)
    GeofenceEvent.objects.filter(pk=ev.pk).update(alert_status="sent" if res.get("sent") else "failed",
                                                  alert_detail=res["detail"][:500])
    if not res.get("sent"):
        log.warning("geofence alert %s not delivered: %s", ev.pk, res["detail"])


def _dispatch(event_ids: list[int]) -> None:
    if _cfg("GEOFENCE_SYNC_ALERTS", False):  # tests
        for pk in event_ids:
            send_alert(pk)
        return

    def run():
        try:
            for pk in event_ids:
                send_alert(pk)
        finally:
            close_old_connections()

    threading.Thread(target=run, name="geofence-alert", daemon=True).start()


def as_dict(f: Geofence) -> dict:
    return {"id": f.pk, "name": f.name, "shape": f.shape, "center_lat": f.center_lat, "center_lon": f.center_lon,
            "radius_m": f.radius_m, "points": f.points, "color": f.color, "active": f.active,
            "alert_enter": f.alert_enter, "alert_exit": f.alert_exit, "recipients": f.recipients, "notes": f.notes,
            "device_ids": [d.pk for d in f.devices.all()]}


def event_dict(e: GeofenceEvent) -> dict:
    return {"id": e.pk, "geofence_id": e.geofence_id, "geofence": e.geofence_name, "device_id": e.device_id,
            "device": e.device.name, "kind": e.kind, "timestamp_utc": e.timestamp_utc.isoformat(),
            "latitude": e.latitude, "longitude": e.longitude, "speed": e.speed, "alert_status": e.alert_status,
            "alert_detail": e.alert_detail}
