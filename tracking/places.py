"""
Mobile car view (/drive/) and saved places (/places/).

* /drive/          full-screen page for a phone in the car: Start / Stop a drive, live stats, pin a spot.
                   Position comes from the phone's GPS (browser geolocation, needs HTTPS) or from a tracked vehicle.
* /places/         list of saved places with Open in Google Maps / Navigate, rename, delete, export and import.
* /places/export.<kml|csv|geojson>   files Google My Maps, Google Earth and most GIS tools import.
* /places/import/  load a KML / CSV / GeoJSON file back in as saved places.
* /places/geocode/ reverse geocoding (place name for a lat/lon) through OpenStreetMap Nominatim, cached.
* /api/v1/places/  REST API (list / create / update / delete), same rules as the pages.

Who sees what: a user sees the places they saved; staff users see everyone's.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import timezone as dt_timezone
from xml.sax.saxutils import escape

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
from rest_framework import permissions, serializers, viewsets

from .models import Device, SavedPlace

log = logging.getLogger(__name__)

MAX_TRACK_POINTS = 2000
MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 2000
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "MelticeGPSTracker/1.0 (fleet tracking web app)"


def places_for(user):
    qs = SavedPlace.objects.select_related("device", "owner")
    return qs if user.is_staff else qs.filter(owner=user)


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------
def _thin(track, limit=MAX_TRACK_POINTS):
    """Keep at most `limit` points, always keeping the first and last."""
    if len(track) <= limit:
        return track
    step = (len(track) - 1) / (limit - 1)
    return [track[round(i * step)] for i in range(limit)]


class SavedPlaceSerializer(serializers.ModelSerializer):
    owner = serializers.CharField(source="owner.username", read_only=True, default="")
    device_name = serializers.CharField(source="device.name", read_only=True, default="")
    device = serializers.PrimaryKeyRelatedField(queryset=Device.objects.all(), required=False, allow_null=True)
    google_maps_url = serializers.SerializerMethodField()
    directions_url = serializers.SerializerMethodField()

    class Meta:
        model = SavedPlace
        fields = ["id", "kind", "source", "name", "notes", "latitude", "longitude", "accuracy_m", "address",
                  "created_utc", "owner", "device", "device_name",
                  "started_utc", "ended_utc", "start_latitude", "start_longitude", "start_address",
                  "distance_km", "duration_s", "moving_s", "max_speed_kmh", "avg_speed_kmh",
                  "driver_score", "driver_rating", "harsh_accel", "harsh_brake", "harsh_turn", "overspeed_s",
                  "track", "google_maps_url", "directions_url"]
        read_only_fields = ["created_utc", "owner"]
        extra_kwargs = {"name": {"required": False, "allow_blank": True}}

    def get_google_maps_url(self, obj) -> str:
        return google_maps_url(obj.latitude, obj.longitude)

    def get_directions_url(self, obj) -> str:
        return directions_url(obj.latitude, obj.longitude)

    def validate_track(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("Send a list of [lat, lon] pairs.")
        out = []
        for p in value:
            try:
                lat, lon = float(p[0]), float(p[1])
            except (TypeError, ValueError, IndexError):
                raise serializers.ValidationError("Each point must be [lat, lon].")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise serializers.ValidationError("Point out of range.")
            out.append([round(lat, 6), round(lon, 6)])
        return _thin(out)

    def validate_driver_rating(self, value):
        if value and value not in ("good", "fair", "aggressive"):
            raise serializers.ValidationError("Use good, fair or aggressive.")
        return value

    def validate(self, attrs):
        if not attrs.get("name") and not (self.instance and self.instance.name):
            attrs["name"] = default_name(attrs.get("kind", "trip"), attrs.get("address", ""))
        return attrs


class OwnPlacesPermission(permissions.IsAuthenticated):
    def has_object_permission(self, request, view, obj):
        return request.user.is_staff or obj.owner_id == request.user.id


class SavedPlaceViewSet(viewsets.ModelViewSet):
    """Saved places: where drives were stopped in the car view, pinned spots and imported places.
    Users see their own; staff see everyone's. Filter with ?kind=trip|pin|import."""
    queryset = SavedPlace.objects.none()  # for the API schema; get_queryset() scopes to the user
    serializer_class = SavedPlaceSerializer
    permission_classes = [OwnPlacesPermission]

    def get_queryset(self):
        qs = places_for(self.request.user)
        kind = self.request.query_params.get("kind")
        return qs.filter(kind=kind) if kind else qs

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)


