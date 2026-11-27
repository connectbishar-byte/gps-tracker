"""
Driver score sheet: rates driving from what the trackers send and from the drives saved in Car view.

Pages
* /drivers/                         every vehicle and Car view driver ranked for a date range
* /drivers/vehicle/<id>/            score sheet for one vehicle (tracker data)
* /drivers/user/<id>/               score sheet for one Car view driver (drives saved from the phone)
  Each page exports with ?format=xlsx or ?format=csv, and prints / saves as PDF from the browser.
* /drivers/settings/                (staff, POST) the scoring weights and thresholds
* /api/v1/driver-scores/...         the same numbers as JSON

Where the numbers come from (Teltonika FMC130 AVL data)
* Trips          ignition on → off (AVL 239). Without ignition data, a trip is movement with stops under 5 min.
* Distance       sum of the distances between valid GPS fixes.
* Harsh events   Green Driving events (AVL 253: 1 acceleration, 2 braking, 3 cornering) when the tracker sends
                 them; otherwise estimated from speed and heading changes between records 3 s or less apart.
* Over speeding  time driven above the speed limit set here (from the GPS speed of each record).
* Idling         ignition on and standing still (speed ≤ 3 km/h).
* Crashes        Crash Detection events (AVL 247).
* Driver ID      iButton (AVL 78), shown on each trip when the tracker sends one.

Scoring: 100 minus the points lost on each parameter, kept between 0 and 100. Event counts are normalised per
100 km (with a minimum distance, so one event on a 2 km trip does not sink the score); over speeding and idling
are percentages of driving / engine-on time. Weights, limits and rating bands are configurable on Settings and
stored as JSON in the IntegrationSetting row "driver_score_config".
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.cache import cache
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import AVLRecord, Device, IntegrationSetting, IOValue, SavedPlace
from .xlsx import Cell, Sheet, workbook

log = logging.getLogger("fleet.driverscore")

CONFIG_KEY = "driver_score_config"
CONFIG_CACHE = "driverscore:config:v1"
MAX_DAYS = 92

IGNITION, GREEN_DRIVING, CRASH, IBUTTON, ECO_SCORE, OVERSPEED_EVT = 239, 253, 247, 78, 15, 255
STOP_KMH = 3            # at or below this a vehicle is standing still
MAX_GAP_S = 300         # a gap between records longer than this is not counted as driving or idling
SPLIT_GAP_S = 300       # without ignition data, a stop longer than this ends a trip
MIN_TRIP_KM, MIN_TRIP_S = 0.3, 120
DERIVE_MAX_DT = 3       # seconds between records to estimate harsh events from GPS speed / heading

# Every configurable value: key -> (label, default, min, max, step, help, group)
FIELDS = {
    "w_harsh_accel": ("Harsh acceleration", 3.0, 0, 50, 0.5, "points per event per 100 km", "weights"),
    "w_harsh_brake": ("Harsh braking", 4.0, 0, 50, 0.5, "points per event per 100 km", "weights"),
    "w_harsh_turn": ("Harsh cornering", 3.0, 0, 50, 0.5, "points per event per 100 km", "weights"),
    "w_overspeed": ("Over speeding", 2.0, 0, 20, 0.1, "points per 1 % of driving time over the limit", "weights"),
    "w_idling": ("Idling", 0.5, 0, 10, 0.1, "points per 1 % of engine-on time idling, above the allowance", "weights"),
    "w_crash": ("Crash", 25.0, 0, 100, 1, "points per crash event", "weights"),
    "speed_limit_kmh": ("Speed limit", 120, 20, 250, 1, "km/h; faster counts as over speeding", "limits"),
    "idle_allow_pct": ("Idling allowance", 10.0, 0, 100, 1, "% of engine-on time allowed before points are lost", "limits"),
    "min_km": ("Minimum distance", 10.0, 1, 500, 1, "km; event rates use at least this distance", "limits"),
    "accel_ms2": ("Estimated harsh acceleration", 3.0, 1, 10, 0.1, "m/s², used when the tracker sends no Green Driving events", "limits"),
    "brake_ms2": ("Estimated harsh braking", 3.5, 1, 10, 0.1, "m/s²", "limits"),
    "turn_ms2": ("Estimated harsh cornering", 3.5, 1, 10, 0.1, "m/s² sideways", "limits"),
    "good_min": ("Good from", 80, 1, 100, 1, "score at or above this is Good", "bands"),
    "fair_min": ("Fair from", 60, 0, 99, 1, "score at or above this is Fair; below is Aggressive", "bands"),
}
DEFAULTS = {k: v[1] for k, v in FIELDS.items()}
RATINGS = {"good": "Good", "fair": "Fair", "aggressive": "Aggressive"}


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
            cfg.update({k: v for k, v in json.loads(row.value).items() if k in DEFAULTS})
        except ValueError:
            log.warning("driver_score_config is not valid JSON; using defaults")
    cache.set(CONFIG_CACHE, cfg, 60)
    return cfg


def clean_config(data) -> tuple[dict, list[str]]:
    cfg, errors = dict(DEFAULTS), []
    for key, (label, default, lo, hi, _step, _help, _g) in FIELDS.items():
        raw = str(data.get(key, "")).strip()
        if raw == "":
            continue
        try:
            v = float(raw)
        except ValueError:
            errors.append(f"{label}: enter a number.")
            continue
        if not (lo <= v <= hi) or math.isnan(v):
            errors.append(f"{label}: use a value from {lo:g} to {hi:g}.")
            continue
        cfg[key] = int(v) if isinstance(default, int) else v
    if cfg["fair_min"] >= cfg["good_min"]:
        errors.append("“Fair from” must be lower than “Good from”.")
    return cfg, errors


def save_config(cfg: dict, user: str = "") -> None:
    IntegrationSetting.objects.update_or_create(key=CONFIG_KEY, defaults={"value": json.dumps(cfg), "updated_by": user})
    cache.delete(CONFIG_CACHE)


def settings_context() -> dict:
    cfg = get_config()
    groups = {"weights": [], "limits": [], "bands": []}
    for key, (label, default, lo, hi, step, help_, g) in FIELDS.items():
        groups[g].append({"key": key, "label": label, "value": cfg[key], "default": default, "min": lo, "max": hi,
                          "step": step, "help": help_})
    return {"ds_groups": [("Weights (points lost)", groups["weights"]), ("Limits and thresholds", groups["limits"]),
                          ("Rating bands", groups["bands"])]}


def rating_for(score, cfg) -> str:
    if score is None:
        return ""
    return "good" if score >= cfg["good_min"] else "fair" if score >= cfg["fair_min"] else "aggressive"


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
@dataclass
class Metrics:
    km: float = 0.0
    duration_s: float = 0.0      # engine on (or trip) time
    moving_s: float = 0.0
    idle_s: float | None = 0.0   # None: not measurable (phone drives)
    overspeed_s: float = 0.0
    max_kmh: float = 0.0
    harsh_accel: int = 0
    harsh_brake: int = 0
    harsh_turn: int = 0
    crashes: int = 0
    overspeed_events: int = 0
    trips: int = 0

    def add(self, o: "Metrics"):
        self.km += o.km
        self.duration_s += o.duration_s
        self.moving_s += o.moving_s
        self.idle_s = None if (self.idle_s is None or o.idle_s is None) else self.idle_s + o.idle_s
        self.overspeed_s += o.overspeed_s
        self.max_kmh = max(self.max_kmh, o.max_kmh)
        for k in ("harsh_accel", "harsh_brake", "harsh_turn", "crashes", "overspeed_events", "trips"):
            setattr(self, k, getattr(self, k) + getattr(o, k))

    @property
    def avg_kmh(self) -> float:
        return self.km / (self.moving_s / 3600) if self.moving_s > 60 else 0.0

    @property
    def driven(self) -> bool:
        return self.km >= 0.5 or self.moving_s >= 60


def _fmt_dur(s) -> str:
    if s is None:
        return "—"
    m = int(round(s / 60))
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def score(m: Metrics, cfg: dict) -> dict:
    """Overall score and the per-parameter breakdown for a set of metrics."""
    dist = max(m.km, cfg["min_km"])
    per100 = (lambda n: n * 100 / dist)
    over_pct = 100 * m.overspeed_s / m.moving_s if m.moving_s else 0.0
    idle_pct = (100 * m.idle_s / m.duration_s) if (m.idle_s is not None and m.duration_s) else None
    rows = [
        ("harsh_accel", "Harsh acceleration", f"{m.harsh_accel}", f"{per100(m.harsh_accel):.1f} per 100 km",
         cfg["w_harsh_accel"] * per100(m.harsh_accel), f"{cfg['w_harsh_accel']:g} / event / 100 km"),
        ("harsh_brake", "Harsh braking", f"{m.harsh_brake}", f"{per100(m.harsh_brake):.1f} per 100 km",
         cfg["w_harsh_brake"] * per100(m.harsh_brake), f"{cfg['w_harsh_brake']:g} / event / 100 km"),
        ("harsh_turn", "Harsh cornering", f"{m.harsh_turn}", f"{per100(m.harsh_turn):.1f} per 100 km",
         cfg["w_harsh_turn"] * per100(m.harsh_turn), f"{cfg['w_harsh_turn']:g} / event / 100 km"),
        ("overspeed", f"Over speeding (> {cfg['speed_limit_kmh']} km/h)", _fmt_dur(m.overspeed_s),
         f"{over_pct:.1f} % of driving time", cfg["w_overspeed"] * over_pct, f"{cfg['w_overspeed']:g} / %"),
        ("idling", "Idling", _fmt_dur(m.idle_s) if m.idle_s is not None else "not measured",
         f"{idle_pct:.1f} % of engine-on time" if idle_pct is not None else "—",
         cfg["w_idling"] * max(0.0, idle_pct - cfg["idle_allow_pct"]) if idle_pct is not None else 0.0,
         f"{cfg['w_idling']:g} / % above {cfg['idle_allow_pct']:g} %"),
        ("crash", "Crashes", f"{m.crashes}", f"{m.crashes} event{'s' if m.crashes != 1 else ''}",
         cfg["w_crash"] * m.crashes, f"{cfg['w_crash']:g} / event"),
    ]
    breakdown = []
    for key, label, measured, rate, pts, weight in rows:
        pts = round(pts, 1)
        breakdown.append({"key": key, "label": label, "measured": measured, "rate": rate, "weight": weight,
                          "points": pts, "score": max(0, min(100, round(100 - pts))),
                          "rating": rating_for(max(0, min(100, round(100 - pts))), cfg)})
    if not m.driven:
        total = None
    else:
        total = max(0, min(100, round(100 - sum(b["points"] for b in breakdown))))
    return {"score": total, "rating": rating_for(total, cfg), "rating_label": RATINGS.get(rating_for(total, cfg), "No driving"),
            "breakdown": breakdown, "overspeed_pct": round(over_pct, 1),
            "idle_pct": None if idle_pct is None else round(idle_pct, 1)}


def _hav_km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _dedupe(times: list, gap_s: float) -> list:
    out = []
    for t in sorted(times):
        if not out or (t - out[-1]).total_seconds() > gap_s:
            out.append(t)
    return out


@dataclass
class Trip:
    start: datetime
    end: datetime
    m: Metrics
    start_pos: tuple | None = None
    end_pos: tuple | None = None
    driver_ids: list = field(default_factory=list)
    source: str = "tracker"
    name: str = ""
    result: dict | None = None


def vehicle_trips(device: Device, start: datetime, end: datetime, cfg: dict) -> tuple[list[Trip], dict]:
    """Split a vehicle's records into trips and measure each one. Returns (trips, notes about the data)."""
    recs, seen = [], set()
    qs = (AVLRecord.objects.filter(device=device, timestamp_utc__gte=start, timestamp_utc__lt=end)
          .order_by("timestamp_utc", "id").values_list("timestamp_utc", "speed", "latitude", "longitude", "valid_fix", "angle"))
    for row in qs.iterator(chunk_size=5000):
        if row[0] in seen:
            continue
        seen.add(row[0])
        recs.append(row)
    ios = list(IOValue.objects.filter(device=device, timestamp_utc__gte=start, timestamp_utc__lt=end,
                                      avl_id__in=[IGNITION, GREEN_DRIVING, CRASH, IBUTTON, ECO_SCORE, OVERSPEED_EVT])
               .order_by("timestamp_utc").values_list("timestamp_utc", "avl_id", "value"))
    ign = {t: bool(v) for t, a, v in ios if a == IGNITION and v is not None}
    green = {1: [], 2: [], 3: []}
    crashes, ospeed, eco, drivers = [], [], [], []
    for t, a, v in ios:
        if v is None:
            continue
        if a == GREEN_DRIVING and int(v) in green:
            green[int(v)].append(t)
        elif a == CRASH and v > 0:
            crashes.append(t)
        elif a == OVERSPEED_EVT and v > 0:
            ospeed.append(t)
        elif a == ECO_SCORE and v > 0:
            eco.append(v)
        elif a == IBUTTON and v > 0:
            drivers.append((t, f"{int(v):X}"))
    have_green = any(green.values())
    green = {k: _dedupe(v, 10) for k, v in green.items()}
    crashes, ospeed = _dedupe(crashes, 60), _dedupe(ospeed, 30)
    have_ign = bool(ign)

    # Walk the records and cut trips
    trips: list[Trip] = []
    cur, last_moving, prev, ign_state = None, None, None, None
    derived = {1: [], 2: [], 3: []}
    limit = cfg["speed_limit_kmh"]

    def close(trip):
        if trip and (trip.m.km >= MIN_TRIP_KM or (trip.end - trip.start).total_seconds() >= MIN_TRIP_S and trip.m.moving_s >= 60):
            trip.m.duration_s = (trip.end - trip.start).total_seconds()
            trip.m.trips = 1
            trips.append(trip)

    for t, spd, lat, lon, fix, ang in recs:
        if t in ign:
            ign_state = ign[t]
        moving = spd > STOP_KMH
        on = ign_state if have_ign and ign_state is not None else None
        if on is None:
            # no ignition data: a trip is movement, ending after a long stop
            if cur and not moving and last_moving and (t - last_moving).total_seconds() > SPLIT_GAP_S:
                cur.end = last_moving
                close(cur)
                cur = None
            in_trip = moving or cur is not None
        else:
            if cur and not on:
                cur.end = t
                close(cur)
                cur = None
            in_trip = on
        if in_trip and cur is None:
            cur = Trip(start=t, end=t, m=Metrics(), start_pos=(lat, lon) if fix else None)
            prev = None
        if cur is None:
            prev = (t, spd, lat, lon, fix, ang)
            continue
        if moving:
            last_moving = t
        cur.end = t
        if fix:
            cur.end_pos = (lat, lon)
            if cur.start_pos is None:
                cur.start_pos = (lat, lon)
        cur.m.max_kmh = max(cur.m.max_kmh, spd)
        if prev is not None:
            pt, pspd, plat, plon, pfix, pang = prev
            dt = (t - pt).total_seconds()
            if 0 < dt <= MAX_GAP_S:
                if moving or pspd > STOP_KMH:
                    cur.m.moving_s += dt
                    if (spd + pspd) / 2 > limit:
                        cur.m.overspeed_s += dt
                elif on:
                    cur.m.idle_s += dt
            if fix and pfix and dt > 0:
                d = _hav_km(plat, plon, lat, lon)
                if d / (dt / 3600) < 300:  # ignore GPS jumps
                    cur.m.km += d
            if not have_green and 0 < dt <= DERIVE_MAX_DT:
                a = (spd - pspd) / 3.6 / dt
                if a > cfg["accel_ms2"] and spd > 10:
                    derived[1].append(t)
                elif -a > cfg["brake_ms2"] and pspd > 10:
                    derived[2].append(t)
                if spd > 20 and pspd > 20:
                    dh = abs((ang - pang + 180) % 360 - 180)
                    if (spd / 3.6) * math.radians(dh) / dt > cfg["turn_ms2"]:
                        derived[3].append(t)
        prev = (t, spd, lat, lon, fix, ang)
    if cur:
        close(cur)

    events = green if have_green else {k: _dedupe(v, 5) for k, v in derived.items()}
    for tr in trips:
        inside = (lambda ts: tr.start <= ts <= tr.end)
        tr.m.harsh_accel = sum(1 for x in events[1] if inside(x))
        tr.m.harsh_brake = sum(1 for x in events[2] if inside(x))
        tr.m.harsh_turn = sum(1 for x in events[3] if inside(x))
        tr.m.crashes = sum(1 for x in crashes if inside(x))
        tr.m.overspeed_events = sum(1 for x in ospeed if inside(x))
        tr.driver_ids = sorted({d for x, d in drivers if inside(x)})
        if not have_ign:
            tr.m.idle_s = None

    notes = {
        "records": len(recs),
        "ignition": have_ign,
        "harsh_source": "tracker" if have_green else ("estimated" if any(derived.values()) or _dense(recs) else "none"),
        "eco_score": round(sum(eco) / len(eco), 2) if eco else None,
        "drivers": sorted({d for _, d in drivers}),
    }
    return trips, notes


