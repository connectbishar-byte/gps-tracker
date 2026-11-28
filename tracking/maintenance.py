"""
Maintenance, fuel and costs (FleetConnect 360 FLT-03, FLT-04) and fleet reports (FLT-06).

Pages (everyone signed in reads; staff edit)
* /maintenance/                         Due · Plans · Work orders · Expenses · Fuel tabs
* /maintenance/plans/new/ · /<id>/      service plan: every N km and/or N days, last done date / km
* /maintenance/orders/new/ · /<id>/     work order; marking a planned service Done moves the plan's "last done"
* /maintenance/expenses/new/ · /<id>/   expense (fuel with litres and odometer gives consumption)
* /reports/                             trips, utilization, mileage, idle, costs, maintenance and device
                                        availability for a date range, each as a table and a CSV download

Due: a plan is "due" inside its reminder window (km or days before) and "overdue" past it. The current odometer is
fleet.odometer() (tracker AVL 16, or the entered reading plus GPS distance). The receiver sends one WhatsApp
reminder for due plans with the document reminders, at most weekly per plan.

Fuel: L/100 km is measured between full-tank fills with odometer readings. A fill more than 30 % above the vehicle's
median is flagged "check" — a suspected anomaly to look at, never a confirmed theft.
"""
from __future__ import annotations

import csv
import io
import logging
import statistics
from datetime import timedelta
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from rest_framework import permissions, serializers, viewsets

from . import fleet
from .models import AlertEvent, Device, Driver, Expense, Incident, MaintenancePlan, WorkOrder

log = logging.getLogger("fleet.maintenance")
ANOMALY_FACTOR = 1.3


# ---------------------------------------------------------------------------
# Due calculation
# ---------------------------------------------------------------------------
def plan_status(plan: MaintenancePlan, odo_km, today=None) -> dict:
    today = today or timezone.localdate()
    out = {"state": "ok", "next_km": None, "km_left": None, "next_date": None, "days_left": None, "why": []}
    if plan.every_km and plan.last_done_km is not None:
        out["next_km"] = plan.last_done_km + plan.every_km
        if odo_km is not None:
            out["km_left"] = round(out["next_km"] - odo_km)
    if plan.every_days and plan.last_done_date:
        out["next_date"] = plan.last_done_date + timedelta(days=plan.every_days)
        out["days_left"] = (out["next_date"] - today).days
    states = []
    if out["km_left"] is not None:
        states.append("overdue" if out["km_left"] < 0 else ("due" if out["km_left"] <= plan.remind_km else "ok"))
    if out["days_left"] is not None:
        states.append("overdue" if out["days_left"] < 0 else ("due" if out["days_left"] <= plan.remind_days else "ok"))
    if not states:
        out["state"] = "unknown"  # never done / no odometer: we can't tell
    else:
        out["state"] = "overdue" if "overdue" in states else ("due" if "due" in states else "ok")
    return out


def plans_with_status(qs=None, today=None):
    qs = qs if qs is not None else MaintenancePlan.objects.filter(active=True)
    odo_cache, rows = {}, []
    for p in qs.select_related("device"):
        if p.device_id not in odo_cache:
            try:
                odo_cache[p.device_id] = fleet.odometer(p.device)["km"]
            except Exception:
                odo_cache[p.device_id] = None
        rows.append({"p": p, "odo": odo_cache[p.device_id], **plan_status(p, odo_cache[p.device_id], today)})
    order = {"overdue": 0, "due": 1, "unknown": 2, "ok": 3}
    rows.sort(key=lambda r: (order[r["state"]], r["days_left"] if r["days_left"] is not None else 99999))
    return rows


def reminder_lines(today=None) -> tuple[list[str], list[int]]:
    today = today or timezone.localdate()
    lines, ids = [], []
    for r in plans_with_status(today=today):
        p = r["p"]
        if r["state"] not in ("due", "overdue"):
            continue
        if p.last_reminder_on and (today - p.last_reminder_on).days < 7:
            continue
        bits = []
        if r["km_left"] is not None:
            bits.append(f"{-r['km_left']:,} km overdue" if r["km_left"] < 0 else f"in {r['km_left']:,} km")
        if r["days_left"] is not None:
            bits.append(f"{-r['days_left']} day(s) overdue" if r["days_left"] < 0 else f"in {r['days_left']} day(s)")
        lines.append(f"- {p.device.name}: {p.name} {', '.join(bits)}")
        ids.append(p.pk)
    return lines, ids


