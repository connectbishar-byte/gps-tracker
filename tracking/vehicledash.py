"""
Vehicle dashboard: one page per vehicle with its daily distance, driver performance and activity, as charts.

Pages
* /vehicles/<id>/                       the dashboard (?from=YYYY-MM-DD&to=YYYY-MM-DD, default the last 7 days)
* /api/v1/vehicles/<id>/dashboard/      the same numbers as JSON

Everything comes from the same engine as the Driver score sheet (driverscore.vehicle_trips), so the scores and
distances on both pages always agree:
* Daily distance, driving and idling time, trips and the daily score are the trips that started that day.
* Speed profile and the activity heatmap are measured from every record (time spent at each speed / in each hour).
* Drivers are the iButton IDs the tracker sent (AVL 78) and the Car view drivers who saved drives with this vehicle.
"""
from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from rest_framework.decorators import api_view
from rest_framework.response import Response

from . import driverscore as ds
from .models import AVLRecord, Device, SavedPlace

SPEED_BINS = [(0, 20), (20, 40), (40, 60), (60, 80), (80, 100), (100, 120), (120, None)]
HEATMAP_MAX_DAYS = 31


def _r(x, n=1):
    return None if x is None else round(x, n)


def _day_row(day, m, cfg):
    res = ds.score(m, cfg)
    return {
        "date": day.isoformat(), "km": _r(m.km, 2), "trips": m.trips, "driving_s": round(m.moving_s),
        "engine_s": round(m.duration_s), "idle_s": None if m.idle_s is None else round(m.idle_s),
        "overspeed_s": round(m.overspeed_s), "max_kmh": round(m.max_kmh), "avg_kmh": round(m.avg_kmh),
        "harsh": m.harsh_accel + m.harsh_brake + m.harsh_turn, "crashes": m.crashes,
        "score": res["score"], "rating": res["rating"],
    }


def _profile(device, start, end, days):
    """Time spent in each speed band and driving minutes per day and hour, from the raw records."""
    bins = [0.0] * len(SPEED_BINS)
    hours = [0.0] * 24
    grid = {d: [0.0] * 24 for d in days}
    prev = None
    qs = (AVLRecord.objects.filter(device=device, timestamp_utc__gte=start, timestamp_utc__lt=end)
          .order_by("timestamp_utc", "id").values_list("timestamp_utc", "speed"))
    for t, spd in qs.iterator(chunk_size=5000):
        if prev is not None:
            pt, pspd = prev
            dt = (t - pt).total_seconds()
            if 0 < dt <= ds.MAX_GAP_S and (spd > ds.STOP_KMH or pspd > ds.STOP_KMH):
                v = (spd + pspd) / 2
                for i, (lo, hi) in enumerate(SPEED_BINS):
                    if hi is None or v < hi:
                        bins[i] += dt
                        break
                lt = timezone.localtime(t)
                hours[lt.hour] += dt
                if lt.date() in grid:
                    grid[lt.date()][lt.hour] += dt
        prev = (t, spd)
    total = sum(bins)
    speed = [{"label": f"{lo}–{hi}" if hi else f"{lo}+", "seconds": round(s),
              "pct": round(100 * s / total, 1) if total else 0.0} for (lo, hi), s in zip(SPEED_BINS, bins)]
    return speed, [round(h / 60, 1) for h in hours], {d.isoformat(): [round(x / 60, 1) for x in v] for d, v in grid.items()}


def _phone_drivers(device, start, end, cfg):
    """Car view drivers who saved drives with this vehicle, with their score on those drives."""
    per = {}
    qs = (SavedPlace.objects.filter(device=device, kind="trip", created_utc__gte=start, created_utc__lt=end)
          .select_related("owner"))
    for p in qs:
        dur = float(p.duration_s or 0)
        m = ds.Metrics(km=float(p.distance_km or 0), duration_s=dur, moving_s=float(p.moving_s or dur), idle_s=None,
                       overspeed_s=float(p.overspeed_s or 0), max_kmh=float(p.max_speed_kmh or 0),
                       harsh_accel=p.harsh_accel, harsh_brake=p.harsh_brake, harsh_turn=p.harsh_turn, trips=1)
        name = (p.owner.get_full_name() or p.owner.get_username()) if p.owner else "Unknown driver"
        key = p.owner_id or 0
        if key not in per:
            per[key] = {"id": p.owner_id, "name": name, "m": ds.Metrics(idle_s=None)}
        per[key]["m"].add(m)
    out = []
    for v in per.values():
        r = ds.score(v["m"], cfg)
        out.append({"id": v["id"], "name": v["name"], "source": "car view", "trips": v["m"].trips,
                    "km": _r(v["m"].km, 1), "driving_s": round(v["m"].moving_s), "score": r["score"],
                    "rating": r["rating"]})
    return out