def _dense(recs) -> bool:
    """True when enough records are close together to estimate harsh events from GPS."""
    close_pairs = sum(1 for a, b in zip(recs, recs[1:]) if (b[0] - a[0]).total_seconds() <= DERIVE_MAX_DT)
    return close_pairs >= max(10, len(recs) // 4)


def phone_trips(user, start: datetime, end: datetime) -> list[Trip]:
    out = []
    qs = SavedPlace.objects.filter(owner=user, kind="trip", created_utc__gte=start, created_utc__lt=end).select_related("device")
    for p in qs.order_by("created_utc"):
        dur = float(p.duration_s or 0)
        m = Metrics(km=float(p.distance_km or 0), duration_s=dur, moving_s=float(p.moving_s or dur),
                    idle_s=None, overspeed_s=float(p.overspeed_s or 0), max_kmh=float(p.max_speed_kmh or 0),
                    harsh_accel=p.harsh_accel, harsh_brake=p.harsh_brake, harsh_turn=p.harsh_turn, trips=1)
        st = p.started_utc or (p.created_utc - timedelta(seconds=dur))
        out.append(Trip(start=st, end=p.ended_utc or p.created_utc, m=m, source="car view", name=p.name,
                        start_pos=(p.start_latitude, p.start_longitude) if p.start_latitude is not None else None,
                        end_pos=(p.latitude, p.longitude)))
    return out


# ---------------------------------------------------------------------------
# Building a sheet
# ---------------------------------------------------------------------------
def parse_range(get) -> tuple[date, date, datetime, datetime, str]:
    """Dates from ?from=YYYY-MM-DD&to=YYYY-MM-DD (inclusive, local time). Default: the last 7 days."""
    today = timezone.localdate()
    err = ""
    try:
        d_to = date.fromisoformat(get.get("to") or "") if get.get("to") else today
        d_from = date.fromisoformat(get.get("from") or "") if get.get("from") else d_to - timedelta(days=6)
    except ValueError:
        d_from, d_to, err = today - timedelta(days=6), today, "Dates must look like 2026-11-27; showing the last 7 days."
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    if (d_to - d_from).days + 1 > MAX_DAYS:
        d_from, err = d_to - timedelta(days=MAX_DAYS - 1), f"A sheet covers at most {MAX_DAYS} days; shortened."
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime.combine(d_from, time.min), tz)
    end = timezone.make_aware(datetime.combine(d_to + timedelta(days=1), time.min), tz)
    return d_from, d_to, start, end, err