def default_name(kind: str, address: str = "") -> str:
    now = timezone.localtime().strftime("%d %b %Y %H:%M")
    label = {"trip": "Trip", "pin": "Pinned spot", "import": "Imported place"}.get(kind, "Place")
    short = address.split(",")[0].strip() if address else ""
    return f"{short} · {now}" if short else f"{label} · {now}"


def google_maps_url(lat, lon) -> str:
    return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"


def directions_url(lat, lon) -> str:
    return f"https://www.google.com/maps/dir/?api=1&destination={lat:.6f},{lon:.6f}&travelmode=driving"


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@login_required
def drive(request):
    vehicles = Device.objects.filter(enabled=True).order_by("name").values("id", "name", "plate")
    return render(request, "tracking/drive.html", {"nav": "drive", "vehicles": list(vehicles)})


@login_required
def places_page(request):
    qs = places_for(request.user)
    kind = request.GET.get("kind", "")
    if kind in dict(SavedPlace.KINDS):
        qs = qs.filter(kind=kind)
    q = request.GET.get("q", "").strip()
    if q:
        from django.db.models import Q
        qs = qs.filter(Q(name__icontains=q) | Q(address__icontains=q) | Q(notes__icontains=q))
    places = list(qs[:500])
    data = SavedPlaceSerializer(places, many=True).data
    return render(request, "tracking/places.html", {
        "nav": "places", "places": places, "places_data": data,
        "kind": kind, "q": q, "kinds": SavedPlace.KINDS,
    })


@login_required
@require_POST
def place_delete(request, pk):
    obj = places_for(request.user).filter(pk=pk).first()
    if obj is None:
        raise Http404
    obj.delete()
    messages.success(request, f"Deleted “{obj.name}”.")
    return redirect("places")


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def _fmt_dur(s):
    if s is None:
        return ""
    h, m = divmod(int(s) // 60, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def _description(p: SavedPlace) -> str:
    lines = []
    if p.address:
        lines.append(p.address)
    if p.kind == "trip" and p.distance_km is not None:
        lines.append(f"Drive: {p.distance_km:.1f} km in {_fmt_dur(p.duration_s)}")
        if p.max_speed_kmh is not None:
            lines.append(f"Max speed {p.max_speed_kmh:.0f} km/h, average {p.avg_speed_kmh or 0:.0f} km/h")
        if p.driver_rating:
            lines.append(f"Driving: {p.driver_rating} ({p.driver_score}/100), harsh accel {p.harsh_accel}, "
                         f"harsh brake {p.harsh_brake}, harsh turns {p.harsh_turn}")
        if p.start_address:
            lines.append(f"From: {p.start_address}")
    if p.notes:
        lines.append(p.notes)
    lines.append(f"Saved {timezone.localtime(p.created_utc):%d %b %Y %H:%M}")
    return "\n".join(lines)


def export_kml(places) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
           "<name>Meltice saved places</name>",
           '<Style id="stop"><IconStyle><Icon><href>https://maps.google.com/mapfiles/kml/paddle/red-circle.png</href></Icon></IconStyle></Style>',
           '<Style id="pin"><IconStyle><Icon><href>https://maps.google.com/mapfiles/kml/paddle/blu-stars.png</href></Icon></IconStyle></Style>',
           '<Style id="route"><LineStyle><color>ffd8781e</color><width>4</width></LineStyle></Style>']
    for p in places:
        when = p.created_utc.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out.append(f"<Placemark><name>{escape(p.name)}</name>"
                   f"<description>{escape(_description(p))}</description>"
                   f"<TimeStamp><when>{when}</when></TimeStamp>"
                   f"<styleUrl>#{'pin' if p.kind == 'pin' else 'stop'}</styleUrl>"
                   f"<Point><coordinates>{p.longitude:.6f},{p.latitude:.6f},0</coordinates></Point></Placemark>")
        if p.track and len(p.track) > 1:
            coords = " ".join(f"{lon:.6f},{lat:.6f},0" for lat, lon in p.track)
            out.append(f"<Placemark><name>{escape(p.name)} (route)</name><styleUrl>#route</styleUrl>"
                       f"<LineString><tessellate>1</tessellate><coordinates>{coords}</coordinates></LineString></Placemark>")
    out.append("</Document></kml>")
    return "\n".join(out)


