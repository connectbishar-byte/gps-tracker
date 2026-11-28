"""
Dispatch (FleetConnect 360 FLT-02) and customer tracking links (FLT-07).

Pages
* /dispatch/                      active jobs first, then done; filter by status
* /dispatch/new/ · /<id>/         create / edit a job; recommended vehicles; status buttons; proof of delivery
* /t/<token>/  (no login)         the customer's page: reference, status, ETA and the vehicle on a map while the job
                                  is on its way. Nothing else (no plate, driver name, phone or history). The link stops
                                  working when the job is cancelled, tracking is switched off, or it expires (default 6 h
                                  after delivery).
* /t/<token>/data.json            the same data for the page's 30-second refresh

Recommendation: vehicles that are enabled, marked Available or In use, have a GPS position and are not already on an
active job, nearest to the pickup first (straight-line distance). ETA is a rough estimate: straight-line distance x 1.3
at 45 km/h (a routing provider with traffic is a pending decision in the scope).

Status changes: staff, or the job's driver (a Driver linked to the user's login), may move a job along. Every change is
kept as a JobEvent. Assigning a vehicle with a different current driver changes nothing on the Fleet page; the job just
records who drives it.
"""
from __future__ import annotations

import logging
import math
from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from rest_framework import permissions, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from . import fleet
from .models import Device, Driver, Job, JobEvent

log = logging.getLogger("fleet.dispatch")
ROAD_FACTOR = 1.3
ETA_KMH = 45
LINK_TTL_AFTER_DONE = timedelta(hours=6)
FRESH = timedelta(minutes=30)


def _km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def eta(device, lat, lon) -> dict | None:
    if device is None or device.last_fix_lat is None or lat is None:
        return None
    km = _km(device.last_fix_lat, device.last_fix_lon, lat, lon) * ROAD_FACTOR
    minutes = km / ETA_KMH * 60
    return {"km": round(km, 1), "minutes": max(1, round(minutes)),
            "at": timezone.now() + timedelta(minutes=minutes),
            "fresh": bool(device.last_fix_ts and timezone.now() - device.last_fix_ts < FRESH)}


def recommend(job: Job, limit=5) -> list[dict]:
    busy = set(Job.objects.filter(status__in=Job.ACTIVE).exclude(pk=job.pk).values_list("device_id", flat=True))
    cur = fleet.current_drivers()
    out = []
    for v in Device.objects.filter(enabled=True, availability__in=["available", "in_use"]).exclude(model="SIMULATOR"):
        if v.pk in busy or v.last_fix_lat is None:
            continue
        d = _km(v.last_fix_lat, v.last_fix_lon, job.pickup_lat, job.pickup_lon) if job.pickup_lat is not None else None
        out.append({"v": v, "km": None if d is None else round(d * ROAD_FACTOR, 1), "driver": cur.get(v.pk),
                    "stale": not (v.last_fix_ts and timezone.now() - v.last_fix_ts < FRESH)})
    out.sort(key=lambda r: (r["stale"], r["km"] if r["km"] is not None else 1e9))
    return out[:limit]


def link_active(job: Job, now=None) -> bool:
    now = now or timezone.now()
    if not job.tracking_enabled or job.status == "cancelled":
        return False
    if job.tracking_expires:
        return now < job.tracking_expires
    if job.finished_utc:
        return now < job.finished_utc + LINK_TTL_AFTER_DONE
    return True


def set_status(job: Job, status: str, user: str = "", text: str = "", lat=None, lon=None) -> None:
    now = timezone.now()
    if status == job.status and not text:
        return
    job.status = status
    if status == "assigned" and not job.assigned_utc:
        job.assigned_utc = now
    elif status == "picked_up" and not job.picked_up_utc:
        job.picked_up_utc = now
    elif status in ("delivered", "failed", "cancelled"):
        job.finished_utc = now
        if status == "delivered" and job.device and job.pod_lat is None:
            job.pod_lat, job.pod_lon = (lat, lon) if lat is not None else (job.device.last_fix_lat, job.device.last_fix_lon)
    job.save()
    JobEvent.objects.create(job=job, user=user, status=status, text=text[:500])


def can_drive(user, job) -> bool:
    if user.is_staff:
        return True
    return bool(job.driver_id and job.driver.user_id and job.driver.user_id == user.pk)