def build_sheet(kind: str, subject, start, end, cfg) -> dict:
    if kind == "vehicle":
        trips, notes = vehicle_trips(subject, start, end, cfg)
        title = subject.name + (f" · {subject.plate}" if subject.plate else "")
    else:
        trips, notes = phone_trips(subject, start, end), {"ignition": False, "harsh_source": "phone",
                                                           "eco_score": None, "drivers": [], "records": None}
        title = (subject.get_full_name() or subject.get_username())
    total = Metrics(idle_s=0.0 if notes["ignition"] else None)
    for tr in trips:
        tr.result = score(tr.m, cfg)
        total.add(tr.m)
    if not trips:
        total.idle_s = None if not notes["ignition"] else 0.0
    res = score(total, cfg)
    return {"kind": kind, "subject": subject, "title": title, "trips": trips, "total": total, "result": res,
            "notes": notes, "cfg": cfg}


def subjects_for(user):
    """Vehicles (everyone sees all, as on the other pages) and Car view drivers (staff: all, others: self)."""
    vehicles = list(Device.objects.order_by("name"))
    User = get_user_model()
    ids = SavedPlace.objects.filter(kind="trip", owner__isnull=False).values_list("owner_id", flat=True).distinct()
    drivers = User.objects.filter(pk__in=ids).order_by("username")
    if not user.is_staff:
        drivers = drivers.filter(pk=user.pk)
    return vehicles, list(drivers)