CSV_FIELDS = ["name", "latitude", "longitude", "address", "kind", "saved_at", "notes", "distance_km",
              "duration_min", "max_speed_kmh", "avg_speed_kmh", "driver_rating", "driver_score", "google_maps_url"]


def export_csv(places) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_FIELDS)
    for p in places:
        w.writerow([p.name, f"{p.latitude:.6f}", f"{p.longitude:.6f}", p.address, p.kind,
                    timezone.localtime(p.created_utc).strftime("%Y-%m-%d %H:%M"), p.notes,
                    "" if p.distance_km is None else f"{p.distance_km:.2f}",
                    "" if p.duration_s is None else round(p.duration_s / 60),
                    "" if p.max_speed_kmh is None else round(p.max_speed_kmh),
                    "" if p.avg_speed_kmh is None else round(p.avg_speed_kmh),
                    p.driver_rating, "" if p.driver_score is None else p.driver_score,
                    google_maps_url(p.latitude, p.longitude)])
    return "﻿" + buf.getvalue()  # BOM so Excel opens Arabic / accented names correctly


def export_geojson(places) -> str:
    feats = []
    for p in places:
        props = {"name": p.name, "kind": p.kind, "address": p.address, "notes": p.notes,
                 "saved_at": p.created_utc.isoformat(), "distance_km": p.distance_km, "duration_s": p.duration_s,
                 "max_speed_kmh": p.max_speed_kmh, "avg_speed_kmh": p.avg_speed_kmh,
                 "driver_rating": p.driver_rating, "driver_score": p.driver_score}
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "Point", "coordinates": [p.longitude, p.latitude]}})
        if p.track and len(p.track) > 1:
            feats.append({"type": "Feature", "properties": {"name": p.name + " (route)", "kind": "route"},
                          "geometry": {"type": "LineString", "coordinates": [[lon, lat] for lat, lon in p.track]}})
    return json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False)


EXPORTERS = {
    "kml": (export_kml, "application/vnd.google-earth.kml+xml"),
    "csv": (export_csv, "text/csv; charset=utf-8"),
    "geojson": (export_geojson, "application/geo+json"),
}


@login_required
@require_GET
def places_export(request, fmt):
    if fmt not in EXPORTERS:
        raise Http404
    qs = places_for(request.user)
    ids = [int(i) for i in request.GET.get("ids", "").split(",") if i.strip().isdigit()]
    if ids:
        qs = qs.filter(pk__in=ids)
    fn, ctype = EXPORTERS[fmt]
    resp = HttpResponse(fn(list(qs[:5000])), content_type=ctype)
    resp["Content-Disposition"] = f'attachment; filename="meltice-places-{timezone.localtime():%Y%m%d-%H%M}.{fmt}"'
    return resp


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
def _ok(lat, lon):
    return -90 <= lat <= 90 and -180 <= lon <= 180 and not (lat == 0 and lon == 0)


def parse_kml(data: bytes):
    if b"<!DOCTYPE" in data[:2048].upper() or b"<!ENTITY" in data.upper():
        raise ValueError("KML files with DOCTYPE / ENTITY declarations are not accepted.")
    root = ET.fromstring(data)
    rows = []
    for pm in root.iter():
        if not pm.tag.endswith("Placemark"):
            continue
        name = desc = ""
        point = None
        for el in pm.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "name" and not name:
                name = (el.text or "").strip()
            elif tag == "description" and not desc:
                desc = (el.text or "").strip()
            elif tag == "Point":
                for c in el.iter():
                    if c.tag.endswith("coordinates") and c.text:
                        lon, lat = [float(x) for x in c.text.strip().split(",")[:2]]
                        point = (lat, lon)
        if point:
            rows.append({"name": name, "notes": desc, "lat": point[0], "lon": point[1]})
    return rows


def parse_geojson(data: bytes):
    doc = json.loads(data.decode("utf-8-sig"))
    feats = doc.get("features", []) if doc.get("type") == "FeatureCollection" else [doc]
    rows = []
    for f in feats:
        g = f.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        lon, lat = float(g["coordinates"][0]), float(g["coordinates"][1])
        props = f.get("properties") or {}
        rows.append({"name": str(props.get("name") or ""), "notes": str(props.get("notes") or props.get("description") or ""),
                     "address": str(props.get("address") or ""), "lat": lat, "lon": lon})
    return rows