def send_reminders(today=None, dry_run=False) -> dict:
    today = today or timezone.localdate()
    lines, ids = reminder_lines(today)
    if not lines:
        return {"count": 0, "detail": "Nothing due."}
    text = "Vehicle service due:\n" + "\n".join(lines)
    if dry_run:
        return {"count": len(ids), "detail": text}
    from . import whatsapp
    res = whatsapp.send(text, None, subject="Service due")
    if res.get("sent"):
        MaintenancePlan.objects.filter(pk__in=ids).update(last_reminder_on=today)
    return {"count": len(ids), "detail": res.get("detail", ""), "sent": bool(res.get("sent"))}


# ---------------------------------------------------------------------------
# Fuel consumption
# ---------------------------------------------------------------------------
def fuel_rows(device=None, start=None, end=None):
    """Full-tank fills with L/100 km since the previous full fill (litres of any partial fills in between count)."""
    qs = Expense.objects.filter(category="fuel").select_related("device", "driver").order_by("device_id", "date", "id")
    if device is not None:
        qs = qs.filter(device=device)
    by_dev, rows = {}, []
    for e in qs:
        st = by_dev.setdefault(e.device_id, {"prev_km": None, "litres": 0.0, "rates": []})
        row = {"e": e, "km": None, "l100": None, "flag": False}
        st["litres"] += e.litres or 0
        if e.full_tank and e.odometer_km is not None:
            if st["prev_km"] is not None and e.odometer_km > st["prev_km"] and st["litres"] > 0:
                row["km"] = round(e.odometer_km - st["prev_km"], 1)
                row["l100"] = round(st["litres"] / row["km"] * 100, 1)
                st["rates"].append(row["l100"])
            st["prev_km"], st["litres"] = e.odometer_km, 0.0
        row["_rates"] = st["rates"]
        rows.append(row)
    for r in rows:
        rates = r.pop("_rates")
        if r["l100"] is not None and len(rates) >= 3:
            med = statistics.median(rates)
            r["median"] = med
            r["flag"] = r["l100"] > med * ANOMALY_FACTOR
    if start is not None:
        rows = [r for r in rows if start <= r["e"].date <= end]
    rows.reverse()
    return rows