def overview(user, start, end, cfg) -> list[dict]:
    vehicles, drivers = subjects_for(user)
    rows = []
    for v in vehicles:
        rows.append(build_sheet("vehicle", v, start, end, cfg))
    for u in drivers:
        rows.append(build_sheet("user", u, start, end, cfg))
    rows.sort(key=lambda s: (s["result"]["score"] is None, -(s["result"]["score"] or 0), s["title"].lower()))
    return rows


def _local(dt):
    return timezone.localtime(dt) if dt else None


def _maps(pos):
    return f"https://www.google.com/maps/search/?api=1&query={pos[0]:.6f},{pos[1]:.6f}" if pos else ""


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------
def _harsh_note(notes) -> str:
    return {"tracker": "Harsh events: from the tracker's Green Driving events (AVL 253).",
            "estimated": "Harsh events: estimated from GPS speed and heading (the tracker sends no Green Driving events).",
            "none": "Harsh events: not available. Records are too far apart and the tracker sends no Green Driving "
                    "events; turn on Green Driving in the FMC130 configurator.",
            "phone": "Harsh events: measured by the phone in Car view."}[notes["harsh_source"]]


def trip_row(tr: Trip) -> list:
    r = tr.result
    return [_local(tr.start), _local(tr.end), round(tr.m.km, 2), round(tr.m.duration_s / 60),
            round(tr.m.moving_s / 60), "" if tr.m.idle_s is None else round(tr.m.idle_s / 60),
            round(tr.m.max_kmh), round(tr.m.avg_kmh), tr.m.harsh_accel, tr.m.harsh_brake, tr.m.harsh_turn,
            round(tr.m.overspeed_s / 60, 1), tr.m.crashes, "" if r["score"] is None else r["score"],
            r["rating_label"], ", ".join(tr.driver_ids), tr.name, _maps(tr.start_pos), _maps(tr.end_pos)]