# ---------------------------------------------------------------------------
# Forms and pages
# ---------------------------------------------------------------------------
class JobForm(fleet._Styled, forms.ModelForm):
    class Meta:
        model = Job
        fields = ["customer_name", "customer_phone", "pickup_address", "pickup_lat", "pickup_lon", "dropoff_address",
                  "dropoff_lat", "dropoff_lon", "load", "scheduled", "due_by", "device", "driver", "tracking_enabled",
                  "tracking_expires", "notes"]
        labels = {"device": "Vehicle", "pickup_lat": "Pickup latitude", "pickup_lon": "Pickup longitude",
                  "dropoff_lat": "Delivery latitude", "dropoff_lon": "Delivery longitude"}
        widgets = {k: forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")
                   for k in ("scheduled", "due_by", "tracking_expires")} | {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["device"].queryset = Device.objects.exclude(model="SIMULATOR")
        self.fields["driver"].queryset = Driver.objects.filter(active=True)
        self._style()

    def clean(self):
        c = super().clean()
        for p in ("pickup", "dropoff"):
            if (c.get(f"{p}_lat") is None) != (c.get(f"{p}_lon") is None):
                self.add_error(f"{p}_lat", "Enter both latitude and longitude, or neither.")
        return c


@login_required
def jobs_page(request):
    show = request.GET.get("show", "active")
    qs = Job.objects.select_related("device", "driver")
    if show == "active":
        qs = qs.filter(status__in=("new",) + Job.ACTIVE)
    elif show in dict(Job.STATUSES):
        qs = qs.filter(status=show)
    rows = []
    for j in qs[:300]:
        target = (j.pickup_lat, j.pickup_lon) if j.status in ("new", "assigned") else (j.dropoff_lat, j.dropoff_lon)
        rows.append({"j": j, "eta": eta(j.device, *target) if j.status in Job.ACTIVE else None,
                     "late": bool(j.due_by and j.status not in ("delivered", "cancelled", "failed") and timezone.now() > j.due_by)})
    counts = {s: Job.objects.filter(status=s).count() for s in ("new", "assigned", "picked_up")}
    return render(request, "tracking/dispatch.html", {"nav": "dispatch", "rows": rows, "show": show, "counts": counts,
                                                      "can_edit": request.user.is_staff})


@login_required
def job_page(request, pk=None):
    job = get_object_or_404(Job.objects.select_related("device", "driver"), pk=pk) if pk else None
    form = JobForm(request.POST or None, instance=job)
    if request.method == "POST" and "save" in request.POST:
        if not request.user.is_staff:
            return HttpResponse(status=403)
        if form.is_valid():
            new = not form.instance.pk
            j = form.save(commit=False)
            if new:
                j.created_by = request.user.get_username()
            if j.device_id and not j.driver_id:
                a = fleet.current_assignment(j.device)
                j.driver = a.driver if a else None
            j.save()
            if new:
                JobEvent.objects.create(job=j, user=request.user.get_username(), status=j.status, text="Created")
            if j.device_id and j.status == "new":
                set_status(j, "assigned", request.user.get_username(), f"Vehicle {j.device.name}")
            messages.success(request, f"{j.reference} saved.")
            return redirect("job", pk=j.pk)
    ctx = {"nav": "dispatch", "job": job, "form": form, "can_edit": request.user.is_staff}
    if job:
        target = (job.pickup_lat, job.pickup_lon) if job.status in ("new", "assigned") else (job.dropoff_lat, job.dropoff_lon)
        ctx.update({"events": job.events.all(), "recommended": recommend(job) if job.status in ("new", "assigned") else [],
                    "eta": eta(job.device, *target) if job.status in Job.ACTIVE else None,
                    "link": request.build_absolute_uri(reverse("track", args=[job.tracking_token])),
                    "link_active": link_active(job), "can_drive": can_drive(request.user, job)})
    return render(request, "tracking/job.html", ctx)


@login_required
@require_POST
def job_action(request, pk):
    job = get_object_or_404(Job.objects.select_related("device", "driver"), pk=pk)
    if not can_drive(request.user, job):
        return HttpResponse(status=403)
    user, act = request.user.get_username(), request.POST.get("action")
    text = request.POST.get("text", "").strip()
    if act == "assign" and request.user.is_staff:
        v = get_object_or_404(Device, pk=request.POST.get("device"))
        if Job.objects.filter(device=v, status__in=Job.ACTIVE).exclude(pk=job.pk).exists() and not request.POST.get("force"):
            messages.warning(request, f"{v.name} is already on another active job.")
            return redirect("job", pk=pk)
        a = fleet.current_assignment(v)
        job.device, job.driver = v, (a.driver if a else job.driver)
        job.save()
        set_status(job, "assigned", user, f"Vehicle {v.name}" + (f", driver {job.driver.name}" if job.driver else ""))
    elif act in ("picked_up", "failed", "cancelled", "new"):
        if act in ("cancelled", "new") and not request.user.is_staff:
            return HttpResponse(status=403)
        set_status(job, act, user, text)
    elif act == "delivered":
        job.pod_name = request.POST.get("pod_name", "").strip()[:120]
        job.pod_notes = text
        set_status(job, "delivered", user, f"Received by {job.pod_name}" if job.pod_name else text)
    elif act == "send_link":
        if not request.user.is_staff:
            return HttpResponse(status=403)
        if not job.customer_phone:
            messages.warning(request, "Add the customer's phone number first.")
            return redirect("job", pk=pk)
        from . import whatsapp
        url = request.build_absolute_uri(reverse("track", args=[job.tracking_token]))
        res = whatsapp.send(f"Hello {job.customer_name}, track your delivery {job.reference} here: {url}",
                            job.customer_phone, subject="Delivery tracking")
        JobEvent.objects.create(job=job, user=user, status=job.status, text=f"Tracking link sent: {res.get('detail', '')}"[:500])
        (messages.success if res.get("sent") else messages.warning)(request, res.get("detail", ""))
    else:
        return HttpResponse(status=400)
    return redirect("job", pk=pk)


# ---------------------------------------------------------------------------
# Customer tracking link (no login)
# ---------------------------------------------------------------------------
def _public(job: Job) -> dict:
    data = {"reference": job.reference, "status": job.status, "status_label": job.get_status_display(),
            "destination": job.dropoff_address, "dest": None, "vehicle": None, "eta_minutes": None, "eta_at": None,
            "delivered_at": job.finished_utc.isoformat() if job.status == "delivered" and job.finished_utc else None,
            "updated": None}
    if job.dropoff_lat is not None:
        data["dest"] = [round(job.dropoff_lat, 5), round(job.dropoff_lon, 5)]
    if job.status == "picked_up" and job.device and job.device.last_fix_lat is not None:
        v = job.device
        data["vehicle"] = [round(v.last_fix_lat, 5), round(v.last_fix_lon, 5)]
        data["updated"] = v.last_fix_ts.isoformat() if v.last_fix_ts else None
        e = eta(v, job.dropoff_lat, job.dropoff_lon)
        if e and e["fresh"]:
            data["eta_minutes"], data["eta_at"] = e["minutes"], e["at"].isoformat()
    return data


def _job_for_token(token):
    job = Job.objects.select_related("device").filter(tracking_token=token).first()
    if not job or not link_active(job):
        raise Http404("This tracking link is not valid any more.")
    return job


def track_page(request, token):
    try:
        job = _job_for_token(token)
    except Http404:
        return render(request, "tracking/track_public.html", {"expired": True}, status=404)
    resp = render(request, "tracking/track_public.html", {"d": _public(job), "token": token})
    resp["X-Robots-Tag"] = "noindex"
    return resp


def track_data(request, token):
    return JsonResponse(_public(_job_for_token(token)))


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------
class JobSerializer(serializers.ModelSerializer):
    vehicle_name = serializers.CharField(source="device.name", read_only=True, default=None)
    driver_name = serializers.CharField(source="driver.name", read_only=True, default=None)
    tracking_url = serializers.SerializerMethodField()

    class Meta:
        model = Job
        fields = ["id", "reference", "customer_name", "customer_phone", "pickup_address", "pickup_lat", "pickup_lon",
                  "dropoff_address", "dropoff_lat", "dropoff_lon", "load", "scheduled", "due_by", "device",
                  "vehicle_name", "driver", "driver_name", "status", "assigned_utc", "picked_up_utc", "finished_utc",
                  "pod_name", "pod_notes", "tracking_enabled", "tracking_expires", "tracking_url", "notes", "created_utc"]
        read_only_fields = ["reference", "status", "assigned_utc", "picked_up_utc", "finished_utc", "created_utc"]

    def get_tracking_url(self, obj) -> str:
        req = self.context.get("request")
        path = reverse("track", args=[obj.tracking_token])
        return req.build_absolute_uri(path) if req else path


class JobViewSet(viewsets.ModelViewSet):
    """Dispatch jobs. ?status=..., ?active=1. POST /{id}/status/ {"status": "assigned|picked_up|delivered|failed|cancelled",
    "text": "", "pod_name": ""}; GET /{id}/recommend/ lists eligible vehicles nearest the pickup."""
    serializer_class = JobSerializer
    permission_classes = [fleet.StaffWriteOrReadOnly]

    def get_queryset(self):
        qs = Job.objects.select_related("device", "driver")
        p = self.request.query_params
        if p.get("status") in dict(Job.STATUSES):
            qs = qs.filter(status=p["status"])
        if p.get("active") in ("1", "true"):
            qs = qs.filter(status__in=("new",) + Job.ACTIVE)
        return qs

    def perform_create(self, serializer):
        job = serializer.save(created_by=self.request.user.get_username())
        JobEvent.objects.create(job=job, user=job.created_by, status=job.status, text="Created (API)")
        if job.device_id:
            set_status(job, "assigned", job.created_by, f"Vehicle {job.device.name}")

    @action(detail=True, methods=["post"], url_path="status")
    def change_status(self, request, pk=None):
        job = self.get_object()
        st = request.data.get("status")
        if st not in dict(Job.STATUSES):
            return Response({"detail": "Unknown status."}, status=400)
        if st == "delivered":
            job.pod_name = (request.data.get("pod_name") or "")[:120]
            job.pod_notes = request.data.get("text") or ""
        set_status(job, st, request.user.get_username(), request.data.get("text") or "")
        return Response(JobSerializer(job, context={"request": request}).data)

    @action(detail=True, methods=["get"])
    def recommend(self, request, pk=None):
        job = self.get_object()
        return Response([{"device": r["v"].pk, "name": r["v"].name, "km": r["km"], "stale": r["stale"],
                          "driver": r["driver"].driver.name if r["driver"] else None} for r in recommend(job, 10)])