def build(device: Device, d_from, d_to, start, end, cfg=None) -> dict:
    cfg = cfg or ds.get_config()
    trips, notes = ds.vehicle_trips(device, start, end, cfg)
    n_days = (d_to - d_from).days + 1
    days = [d_from + timedelta(days=i) for i in range(n_days)]
    idle0 = 0.0 if notes["ignition"] else None
    per_day = {d: ds.Metrics(idle_s=idle0) for d in days}
    total = ds.Metrics(idle_s=idle0)
    drivers = {}
    for tr in trips:
        tr.result = ds.score(tr.m, cfg)
        day = timezone.localtime(tr.start).date()
        if day in per_day:
            per_day[day].add(tr.m)
        total.add(tr.m)
        for did in tr.driver_ids or []:
            drivers.setdefault(did, ds.Metrics(idle_s=idle0)).add(tr.m)
    res = ds.score(total, cfg)
    daily = [_day_row(d, per_day[d], cfg) for d in days]
    speed, hours, grid = _profile(device, start, end, days)
    active = [r for r in daily if r["km"] >= 0.5]
    best = max(active, key=lambda r: r["km"]) if active else None

    driver_rows = []
    for did, m in sorted(drivers.items()):
        r = ds.score(m, cfg)
        driver_rows.append({"id": did, "name": f"iButton {did}", "source": "tracker", "trips": m.trips,
                            "km": _r(m.km, 1), "driving_s": round(m.moving_s), "score": r["score"], "rating": r["rating"]})
    driver_rows += _phone_drivers(device, start, end, cfg)
    driver_rows.sort(key=lambda r: -(r["km"] or 0))

    return {
        "vehicle": {"id": device.pk, "name": device.name, "plate": device.plate, "group": device.group,
                    "imei": device.masked_imei},
        "from": d_from.isoformat(), "to": d_to.isoformat(), "days": n_days,
        "totals": {
            "km": _r(total.km, 1), "trips": total.trips, "driving_s": round(total.moving_s),
            "engine_s": round(total.duration_s), "idle_s": None if total.idle_s is None else round(total.idle_s),
            "max_kmh": round(total.max_kmh), "avg_kmh": round(total.avg_kmh),
            "avg_km_day": _r(total.km / n_days, 1), "active_days": len(active),
            "best_day": best and {"date": best["date"], "km": best["km"]},
            "harsh_accel": total.harsh_accel, "harsh_brake": total.harsh_brake, "harsh_turn": total.harsh_turn,
            "crashes": total.crashes, "overspeed_s": round(total.overspeed_s),
            "overspeed_pct": res["overspeed_pct"], "idle_pct": res["idle_pct"],
        },
        "score": {"score": res["score"], "rating": res["rating"], "label": res["rating_label"],
                  "good_min": cfg["good_min"], "fair_min": cfg["fair_min"], "breakdown": res["breakdown"],
                  "speed_limit_kmh": cfg["speed_limit_kmh"]},
        "daily": daily,
        "speed_profile": speed,
        "hours": hours,
        "heatmap": grid if n_days <= HEATMAP_MAX_DAYS else None,
        "drivers": driver_rows,
        "trips": [{"start": tr.start.isoformat(), "end": tr.end.isoformat(), "km": _r(tr.m.km, 2),
                   "duration_s": round(tr.m.duration_s), "driving_s": round(tr.m.moving_s),
                   "max_kmh": round(tr.m.max_kmh), "harsh": tr.m.harsh_accel + tr.m.harsh_brake + tr.m.harsh_turn,
                   "score": tr.result["score"], "rating": tr.result["rating"], "drivers": tr.driver_ids}
                  for tr in reversed(trips)][:100],
        "notes": {"ignition": notes["ignition"], "harsh_source": notes["harsh_source"],
                  "harsh_note": ds._harsh_note(notes), "records": notes["records"], "eco_score": notes["eco_score"]},
    }


@login_required
def vehicle_dashboard(request, pk):
    device = get_object_or_404(Device, pk=pk)
    d_from, d_to, start, end, ctx = ds._range_ctx(request)
    today = timezone.localdate()
    ctx["presets"] = [("Today", today, today), ("7 days", today - timedelta(days=6), today),
                      ("14 days", today - timedelta(days=13), today), ("30 days", today - timedelta(days=29), today),
                      ("This month", today.replace(day=1), today)]
    data = build(device, d_from, d_to, start, end)
    ctx.update({"nav": "vehicle", "d": device, "data": data, "vehicles": Device.objects.order_by("name")})
    return render(request, "tracking/vehicle_dashboard.html", ctx)


@api_view(["GET"])
def api_dashboard(request, pk):
    """Vehicle dashboard numbers: daily distance, driving / idling time, daily driver score, speed profile,
    activity by hour, drivers and trips. ?from=YYYY-MM-DD&to=YYYY-MM-DD (default: the last 7 days)."""
    device = get_object_or_404(Device, pk=pk)
    d_from, d_to, start, end, _ = ds.parse_range(request.query_params)
    return Response(build(device, d_from, d_to, start, end))