TRIP_HEAD = ["Start", "End", "Distance (km)", "Duration (min)", "Driving (min)", "Idling (min)", "Max speed (km/h)",
             "Avg speed (km/h)", "Harsh accel", "Harsh brake", "Harsh turns", "Over speeding (min)", "Crashes",
             "Score", "Rating", "Driver ID", "Name", "Start on map", "End on map"]
OVERVIEW_HEAD = ["Driver / vehicle", "Type", "Trips", "Distance (km)", "Driving time (min)", "Harsh accel",
                 "Harsh brake", "Harsh turns", "Over speeding (% of driving)", "Idling (% of engine on)", "Crashes",
                 "Max speed (km/h)", "Score", "Rating"]


def overview_row(s) -> list:
    t, r = s["total"], s["result"]
    return [s["title"], "Vehicle" if s["kind"] == "vehicle" else "Car view driver", t.trips, round(t.km, 1),
            round(t.moving_s / 60), t.harsh_accel, t.harsh_brake, t.harsh_turn, r["overspeed_pct"],
            "" if r["idle_pct"] is None else r["idle_pct"], t.crashes, round(t.max_kmh),
            "" if r["score"] is None else r["score"], r["rating_label"]]


def _settings_sheet(cfg) -> Sheet:
    rows = [[Cell("Setting", "head"), Cell("Value", "head"), Cell("Meaning", "head")]]
    for key, (label, *_rest) in FIELDS.items():
        rows.append([label, cfg[key], FIELDS[key][5]])
    return Sheet("Scoring settings", rows, widths=[30, 10, 70], freeze=1)