# ---------------------------------------------------------------------------
# Forms
# ---------------------------------------------------------------------------
class PlanForm(fleet._Styled, forms.ModelForm):
    class Meta:
        model = MaintenancePlan
        fields = ["device", "name", "every_km", "every_days", "last_done_date", "last_done_km", "remind_km",
                  "remind_days", "active", "notes"]
        labels = {"device": "Vehicle"}
        widgets = {"last_done_date": forms.DateInput(attrs={"type": "date"}), "notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["device"].queryset = Device.objects.exclude(model="SIMULATOR")
        self._style()

    def clean(self):
        c = super().clean()
        if not c.get("every_km") and not c.get("every_days"):
            raise forms.ValidationError("Set every N km, every N days, or both.")
        return c


class WorkOrderForm(fleet._Styled, forms.ModelForm):
    class Meta:
        model = WorkOrder
        fields = ["device", "plan", "title", "status", "scheduled", "started", "completed", "odometer_km", "vendor",
                  "cost", "downtime_hours", "reference", "notes"]
        labels = {"device": "Vehicle", "plan": "Service plan (optional)"}
        widgets = {k: forms.DateInput(attrs={"type": "date"}) for k in ("scheduled", "started", "completed")} | {
            "notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["device"].queryset = Device.objects.exclude(model="SIMULATOR")
        self.fields["plan"].queryset = MaintenancePlan.objects.select_related("device")
        self._style()

    def clean(self):
        c = super().clean()
        if c.get("plan") and c.get("device") and c["plan"].device_id != c["device"].pk:
            self.add_error("plan", "That plan belongs to another vehicle.")
        if c.get("status") == "done" and not c.get("completed"):
            c["completed"] = timezone.localdate()
        return c


class ExpenseForm(fleet._Styled, forms.ModelForm):
    class Meta:
        model = Expense
        fields = ["device", "driver", "date", "category", "amount", "litres", "full_tank", "odometer_km", "vendor",
                  "reference", "notes"]
        labels = {"device": "Vehicle", "amount": "Amount (AED)"}
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["device"].queryset = Device.objects.exclude(model="SIMULATOR")
        self.fields["driver"].queryset = Driver.objects.filter(active=True)
        self._style()

    def clean(self):
        c = super().clean()
        if c.get("category") == "fuel" and not c.get("litres"):
            self.add_error("litres", "Enter the litres for a fuel expense.")
        return c


def apply_done(wo: WorkOrder) -> None:
    """A finished planned service moves its plan's last-done date / km."""
    if wo.status != "done" or not wo.plan_id:
        return
    p = wo.plan
    if wo.completed and (not p.last_done_date or wo.completed >= p.last_done_date):
        p.last_done_date = wo.completed
        if wo.odometer_km is not None:
            p.last_done_km = wo.odometer_km
        p.last_reminder_on = None
        p.save()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@login_required
def maintenance_page(request):
    today = timezone.localdate()
    plans = plans_with_status(MaintenancePlan.objects.all(), today)
    month = today.replace(day=1)
    costs = (Expense.objects.filter(date__gte=month).values("category").annotate(total=Sum("amount")).order_by("-total"))
    cat = dict(Expense.CATEGORIES)
    ctx = {
        "nav": "maintenance", "tab": request.GET.get("tab", "due"), "can_edit": request.user.is_staff,
        "due": [r for r in plans if r["state"] in ("due", "overdue") and r["p"].active], "plans": plans,
        "orders": WorkOrder.objects.select_related("device", "plan")[:300],
        "expenses": Expense.objects.select_related("device", "driver")[:300],
        "fuel": fuel_rows()[:300],
        "month_costs": [{"label": cat.get(c["category"], c["category"]), "total": c["total"]} for c in costs],
        "month_total": sum((c["total"] for c in costs), Decimal("0")),
        "open_orders": WorkOrder.objects.filter(status__in=["planned", "in_progress"]).count(),
    }
    return render(request, "tracking/maintenance.html", ctx)


def _edit(request, model, form_cls, pk, title, tab, initial_from_get=("device",)):
    obj = get_object_or_404(model, pk=pk) if pk else None
    initial = {}
    if not obj:
        for k in initial_from_get:
            if request.GET.get(k, "").isdigit():
                initial[k] = int(request.GET[k])
    form = form_cls(request.POST or None, instance=obj, initial=initial)
    if request.method == "POST":
        if not request.user.is_staff:
            return HttpResponse(status=403)
        if request.POST.get("delete") and obj:
            obj.delete()
            messages.success(request, "Deleted.")
            return redirect(f"/maintenance/?tab={tab}")
        if form.is_valid():
            o = form.save(commit=False)
            if hasattr(o, "created_by") and not o.pk:
                o.created_by = request.user.get_username()
            o.save()
            if isinstance(o, WorkOrder):
                apply_done(o)
            messages.success(request, "Saved.")
            return redirect(f"/maintenance/?tab={tab}")
    return render(request, "tracking/maintenance_form.html", {"nav": "maintenance", "form": form, "obj": obj,
                                                              "title": title, "tab": tab, "can_edit": request.user.is_staff})


@login_required
def plan_edit(request, pk=None):
    return _edit(request, MaintenancePlan, PlanForm, pk, "Service plan", "plans")


@login_required
def order_edit(request, pk=None):
    return _edit(request, WorkOrder, WorkOrderForm, pk, "Work order", "orders", ("device", "plan"))


@login_required
def expense_edit(request, pk=None):
    return _edit(request, Expense, ExpenseForm, pk, "Expense", "expenses")


# ---------------------------------------------------------------------------
# Reports (FLT-06)
# ---------------------------------------------------------------------------
REPORTS = [("utilization", "Utilization, mileage and idle time"), ("trips", "Trips"), ("costs", "Costs"),
           ("maintenance", "Maintenance"), ("availability", "Device availability")]


def _hours(s):
    return round((s or 0) / 3600, 2)


def report_data(kind, start, end, d_from, d_to) -> tuple[list[str], list[list]]:
    from . import driverscore as ds
    vehicles = list(Device.objects.exclude(model="SIMULATOR"))
    days = (d_to - d_from).days + 1
    if kind in ("utilization", "trips"):
        cfg = ds.get_config()
        head = (["Vehicle", "Plate", "Trips", "km", "Driving h", "Engine on h", "Idle h", "Idle %", "Days used",
                 "Utilization % (days used)", "Max km/h", "Data"]
                if kind == "utilization" else
                ["Vehicle", "Plate", "Start", "End", "Driver", "km", "Duration h", "Idle h", "Max km/h", "Score"])
        rows = []
        for v in vehicles:
            trips, notes = ds.vehicle_trips(v, start, end, cfg)
            if kind == "trips":
                assigns = list(v.assignments.filter(start_utc__lt=end).exclude(end_utc__lt=start).select_related("driver"))
                for t in trips:
                    drv = fleet.driver_at(assigns, t.start)
                    rows.append([v.name, v.plate, timezone.localtime(t.start).strftime("%Y-%m-%d %H:%M"),
                                 timezone.localtime(t.end).strftime("%Y-%m-%d %H:%M"), drv.name if drv else "Unassigned",
                                 round(t.m.km, 1), _hours(t.m.duration_s),
                                 "" if t.m.idle_s is None else _hours(t.m.idle_s), round(t.m.max_kmh),
                                 ds.score(t.m, cfg)["score"]])
                continue
            tot = ds.Metrics()
            for t in trips:
                tot.add(t.m)
            used = len({timezone.localtime(t.start).date() for t in trips})
            idle_pct = round(100 * (tot.idle_s or 0) / tot.duration_s, 1) if tot.duration_s else ""
            data_note = "no data" if not trips and not notes.get("records") else ("" if trips else "parked all period")
            rows.append([v.name, v.plate, tot.trips, round(tot.km, 1), _hours(tot.moving_s), _hours(tot.duration_s),
                         _hours(tot.idle_s), idle_pct, used, round(100 * used / days, 1), round(tot.max_kmh), data_note])
        return head, rows
    if kind == "costs":
        cats = [c for c, _ in Expense.CATEGORIES]
        head = ["Vehicle", "Plate"] + [l for _, l in Expense.CATEGORIES] + ["Work orders", "Total (AED)", "km", "AED / km"]
        cfg = ds.get_config()
        rows = []
        for v in vehicles:
            by = dict(Expense.objects.filter(device=v, date__gte=d_from, date__lte=d_to).values_list("category")
                      .annotate(t=Sum("amount")))
            wo = WorkOrder.objects.filter(device=v, status="done", completed__gte=d_from, completed__lte=d_to).aggregate(t=Sum("cost"))["t"] or 0
            total = sum(by.values(), Decimal("0")) + Decimal(wo)
            trips, _ = ds.vehicle_trips(v, start, end, cfg)
            km = round(sum(t.m.km for t in trips), 1)
            rows.append([v.name, v.plate] + [by.get(c, 0) for c in cats] + [wo, total, km,
                                                                             round(float(total) / km, 2) if km else ""])
        return head, rows
    if kind == "maintenance":
        head = ["Vehicle", "Work order", "Plan", "Status", "Scheduled", "Completed", "Odometer km", "Vendor", "Cost (AED)",
                "Downtime h", "Ref."]
        rows = [[w.device.name, w.title, w.plan.name if w.plan else "", w.get_status_display(), w.scheduled or "",
                 w.completed or "", w.odometer_km or "", w.vendor, w.cost or "", w.downtime_hours or "", w.reference]
                for w in WorkOrder.objects.select_related("device", "plan").filter(created_utc__lt=end)
                .exclude(completed__lt=d_from).order_by("device__name", "scheduled")]
        return head, rows
    if kind == "availability":
        from .models import AVLRecord
        head = ["Vehicle", "Plate", "Hours with data", "Availability %", "Records", "Last data", "Offline alerts",
                "Power-cut alerts"]
        period_h = days * 24
        rows = []
        for v in vehicles:
            hours = (AVLRecord.objects.filter(device=v, timestamp_utc__gte=start, timestamp_utc__lt=end)
                     .datetimes("timestamp_utc", "hour").count())
            n = AVLRecord.objects.filter(device=v, timestamp_utc__gte=start, timestamp_utc__lt=end).count()
            al = dict(AlertEvent.objects.filter(device=v, kind__in=["offline", "power_loss"], timestamp_utc__gte=start,
                                                timestamp_utc__lt=end).values_list("kind").annotate(n=Count("id")))
            rows.append([v.name, v.plate, hours, round(100 * hours / period_h, 1), n,
                         timezone.localtime(v.last_record_ts).strftime("%Y-%m-%d %H:%M") if v.last_record_ts else "never",
                         al.get("offline", 0), al.get("power_loss", 0)])
        return head, rows
    return [], []


@login_required
def reports_page(request):
    from .driverscore import _range_ctx
    d_from, d_to, start, end, rctx = _range_ctx(request)
    kind = request.GET.get("report", "utilization")
    if kind not in dict(REPORTS):
        kind = "utilization"
    head, rows = report_data(kind, start, end, d_from, d_to)
    if request.GET.get("format") == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([f"{dict(REPORTS)[kind]} · {d_from} to {d_to}"])
        w.writerow(head)
        w.writerows(rows)
        resp = HttpResponse("﻿" + buf.getvalue(), content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = f'attachment; filename="{kind}-{d_from:%Y%m%d}-{d_to:%Y%m%d}.csv"'
        return resp
    return render(request, "tracking/reports.html", {"nav": "reports", "reports": REPORTS, "kind": kind,
                                                     "kind_label": dict(REPORTS)[kind], "head": head, "rows": rows, **rctx})


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------
class StaffWriteOrReadOnly(fleet.StaffWriteOrReadOnly):
    pass


class PlanSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()

    class Meta:
        model = MaintenancePlan
        fields = ["id", "device", "name", "every_km", "every_days", "last_done_date", "last_done_km", "remind_km",
                  "remind_days", "active", "notes", "status"]

    def get_status(self, obj) -> dict:
        s = plan_status(obj, fleet.odometer(obj.device)["km"])
        return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in s.items() if k != "why"}


class WorkOrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkOrder
        fields = ["id", "device", "plan", "title", "status", "scheduled", "started", "completed", "odometer_km", "vendor",
                  "cost", "downtime_hours", "reference", "notes", "created_by", "created_utc"]
        read_only_fields = ["created_by", "created_utc"]

    def save(self, **kw):
        obj = super().save(**kw)
        apply_done(obj)
        return obj


class ExpenseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Expense
        fields = ["id", "device", "driver", "date", "category", "amount", "litres", "full_tank", "odometer_km", "vendor",
                  "reference", "notes", "created_by", "created_utc"]
        read_only_fields = ["created_by", "created_utc"]


class PlanViewSet(viewsets.ModelViewSet):
    """Maintenance plans with their due status (km / days left)."""
    queryset = MaintenancePlan.objects.select_related("device")
    serializer_class = PlanSerializer
    permission_classes = [StaffWriteOrReadOnly]
    pagination_class = None


class WorkOrderViewSet(viewsets.ModelViewSet):
    """Work orders. ?device=<id>, ?status=planned|in_progress|done|cancelled."""
    serializer_class = WorkOrderSerializer
    permission_classes = [StaffWriteOrReadOnly]

    def get_queryset(self):
        qs = WorkOrder.objects.all()
        p = self.request.query_params
        if p.get("device", "").isdigit():
            qs = qs.filter(device_id=p["device"])
        if p.get("status"):
            qs = qs.filter(status=p["status"])
        return qs


class ExpenseViewSet(viewsets.ModelViewSet):
    """Expenses. ?device=<id>, ?category=fuel, ?from=YYYY-MM-DD, ?to=YYYY-MM-DD."""
    serializer_class = ExpenseSerializer
    permission_classes = [StaffWriteOrReadOnly]

    def get_queryset(self):
        qs = Expense.objects.all()
        p = self.request.query_params
        if p.get("device", "").isdigit():
            qs = qs.filter(device_id=p["device"])
        if p.get("category"):
            qs = qs.filter(category=p["category"])
        if p.get("from"):
            qs = qs.filter(date__gte=p["from"])
        if p.get("to"):
            qs = qs.filter(date__lte=p["to"])
        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user.get_username())
