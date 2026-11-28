"""
Fleet registry (FleetConnect 360 FLT-01, FLT-05, IVM-02): vehicle details, drivers, driver-to-vehicle assignments and
vehicle / driver documents with expiry reminders.

Pages (everyone signed in reads; staff edit)
* /fleet/                              Vehicles · Drivers · Documents · Assignments tabs
* /fleet/vehicles/<id>/                vehicle registry details, current driver, documents, recent journeys + driver
* /fleet/drivers/new/ · /<id>/         add / edit a driver, with documents and assignment history
* /fleet/documents/new/ · /<id>/       add / edit a document (?device=<id> or ?driver=<id> to pre-fill)
* /fleet/assign/                       POST device, driver (blank driver = unassign)
* /fleet/documents.csv                 all documents with their expiry state

Driver identification: when a record carries an iButton / driver card ID (AVL 78) that matches a driver, the vehicle
is assigned to that driver from that moment (identify_from_records, called by the ingest pipeline). Journeys that no
assignment or iButton covers are shown as "Unassigned".

Reminders: `manage.py fleet_reminders` (daily, from cron) sends one WhatsApp message listing the documents that are
expired or inside their reminder window. A document is repeated at most once a week.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, time, timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from rest_framework import permissions, serializers, viewsets

from .models import AVLRecord, Device, Driver, DriverAssignment, FleetDocument, IOValue

log = logging.getLogger("fleet.registry")
staff_required = user_passes_test(lambda u: u.is_active and u.is_staff)
IBUTTON = 78
TOTAL_ODOMETER = 16


# ---------------------------------------------------------------------------
# Assignments and driver identification
# ---------------------------------------------------------------------------
def current_assignment(device) -> DriverAssignment | None:
    return (DriverAssignment.objects.filter(device=device, end_utc__isnull=True)
            .select_related("driver").order_by("-start_utc").first())


def current_drivers() -> dict[int, DriverAssignment]:
    """device id -> open assignment."""
    out = {}
    for a in DriverAssignment.objects.filter(end_utc__isnull=True).select_related("driver").order_by("start_utc"):
        out[a.device_id] = a
    return out


@transaction.atomic
def assign(device, driver, source="manual", user="", when=None, notes="") -> DriverAssignment | None:
    """Make driver the current driver of device from `when` (default now). driver=None just ends the assignment.
    A driver drives one vehicle at a time, so their open assignment on another vehicle is closed too."""
    when = when or timezone.now()
    open_qs = DriverAssignment.objects.select_for_update().filter(device=device, end_utc__isnull=True)
    cur = open_qs.order_by("-start_utc").first()
    if driver is not None and cur and cur.driver_id == driver.pk:
        return cur
    open_qs.filter(start_utc__lte=when).update(end_utc=when)
    open_qs.filter(start_utc__gt=when).update(end_utc=None)  # a later assignment stays current
    if driver is None:
        return None
    DriverAssignment.objects.filter(driver=driver, end_utc__isnull=True, start_utc__lte=when).exclude(device=device).update(end_utc=when)
    return DriverAssignment.objects.create(driver=driver, device=device, start_utc=when, source=source,
                                           created_by=user[:150], notes=notes[:200])


def _ibutton_keys(text: str) -> set[int]:
    """A driver card ID as typed (decimal or HEX, with or without separators) -> the integers it can mean."""
    t = "".join(ch for ch in (text or "") if ch.isalnum()).upper()
    keys = set()
    if not t:
        return keys
    if t.isdigit():
        keys.add(int(t))
    try:
        keys.add(int(t, 16))
    except ValueError:
        pass
    keys.discard(0)
    return keys


def driver_for_ibutton(value) -> Driver | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if not v:
        return None
    for d in Driver.objects.filter(active=True).exclude(ibutton_id=""):
        if v in _ibutton_keys(d.ibutton_id):
            return d
    return None


def identify_from_records(device_id: int, recs: list[dict]) -> int:
    """Start assignments from iButton IDs in new records. recs are the ingest pipeline's rule records
    ({"ts", "io": {avl_id: value}}). Never raises. Returns the number of assignments started."""
    try:
        keyed = sorted((r for r in recs if r.get("io", {}).get(IBUTTON)), key=lambda r: r["ts"])
        if not keyed:
            return 0
        device = Device.objects.get(pk=device_id)
        started, seen = 0, {}
        for r in keyed:
            v = r["io"][IBUTTON]
            if v not in seen:
                seen[v] = driver_for_ibutton(v)
            drv = seen[v]
            if drv is None:
                continue
            cur = current_assignment(device)
            if cur and (cur.driver_id == drv.pk or cur.start_utc > r["ts"]):
                continue
            assign(device, drv, source="ibutton", when=r["ts"], notes=f"iButton {v}")
            started += 1
        return started
    except Exception:  # never break ingestion
        log.exception("driver identification failed for device %s", device_id)
        return 0


def driver_at(assignments: list[DriverAssignment], ts) -> Driver | None:
    for a in assignments:
        if a.start_utc <= ts and (a.end_utc is None or a.end_utc > ts):
            return a.driver
    return None


# ---------------------------------------------------------------------------
# Odometer
# ---------------------------------------------------------------------------
def tracker_odometer_km(device) -> float | None:
    """Total odometer from the tracker (AVL 16; the IO dictionary already scales it to km) when it sends one."""
    v = (IOValue.objects.filter(device=device, avl_id=TOTAL_ODOMETER, value__gt=0)
         .order_by("-timestamp_utc").values_list("value", flat=True).first())
    return None if v is None else round(v, 1)


def gps_km_since(device, since) -> float:
    from .driverscore import _hav_km
    km, prev = 0.0, None
    qs = (AVLRecord.objects.filter(device=device, timestamp_utc__gte=since, valid_fix=True)
          .order_by("timestamp_utc", "id").values_list("timestamp_utc", "latitude", "longitude"))
    for t, lat, lon in qs.iterator(chunk_size=5000):
        if prev is not None:
            d = _hav_km(prev[1], prev[2], lat, lon)
            dt = (t - prev[0]).total_seconds()
            if d < 5 or (dt > 0 and d / (dt / 3600) < 250):  # drop GNSS jumps
                km += d
        prev = (t, lat, lon)
    return km


def odometer(device) -> dict:
    trk = tracker_odometer_km(device)
    if trk is not None:
        return {"km": trk, "source": "tracker", "label": "From the tracker (total odometer)"}
    if device.odometer_km is None:
        return {"km": None, "source": "none", "label": "Not set"}
    if not device.odometer_date:
        return {"km": round(device.odometer_km, 1), "source": "manual", "label": "Entered reading"}
    since = timezone.make_aware(datetime.combine(device.odometer_date, time.min))
    add = gps_km_since(device, since)
    return {"km": round(device.odometer_km + add, 1), "source": "estimate",
            "label": f"{device.odometer_km:,.0f} km on {device.odometer_date:%d %b %Y} + {add:,.1f} km GPS distance"}


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
def due_documents(today=None):
    today = today or timezone.localdate()
    out = []
    for d in FleetDocument.objects.filter(expiry__isnull=False).select_related("device", "driver"):
        if d.state(today) in ("expired", "due"):
            out.append(d)
    return out


def reminder_text(docs, today) -> str:
    lines = ["Fleet documents needing attention:"]
    for d in docs:
        left = d.days_left(today)
        when = f"expired {-left} day(s) ago" if left < 0 else ("expires today" if left == 0 else f"expires in {left} day(s)")
        num = f" {d.number}" if d.number else ""
        lines.append(f"- {d.owner_name or 'Unlinked'}: {d.get_kind_display()}{num} {when} ({d.expiry:%d %b %Y})")
    return "\n".join(lines)


def send_reminders(today=None, dry_run=False) -> dict:
    today = today or timezone.localdate()
    docs = [d for d in due_documents(today)
            if not d.last_reminder_on or (today - d.last_reminder_on).days >= 7]
    if not docs:
        return {"count": 0, "detail": "Nothing due."}
    text = reminder_text(docs, today)
    if dry_run:
        return {"count": len(docs), "detail": text}
    from . import whatsapp
    res = whatsapp.send(text, None, subject="Document expiry")
    if res.get("sent"):
        FleetDocument.objects.filter(pk__in=[d.pk for d in docs]).update(last_reminder_on=today)
    return {"count": len(docs), "detail": res.get("detail", ""), "sent": bool(res.get("sent"))}


# ---------------------------------------------------------------------------
# Forms
# ---------------------------------------------------------------------------
class _Styled:
    def _style(self):
        for f in self.fields.values():
            w = f.widget
            if isinstance(w, (forms.CheckboxInput, forms.CheckboxSelectMultiple)):
                w.attrs["class"] = "form-check-input"
            elif isinstance(w, forms.Select):
                w.attrs.setdefault("class", "form-select form-select-sm")
            else:
                w.attrs.setdefault("class", "form-control form-control-sm")


class VehicleForm(_Styled, forms.ModelForm):
    class Meta:
        model = Device
        fields = ["name", "plate", "group", "vehicle_type", "make_model", "year", "vin", "capacity", "fuel_type",
                  "odometer_km", "odometer_date", "sim_number", "sim_iccid", "availability", "notes"]
        labels = {"plate": "Registration / plate", "name": "Vehicle name"}
        widgets = {"odometer_date": forms.DateInput(attrs={"type": "date"}), "notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._style()


class DriverForm(_Styled, forms.ModelForm):
    class Meta:
        model = Driver
        fields = ["name", "employee_id", "phone", "email", "licence_number", "licence_class", "ibutton_id", "user",
                  "active", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["user"].required = False
        self._style()

    def clean_ibutton_id(self):
        v = (self.cleaned_data.get("ibutton_id") or "").strip()
        if v and not _ibutton_keys(v):
            raise forms.ValidationError("Use the ID as the tracker shows it: digits or HEX (0-9, A-F).")
        if v:
            mine = _ibutton_keys(v)
            for d in Driver.objects.exclude(pk=self.instance.pk).exclude(ibutton_id=""):
                if mine & _ibutton_keys(d.ibutton_id):
                    raise forms.ValidationError(f"{d.name} already uses this ID.")
        return v


class DocumentForm(_Styled, forms.ModelForm):
    class Meta:
        model = FleetDocument
        fields = ["kind", "title", "number", "device", "driver", "issuer", "issued", "expiry", "remind_days", "link", "notes"]
        widgets = {"issued": forms.DateInput(attrs={"type": "date"}), "expiry": forms.DateInput(attrs={"type": "date"}),
                   "notes": forms.Textarea(attrs={"rows": 2})}
        labels = {"device": "Vehicle", "driver": "Driver"}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["device"].queryset = Device.objects.exclude(model="SIMULATOR")
        self.fields["device"].empty_label = "—"
        self.fields["driver"].empty_label = "—"
        self._style()

    def clean(self):
        c = super().clean()
        if not c.get("device") and not c.get("driver"):
            raise forms.ValidationError("Pick the vehicle or the driver this document belongs to.")
        if c.get("device") and c.get("driver"):
            raise forms.ValidationError("A document belongs to a vehicle or a driver, not both.")
        if c.get("issued") and c.get("expiry") and c["expiry"] < c["issued"]:
            self.add_error("expiry", "Expiry is before the issue date.")
        return c


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def _vehicles():
    return Device.objects.exclude(model="SIMULATOR")


def _doc_rows(qs, today):
    return [{"d": d, "state": d.state(today), "left": d.days_left(today)} for d in qs]


@login_required
def fleet_page(request):
    today = timezone.localdate()
    cur = current_drivers()
    now = timezone.now()
    docs = list(FleetDocument.objects.select_related("device", "driver"))
    doc_state = {}
    for d in docs:
        s = d.state(today)
        for key in ((d.device_id and ("v", d.device_id)) or None, (d.driver_id and ("d", d.driver_id)) or None):
            if key and s in ("expired", "due"):
                doc_state[key] = "expired" if s == "expired" or doc_state.get(key) == "expired" else "due"
    vehicles = []
    for v in _vehicles():
        a = cur.get(v.pk)
        recent = v.last_record_ts and now - v.last_record_ts < timedelta(minutes=15)
        moving = bool(recent and (v.last_movement or (v.last_fix_speed or 0) > 3))
        vehicles.append({"v": v, "a": a, "moving": moving, "unassigned_driving": moving and not a,
                         "docs": doc_state.get(("v", v.pk))})
    drivers = []
    by_driver = {a.driver_id: a for a in cur.values()}
    for d in Driver.objects.all():
        drivers.append({"d": d, "a": by_driver.get(d.pk), "docs": doc_state.get(("d", d.pk))})
    tab = request.GET.get("tab", "vehicles")
    ctx = {
        "nav": "fleet", "tab": tab, "can_edit": request.user.is_staff, "vehicles": vehicles, "drivers": drivers,
        "documents": _doc_rows(sorted(docs, key=lambda d: (d.expiry is None, d.expiry or today)), today),
        "due_count": sum(1 for d in docs if d.state(today) in ("expired", "due")),
        "assignments": DriverAssignment.objects.select_related("driver", "device")[:200],
        "all_drivers": Driver.objects.filter(active=True),
    }
    return render(request, "tracking/fleet.html", ctx)


@login_required
def vehicle_page(request, pk):
    v = get_object_or_404(Device, pk=pk)
    form = VehicleForm(request.POST or None, instance=v)
    if request.method == "POST":
        if not request.user.is_staff:
            return HttpResponse(status=403)
        if form.is_valid():
            form.save()
            messages.success(request, f"{v.name} saved.")
            return redirect("fleet_vehicle", pk=v.pk)
    today = timezone.localdate()
    end = timezone.now()
    start = end - timedelta(days=7)
    journeys = []
    try:
        from . import driverscore as ds
        trips, _ = ds.vehicle_trips(v, start, end, ds.get_config())
        assigns = list(DriverAssignment.objects.filter(device=v, start_utc__lt=end)
                       .exclude(end_utc__lt=start).select_related("driver"))
        for tr in reversed(trips[-30:]):
            drv = driver_at(assigns, tr.start) or (driver_for_ibutton(int(tr.driver_ids[0], 16)) if tr.driver_ids else None)
            journeys.append({"t": tr, "driver": drv, "km": round(tr.m.km, 1), "dur": ds._fmt_dur(tr.m.duration_s)})
    except Exception:
        log.exception("journeys for %s", v.pk)
    ctx = {"nav": "fleet", "v": v, "form": form, "can_edit": request.user.is_staff, "odo": odometer(v),
           "current": current_assignment(v), "all_drivers": Driver.objects.filter(active=True),
           "documents": _doc_rows(v.documents.all(), today),
           "history": v.assignments.select_related("driver")[:30], "journeys": journeys,
           "unassigned": sum(1 for j in journeys if not j["driver"])}
    return render(request, "tracking/fleet_vehicle.html", ctx)


@login_required
def driver_page(request, pk=None):
    d = get_object_or_404(Driver, pk=pk) if pk else None
    form = DriverForm(request.POST or None, instance=d)
    if request.method == "POST":
        if not request.user.is_staff:
            return HttpResponse(status=403)
        if form.is_valid():
            d = form.save()
            messages.success(request, f"{d.name} saved.")
            return redirect("fleet_driver", pk=d.pk)
    today = timezone.localdate()
    ctx = {"nav": "fleet", "d": d, "form": form, "can_edit": request.user.is_staff,
           "documents": _doc_rows(d.documents.all(), today) if d else [],
           "history": d.assignments.select_related("device")[:50] if d else [],
           "current": d.assignments.filter(end_utc__isnull=True).select_related("device").first() if d else None}
    return render(request, "tracking/fleet_driver.html", ctx)


@login_required
def document_page(request, pk=None):
    doc = get_object_or_404(FleetDocument, pk=pk) if pk else None
    initial = {}
    if not doc:
        for k in ("device", "driver"):
            if request.GET.get(k, "").isdigit():
                initial[k] = int(request.GET[k])
        if "driver" in initial:
            initial["kind"] = "licence"
    form = DocumentForm(request.POST or None, instance=doc, initial=initial)
    if request.method == "POST":
        if not request.user.is_staff:
            return HttpResponse(status=403)
        if request.POST.get("delete") and doc:
            back = _doc_back(doc)
            doc.delete()
            messages.success(request, "Document deleted.")
            return redirect(back)
        if form.is_valid():
            doc = form.save(commit=False)
            if doc.pk and "expiry" in form.changed_data:
                doc.last_reminder_on = None
            doc.save()
            messages.success(request, "Document saved.")
            return redirect(_doc_back(doc))
    return render(request, "tracking/fleet_document.html", {"nav": "fleet", "doc": doc, "form": form,
                                                             "can_edit": request.user.is_staff})


def _doc_back(doc):
    if doc.device_id:
        return f"/fleet/vehicles/{doc.device_id}/"
    if doc.driver_id:
        return f"/fleet/drivers/{doc.driver_id}/"
    return "/fleet/?tab=documents"


@require_POST
@staff_required
def assign_view(request):
    device = get_object_or_404(Device, pk=request.POST.get("device"))
    drv_id = request.POST.get("driver") or ""
    driver = get_object_or_404(Driver, pk=drv_id) if drv_id else None
    assign(device, driver, source="manual", user=request.user.get_username())
    messages.success(request, f"{device.name}: {'driver set to ' + driver.name if driver else 'driver unassigned'}.")
    nxt = request.POST.get("next") or "/fleet/"
    return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/fleet/")


@login_required
def documents_csv(request):
    today = timezone.localdate()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Belongs to", "Type", "Kind", "Title", "Number", "Issuer", "Issued", "Expiry", "Days left", "State", "Link"])
    for d in FleetDocument.objects.select_related("device", "driver"):
        w.writerow([d.owner_name, "Vehicle" if d.device_id else "Driver", d.get_kind_display(), d.title, d.number,
                    d.issuer, d.issued or "", d.expiry or "", "" if d.days_left(today) is None else d.days_left(today),
                    d.state(today), d.link])
    resp = HttpResponse("﻿" + buf.getvalue(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="fleet-documents-{today:%Y%m%d}.csv"'
    return resp


# ---------------------------------------------------------------------------
# REST API  (/api/v1/drivers/, /api/v1/assignments/, /api/v1/documents/)
# ---------------------------------------------------------------------------
class StaffWriteOrReadOnly(permissions.BasePermission):
    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        return request.method in permissions.SAFE_METHODS or request.user.is_staff


class DriverSerializer(serializers.ModelSerializer):
    current_vehicle = serializers.SerializerMethodField()

    class Meta:
        model = Driver
        fields = ["id", "name", "employee_id", "phone", "email", "licence_number", "licence_class", "ibutton_id",
                  "active", "notes", "created_utc", "current_vehicle"]
        read_only_fields = ["created_utc"]

    def get_current_vehicle(self, obj) -> dict | None:
        a = obj.assignments.filter(end_utc__isnull=True).select_related("device").first()
        return {"id": a.device_id, "name": a.device.name, "since": a.start_utc} if a else None


class AssignmentSerializer(serializers.ModelSerializer):
    driver_name = serializers.CharField(source="driver.name", read_only=True)
    vehicle_name = serializers.CharField(source="device.name", read_only=True)

    class Meta:
        model = DriverAssignment
        fields = ["id", "driver", "driver_name", "device", "vehicle_name", "start_utc", "end_utc", "source", "notes", "created_by"]
        read_only_fields = ["end_utc", "source", "created_by"]

    def create(self, data):
        req = self.context.get("request")
        return assign(data["device"], data["driver"], source="api",
                      user=req.user.get_username() if req else "", when=data.get("start_utc"), notes=data.get("notes", ""))


class DocumentSerializer(serializers.ModelSerializer):
    state = serializers.SerializerMethodField()
    days_left = serializers.SerializerMethodField()

    class Meta:
        model = FleetDocument
        fields = ["id", "kind", "title", "number", "device", "driver", "issuer", "issued", "expiry", "remind_days",
                  "link", "notes", "state", "days_left"]

    def get_state(self, obj) -> str:
        return obj.state()

    def get_days_left(self, obj) -> int | None:
        return obj.days_left()

    def validate(self, data):
        device = data.get("device", getattr(self.instance, "device", None))
        driver = data.get("driver", getattr(self.instance, "driver", None))
        if bool(device) == bool(driver):
            raise serializers.ValidationError("Set exactly one of device or driver.")
        return data


class DriverViewSet(viewsets.ModelViewSet):
    """Drivers in the fleet registry."""
    queryset = Driver.objects.all()
    serializer_class = DriverSerializer
    permission_classes = [StaffWriteOrReadOnly]
    pagination_class = None


class AssignmentViewSet(viewsets.ModelViewSet):
    """Driver-to-vehicle assignments. POST {driver, device[, start_utc]} makes that driver current on the vehicle.
    ?device=<id> or ?driver=<id> filters; ?current=1 lists only the open ones."""
    serializer_class = AssignmentSerializer
    permission_classes = [StaffWriteOrReadOnly]
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        qs = DriverAssignment.objects.select_related("driver", "device")
        p = self.request.query_params
        if p.get("device", "").isdigit():
            qs = qs.filter(device_id=p["device"])
        if p.get("driver", "").isdigit():
            qs = qs.filter(driver_id=p["driver"])
        if p.get("current") in ("1", "true"):
            qs = qs.filter(end_utc__isnull=True)
        return qs


class DocumentViewSet(viewsets.ModelViewSet):
    """Vehicle and driver documents. ?due=1 lists only expired or due-soon ones."""
    serializer_class = DocumentSerializer
    permission_classes = [StaffWriteOrReadOnly]
    pagination_class = None

    def get_queryset(self):
        qs = FleetDocument.objects.select_related("device", "driver")
        if self.action == "list" and self.request.query_params.get("due") in ("1", "true"):
            return [d for d in qs if d.state() in ("expired", "due")]
        return qs