def sheet_xlsx(s, d_from, d_to) -> bytes:
    t, r = s["total"], s["result"]
    head = [
        [Cell("Driver score sheet", "title")],
        [Cell(s["title"], "bold"), "Vehicle" if s["kind"] == "vehicle" else "Car view driver"],
        ["Period", f"{d_from:%d %b %Y} to {d_to:%d %b %Y}"],
        ["Generated", timezone.localtime().strftime("%d %b %Y %H:%M")],
        [],
        [Cell("Overall score", "bold"), Cell("" if r["score"] is None else r["score"], r["rating"] or "bold"),
         Cell(r["rating_label"], r["rating"] or "bold")],
        ["Trips", t.trips], ["Distance (km)", round(t.km, 1)], ["Driving time", _fmt_dur(t.moving_s)],
        ["Engine on / trip time", _fmt_dur(t.duration_s)], ["Max speed (km/h)", round(t.max_kmh)],
        ["Average speed (km/h)", round(t.avg_kmh)],
    ]
    if s["notes"].get("eco_score") is not None:
        head.append(["Tracker eco score (AVL 15, average)", s["notes"]["eco_score"]])
    if s["notes"].get("drivers"):
        head.append(["Driver IDs (iButton)", ", ".join(s["notes"]["drivers"])])
    head += [[], [Cell("Parameter", "head"), Cell("Measured", "head"), Cell("Rate", "head"), Cell("Weight", "head"),
                  Cell("Points lost", "head"), Cell("Parameter score", "head")]]
    for b in r["breakdown"]:
        head.append([b["label"], b["measured"], b["rate"], b["weight"], b["points"], Cell(b["score"], b["rating"])])
    head += [[], [Cell(_harsh_note(s["notes"]), "muted")]]
    if s["kind"] == "vehicle" and not s["notes"]["ignition"]:
        head.append([Cell("Trips are split on stops longer than 5 minutes (the tracker sends no ignition value); idling is not measured.", "muted")])
    trips = [[Cell(h, "head") for h in TRIP_HEAD]] + [trip_row(tr) for tr in s["trips"]]
    return workbook([Sheet("Score sheet", head, widths=[34, 26, 24, 22, 12, 16]),
                     Sheet("Trips", trips, widths=[17, 17, 12, 12, 12, 11, 12, 12, 10, 10, 10, 14, 9, 8, 11, 12, 24, 30, 30], freeze=1),
                     _settings_sheet(s["cfg"])])


