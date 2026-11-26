"""
Vehicle telemetry for the graphical vehicle panel and the REST API: latest values of every parameter,
today's trip figures, short trends, and the history of any single parameter.

Every parameter of every record is stored in SQL Server: GPS fields in tracking_avlrecord and each
AVL I/O element in tracking_iovalue (one row per parameter per record, indexed by vehicle, parameter
and time).
"""
from __future__ import annotations

import math
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from . import io_dictionary as iod
from .models import AVLRecord, Device, IOValue

# Pseudo parameters taken from the GPS part of each record (not AVL I/O elements)
GPS_PARAMS = {
    "speed": ("GNSS speed", "km/h"),
    "altitude": ("Altitude", "m"),
    "satellites": ("Satellites", ""),
    "angle": ("Heading", "°"),
}
MAX_POINTS = 1500
TREND_IO = {"temp": 72, "ext_v": 66}


def _iso(dt):
    return dt.isoformat() if dt else None


def _downsample(points: list, n: int = MAX_POINTS) -> list:
    if len(points) <= n:
        return points
    step = len(points) / n
    out = [points[int(i * step)] for i in range(n)]
    out[-1] = points[-1]
    return out


def latest_record(d: Device):
    return AVLRecord.objects.filter(device=d).order_by("-timestamp_utc", "-id").first()


def _io_rows(record) -> list[dict]:
    rows = []
    for v in record.io.all():
        rows.append({"id": v.avl_id, "name": v.name or f"AVL {v.avl_id}", "unit": v.unit, "value": v.value,
                     "text": v.text, "raw": v.raw_hex[:64], "known": v.known})
    return rows


def _haversine_km(a, b) -> float:
    r = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(min(1, math.sqrt(h)))


def today_stats(d: Device, now=None) -> dict:
    """Distance and moving time since local midnight, from valid fixes (no odometer needed). Cached 60 s."""
    key = f"tele:today:{d.id}"
    hit = cache.get(key) if now is None else None
    if hit is not None:
        return hit
    res = _today_stats(d, now)
    if now is None:
        cache.set(key, res, 60)
    return res


def _today_stats(d: Device, now=None) -> dict:
    now = now or timezone.now()
    tz = ZoneInfo(settings.TIME_ZONE)
    midnight = datetime.combine(now.astimezone(tz).date(), time.min, tzinfo=tz)
    rows = list(AVLRecord.objects.filter(device=d, timestamp_utc__gte=midnight, valid_fix=True)
                .order_by("timestamp_utc").values_list("timestamp_utc", "latitude", "longitude", "speed")[:50000])
    dist = 0.0
    moving = 0.0
    for prev, cur in zip(rows, rows[1:]):
        step = _haversine_km(prev[1:3], cur[1:3])
        gap = (cur[0] - prev[0]).total_seconds()
        if step < 5 or (gap > 0 and step / (gap / 3600) < 250):   # ignore GPS jumps
            dist += step
        if prev[3] > 3 and 0 < gap <= 300:
            moving += gap
    return {"distance_km": round(dist, 2), "moving_s": int(moving), "records": len(rows)}


def trends(d: Device, minutes: int = 30, end=None) -> dict:
    """Speed, temperature and external voltage for the last `minutes` before `end` (default: last record)."""
    end = end or d.last_record_ts or timezone.now()
    start = end - timedelta(minutes=minutes)
    recs = list(AVLRecord.objects.filter(device=d, timestamp_utc__gte=start, timestamp_utc__lte=end)
                .order_by("timestamp_utc").values_list("id", "timestamp_utc", "speed"))
    io = {}
    for rid, avl_id, value in IOValue.objects.filter(device=d, avl_id__in=TREND_IO.values(),
                                                     timestamp_utc__gte=start, timestamp_utc__lte=end) \
            .values_list("record_id", "avl_id", "value"):
        io[(rid, avl_id)] = value
    pts = [{"ts": _iso(ts), "speed": sp, "temp": io.get((rid, 72)), "ext_v": io.get((rid, 66))}
           for rid, ts, sp in recs]
    return {"from": _iso(start), "to": _iso(end), "points": _downsample(pts)}


def vehicle(d: Device, minutes: int = 30) -> dict:
    from .views import device_status  # avoid an import cycle
    rec = latest_record(d)
    latest = None
    odometer = None
    if rec:
        rows = _io_rows(rec)
        latest = {"id": rec.id, "ts": _iso(rec.timestamp_utc), "received": _iso(rec.received_utc),
                  "lat": rec.latitude, "lon": rec.longitude, "alt": rec.altitude, "angle": rec.angle,
                  "sats": rec.satellites, "speed": rec.speed, "priority": rec.priority, "event_io": rec.event_io_id,
                  "valid_fix": rec.valid_fix, "io": rows}
        odo = next((r["value"] for r in rows if r["id"] == 16 and r["value"] is not None), None)
        if odo is None:  # the latest record may not carry the odometer; use the most recent that did
            v = IOValue.objects.filter(device=d, avl_id=16).order_by("-timestamp_utc").values_list("value", flat=True).first()
            odo = v
        odometer = odo
    return {
        "device": device_status(d), "retention_days": d.retention_days,
        "latest": latest, "odometer_km": odometer, "today": today_stats(d),
        "trends": trends(d, minutes) if rec else {"points": []},
        "parameters": parameter_catalogue(d),
    }


def parameter_catalogue(d: Device) -> list[dict]:
    """The parameters this vehicle reports (from its last 300 records), for the chart picker. Cached 5 min."""
    key = f"tele:params:{d.id}"
    ids = cache.get(key)
    if ids is None:
        recent = list(AVLRecord.objects.filter(device=d).order_by("-timestamp_utc").values_list("id", flat=True)[:300])
        ids = sorted(set(IOValue.objects.filter(record_id__in=recent).values_list("avl_id", flat=True)))
        cache.set(key, ids, 300)
    out = [{"key": k, "name": n, "unit": u, "source": "gps"} for k, (n, u) in GPS_PARAMS.items()]
    for i in ids:
        meta = iod.IO_MAP.get(i)
        out.append({"key": str(i), "id": i, "name": meta[0] if meta else f"AVL {i}", "unit": meta[3] if meta else "",
                    "source": "io", "labels": (meta[4] if meta and meta[4] else None)})
    return out


def parameter_history(d: Device, key: str, hours: float = 24, end=None) -> dict:
    """[ts, value] points for one parameter: a GPS field name or an AVL I/O id."""
    end = end or d.last_record_ts or timezone.now()
    start = end - timedelta(hours=hours)
    if key in GPS_PARAMS:
        name, unit = GPS_PARAMS[key]
        rows = AVLRecord.objects.filter(device=d, timestamp_utc__gte=start, timestamp_utc__lte=end) \
            .order_by("timestamp_utc").values_list("timestamp_utc", key)
    else:
        avl_id = int(key)
        meta = iod.IO_MAP.get(avl_id)
        name, unit = (meta[0], meta[3]) if meta else (f"AVL {avl_id}", "")
        rows = IOValue.objects.filter(device=d, avl_id=avl_id, timestamp_utc__gte=start, timestamp_utc__lte=end) \
            .order_by("timestamp_utc").values_list("timestamp_utc", "value")
    pts = [[_iso(ts), v] for ts, v in rows if v is not None]
    vals = [p[1] for p in pts]
    return {"key": key, "name": name, "unit": unit, "from": _iso(start), "to": _iso(end),
            "count": len(pts), "min": min(vals) if vals else None, "max": max(vals) if vals else None,
            "avg": (sum(vals) / len(vals)) if vals else None, "points": _downsample(pts)}