def parse_csv(data: bytes):
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}

    def col(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None
    lat_c, lon_c = col("latitude", "lat", "y"), col("longitude", "lon", "lng", "long", "x")
    if not lat_c or not lon_c:
        raise ValueError("The CSV needs latitude and longitude columns.")
    name_c, addr_c, notes_c = col("name", "title"), col("address", "location"), col("notes", "description")
    rows = []
    for r in reader:
        try:
            lat, lon = float(r[lat_c]), float(r[lon_c])
        except (TypeError, ValueError):
            continue
        rows.append({"name": (r.get(name_c) or "") if name_c else "", "address": (r.get(addr_c) or "") if addr_c else "",
                     "notes": (r.get(notes_c) or "") if notes_c else "", "lat": lat, "lon": lon})
    return rows


@login_required
@require_POST
def places_import(request):
    f = request.FILES.get("file")
    if not f:
        messages.error(request, "Choose a KML, CSV or GeoJSON file to import.")
        return redirect("places")
    if f.size > MAX_IMPORT_BYTES:
        messages.error(request, "That file is larger than 2 MB.")
        return redirect("places")
    data = f.read()
    ext = f.name.lower().rsplit(".", 1)[-1]
    try:
        if ext == "kml":
            rows = parse_kml(data)
        elif ext in ("geojson", "json"):
            rows = parse_geojson(data)
        elif ext == "csv":
            rows = parse_csv(data)
        else:
            raise ValueError("Use a .kml, .csv or .geojson file. For a .kmz from Google, unzip it first or export as KML.")
    except (ValueError, KeyError, IndexError, TypeError, ET.ParseError, json.JSONDecodeError) as e:
        messages.error(request, f"Could not read {f.name}: {e}")
        return redirect("places")
    objs = []
    for r in rows[:MAX_IMPORT_ROWS]:
        if not _ok(r["lat"], r["lon"]):
            continue
        objs.append(SavedPlace(owner=request.user, kind="import", source="file",
                               name=(r.get("name") or "").strip()[:120] or default_name("import"),
                               notes=(r.get("notes") or "")[:2000], address=(r.get("address") or "")[:300],
                               latitude=r["lat"], longitude=r["lon"]))
    SavedPlace.objects.bulk_create(objs)
    if objs:
        messages.success(request, f"Imported {len(objs)} place{'s' if len(objs) != 1 else ''} from {f.name}.")
    else:
        messages.warning(request, f"No points with valid coordinates were found in {f.name}.")
    return redirect(reverse("places") + "?kind=import")


# ---------------------------------------------------------------------------
# Reverse geocoding (place name)
# ---------------------------------------------------------------------------
def _short_address(doc: dict) -> str:
    a = doc.get("address") or {}
    parts = [a.get("road") or a.get("pedestrian") or a.get("neighbourhood"),
             a.get("suburb") or a.get("quarter") or a.get("village") or a.get("town"),
             a.get("city") or a.get("state")]
    seen, out = set(), []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return ", ".join(out) or doc.get("display_name", "")


def reverse_geocode(lat: float, lon: float) -> dict | None:
    key = f"geo:{lat:.4f},{lon:.4f}"
    hit = cache.get(key)
    if hit is not None:
        return hit
    # Nominatim's usage policy: at most one request per second for the whole site.
    if not cache.add("geo:lock", 1, timeout=1):
        return None
    qs = urllib.parse.urlencode({"format": "jsonv2", "lat": f"{lat:.5f}", "lon": f"{lon:.5f}", "zoom": 17,
                                 "addressdetails": 1, "accept-language": "en"})
    req = urllib.request.Request(f"{NOMINATIM_URL}?{qs}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:
            doc = json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 - a lookup failure only means no place name
        log.warning("reverse geocode failed: %s", e)
        return None
    out = {"name": _short_address(doc), "full": doc.get("display_name", "")}
    cache.set(key, out, 60 * 60 * 24 * 30)
    return out


@login_required
@require_GET
def geocode(request):
    try:
        lat, lon = float(request.GET["lat"]), float(request.GET["lon"])
    except (KeyError, ValueError):
        return JsonResponse({"error": "lat and lon are required"}, status=400)
    if not _ok(lat, lon) or math.isnan(lat) or math.isnan(lon):
        return JsonResponse({"error": "out of range"}, status=400)
    res = reverse_geocode(lat, lon)
    if res is None:
        return JsonResponse({"name": "", "retry": True}, status=503)
    return JsonResponse(res)