def overview_xlsx(rows, d_from, d_to, cfg) -> bytes:
    data = [[Cell("Driver scores", "title")], [f"{d_from:%d %b %Y} to {d_to:%d %b %Y}"], [],
            [Cell(h, "head") for h in OVERVIEW_HEAD]]
    for s in rows:
        row = overview_row(s)
        row[12] = Cell(row[12], s["result"]["rating"])
        data.append(row)
    return workbook([Sheet("Driver scores", data, widths=[30, 16, 7, 12, 14, 10, 10, 10, 16, 16, 8, 12, 8, 12]),
                     _settings_sheet(cfg)])


def _csv(rows_iter) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    for row in rows_iter:
        w.writerow(["" if v is None else (v.strftime("%Y-%m-%d %H:%M") if isinstance(v, datetime) else v) for v in row])
    return "﻿" + buf.getvalue()


def sheet_csv(s, d_from, d_to) -> str:
    r = s["result"]
    rows = [["Driver score sheet", s["title"]], ["Period", f"{d_from} to {d_to}"],
            ["Overall score", "" if r["score"] is None else r["score"], r["rating_label"]], [],
            ["Parameter", "Measured", "Rate", "Weight", "Points lost", "Parameter score"]]
    rows += [[b["label"], b["measured"], b["rate"], b["weight"], b["points"], b["score"]] for b in r["breakdown"]]
    rows += [[], TRIP_HEAD] + [trip_row(tr) for tr in s["trips"]]
    return _csv(rows)


def _download(body, fmt, name):
    ctype = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if fmt == "xlsx"
             else "text/csv; charset=utf-8")
    resp = HttpResponse(body, content_type=ctype)
    resp["Content-Disposition"] = f'attachment; filename="{name}.{fmt}"'
    return resp


def _slug(text) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")[:40] or "driver"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def _range_ctx(request):
    d_from, d_to, start, end, err = parse_range(request.GET)
    if err:
        messages.warning(request, err)
    today = timezone.localdate()
    presets = [("Today", today, today), ("Last 7 days", today - timedelta(days=6), today),
               ("Last 30 days", today - timedelta(days=29), today),
               ("This month", today.replace(day=1), today)]
    return d_from, d_to, start, end, {"d_from": d_from, "d_to": d_to, "presets": presets,
                                      "qs": f"from={d_from.isoformat()}&to={d_to.isoformat()}"}


@login_required
def drivers_page(request):
    d_from, d_to, start, end, ctx = _range_ctx(request)
    cfg = get_config()
    rows = overview(request.user, start, end, cfg)
    fmt = request.GET.get("format")
    if fmt == "xlsx":
        return _download(overview_xlsx(rows, d_from, d_to, cfg), fmt, f"driver-scores-{d_from}-{d_to}")
    if fmt == "csv":
        return _download(_csv([OVERVIEW_HEAD] + [overview_row(s) for s in rows]), fmt, f"driver-scores-{d_from}-{d_to}")
    scored = [s["result"]["score"] for s in rows if s["result"]["score"] is not None]
    ctx.update({"nav": "drivers", "rows": rows, "cfg": cfg,
                "fleet_avg": round(sum(scored) / len(scored)) if scored else None,
                "fleet_rating": rating_for(round(sum(scored) / len(scored)), cfg) if scored else "",
                "counts": {k: sum(1 for s in rows if s["result"]["rating"] == k) for k in RATINGS}})
    if request.user.is_staff:
        ctx.update(settings_context())
    return render(request, "tracking/drivers.html", ctx)


def _subject(request, kind, pk):
    if kind == "vehicle":
        return get_object_or_404(Device, pk=pk)
    if kind == "user":
        if not (request.user.is_staff or request.user.pk == pk):
            raise Http404
        return get_object_or_404(get_user_model(), pk=pk)
    raise Http404


@login_required
def driver_sheet(request, kind, pk):
    subject = _subject(request, kind, pk)
    d_from, d_to, start, end, ctx = _range_ctx(request)
    s = build_sheet(kind, subject, start, end, get_config())
    fmt = request.GET.get("format")
    name = f"driver-score-{_slug(s['title'])}-{d_from}-{d_to}"
    if fmt == "xlsx":
        return _download(sheet_xlsx(s, d_from, d_to), fmt, name)
    if fmt == "csv":
        return _download(sheet_csv(s, d_from, d_to), fmt, name)
    vehicles, drivers = subjects_for(request.user)
    ctx.update({"nav": "drivers", "s": s, "harsh_note": _harsh_note(s["notes"]), "vehicles": vehicles,
                "drivers": drivers, "auto_print": request.GET.get("print") == "1"})
    if request.user.is_staff:
        ctx.update(settings_context())
    return render(request, "tracking/driver_sheet.html", ctx)


staff_required = user_passes_test(lambda u: u.is_active and u.is_staff)


@login_required
@staff_required
@require_POST
def driver_settings(request):
    if request.POST.get("reset"):
        save_config(dict(DEFAULTS), request.user.get_username())
        messages.success(request, "Driver score settings reset to the defaults.")
    else:
        cfg, errors = clean_config(request.POST)
        for e in errors:
            messages.error(request, e)
        if not errors:
            save_config(cfg, request.user.get_username())
            messages.success(request, "Driver score settings saved. Every score sheet now uses them.")
    nxt = request.POST.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(reverse("settings") + "#driverscore")


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------
def _json(s) -> dict:
    t, r = s["total"], s["result"]
    subj = s["subject"]
    return {
        "type": s["kind"], "id": subj.pk, "name": s["title"], "score": r["score"], "rating": r["rating"],
        "trips": t.trips, "distance_km": round(t.km, 2), "driving_s": round(t.moving_s), "engine_on_s": round(t.duration_s),
        "idle_s": None if t.idle_s is None else round(t.idle_s), "overspeed_s": round(t.overspeed_s),
        "max_speed_kmh": round(t.max_kmh), "harsh_accel": t.harsh_accel, "harsh_brake": t.harsh_brake,
        "harsh_turn": t.harsh_turn, "crashes": t.crashes, "harsh_source": s["notes"]["harsh_source"],
        "breakdown": r["breakdown"],
    }


@api_view(["GET"])
def api_scores(request):
    """Driver scores for every vehicle and Car view driver. ?from=YYYY-MM-DD&to=YYYY-MM-DD (default: last 7 days)."""
    d_from, d_to, start, end, _ = parse_range(request.query_params)
    rows = overview(request.user, start, end, get_config())
    return Response({"from": d_from, "to": d_to, "results": [_json(s) for s in rows]})


@api_view(["GET"])
def api_sheet(request, kind, pk):
    """One score sheet with its trips. kind is vehicle or user."""
    subject = _subject(request, kind, pk)
    d_from, d_to, start, end, _ = parse_range(request.query_params)
    s = build_sheet(kind, subject, start, end, get_config())
    out = _json(s)
    out.update({"from": d_from, "to": d_to, "trips_detail": [
        {"start": tr.start, "end": tr.end, "distance_km": round(tr.m.km, 2), "duration_s": round(tr.m.duration_s),
         "driving_s": round(tr.m.moving_s), "idle_s": None if tr.m.idle_s is None else round(tr.m.idle_s),
         "max_speed_kmh": round(tr.m.max_kmh), "harsh_accel": tr.m.harsh_accel, "harsh_brake": tr.m.harsh_brake,
         "harsh_turn": tr.m.harsh_turn, "overspeed_s": round(tr.m.overspeed_s), "crashes": tr.m.crashes,
         "score": tr.result["score"], "rating": tr.result["rating"], "driver_ids": tr.driver_ids, "source": tr.source}
        for tr in s["trips"]]})
    return Response(out)
