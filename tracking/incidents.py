"""
Incidents (FleetConnect 360 GPS-05 asset security, IVM-05 incident handling).

An alert rule with "Open an incident" ticked opens an Incident when it fires (crash, SOS, power cut, towing,
offline...). While that incident is still open, the same kind on the same vehicle is counted on it ("happened
again") instead of opening another one. The alert itself is still sent by the rule's channels, deterministically,
without any AI in the path.

People then acknowledge, escalate, comment on and close the incident; each step is an IncidentNote (audit trail).
If nobody acknowledges within the rule's "Escalate after" minutes, the escalation numbers get a WhatsApp message
(auto_escalate, run every minute by tracking/periodic.py).

Pages: /incidents/ (open first) · /incidents/<id>/ (timeline + actions). REST: /api/v1/incidents/.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from rest_framework import permissions, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import AlertRule, Incident, IncidentNote

log = logging.getLogger("fleet.incidents")

SEVERITY = {"crash": "critical", "sos": "critical", "towing": "high", "power_loss": "high", "offline": "medium"}
OPEN_STATES = ("open", "acknowledged", "escalated")


def _driver_at(device, ts):
    try:
        from . import fleet
        from .models import DriverAssignment
        a = list(DriverAssignment.objects.filter(device=device, start_utc__lte=ts).select_related("driver")[:5])
        return fleet.driver_at(a, ts)
    except Exception:
        return None


def open_from_alert(rule: AlertRule, device, ev) -> Incident:
    """Open an incident for this alert, or count it on the open one of the same kind for this vehicle."""
    cur = (Incident.objects.select_for_update().filter(device=device, kind=rule.kind, status__in=OPEN_STATES)
           .order_by("-opened_utc").first())
    if cur:
        cur.occurrences += 1
        cur.last_seen_utc = ev.timestamp_utc
        cur.save(update_fields=["occurrences", "last_seen_utc"])
        IncidentNote.objects.create(incident=cur, action="repeat", text=ev.text)
        return cur
    kind_label = dict(AlertRule.KINDS).get(rule.kind, rule.kind)
    inc = Incident.objects.create(
        device=device, driver=_driver_at(device, ev.timestamp_utc), rule=rule, alert=ev, kind=rule.kind,
        title=f"{kind_label} · {device.name}"[:200], severity=SEVERITY.get(rule.kind, "high"),
        timestamp_utc=ev.timestamp_utc, last_seen_utc=ev.timestamp_utc, latitude=ev.latitude,
        longitude=ev.longitude, speed=ev.speed or 0)
    IncidentNote.objects.create(incident=inc, action="opened", text=ev.text)
    return inc


def note_back_online(device, ts) -> None:
    for inc in Incident.objects.filter(device=device, kind="offline", status__in=OPEN_STATES):
        IncidentNote.objects.create(incident=inc, action="comment", user="system",
                                    text=f"Data received again (record time {timezone.localtime(ts):%d %b %H:%M}).")


# ---------------------------------------------------------------------------
# Actions (all audited)
# ---------------------------------------------------------------------------
def acknowledge(inc: Incident, user: str, text: str = "") -> None:
    if inc.status == "closed":
        return
    if not inc.acknowledged_utc:
        inc.acknowledged_utc, inc.acknowledged_by = timezone.now(), user
    if inc.status == "open":
        inc.status = "acknowledged"
    inc.save()
    IncidentNote.objects.create(incident=inc, action="ack", user=user, text=text)


def _send_escalation(inc: Incident, why: str) -> str:
    rule = inc.rule
    nums = (rule.escalation_recipients or "").strip() if rule else ""
    if not nums:
        return "No escalation numbers on the rule; nobody was messaged."
    from . import whatsapp
    loc = f" https://maps.google.com/?q={inc.latitude:.6f},{inc.longitude:.6f}" if inc.latitude is not None else ""
    text = (f"⏫ ESCALATED: {inc.title}, {timezone.localtime(inc.timestamp_utc):%d %b %H:%M}. {why}"
            f" Status: {inc.get_status_display()}.{loc}")
    res = whatsapp.send(text, nums, subject="Incident escalation")
    return f"WhatsApp: {res.get('detail', '')}"


def escalate(inc: Incident, user: str, text: str = "", auto: bool = False) -> None:
    if inc.status == "closed":
        return
    why = text or ("Not acknowledged in time." if auto else "Escalated by " + (user or "a user") + ".")
    detail = _send_escalation(inc, why)
    inc.status, inc.escalated_utc = "escalated", timezone.now()
    inc.save()
    IncidentNote.objects.create(incident=inc, action="auto_escalate" if auto else "escalate", user=user,
                                text=(text + "\n" if text else "") + detail)


def close(inc: Incident, user: str, resolution: str, text: str = "") -> None:
    inc.status, inc.closed_utc, inc.closed_by = "closed", timezone.now(), user
    inc.resolution = resolution if resolution in dict(Incident.RESOLUTIONS) else "other"
    inc.save()
    IncidentNote.objects.create(incident=inc, action="close", user=user,
                                text=f"{inc.get_resolution_display()}" + (f": {text}" if text else ""))


def reopen(inc: Incident, user: str, text: str = "") -> None:
    inc.status, inc.closed_utc, inc.closed_by, inc.resolution = ("acknowledged" if inc.acknowledged_utc else "open"), None, "", ""
    inc.save()
    IncidentNote.objects.create(incident=inc, action="reopen", user=user, text=text)


def comment(inc: Incident, user: str, text: str) -> None:
    if text.strip():
        IncidentNote.objects.create(incident=inc, action="comment", user=user, text=text.strip()[:4000])


def auto_escalate(now=None) -> int:
    """Escalate open, unacknowledged incidents whose rule asks for it. Run every minute."""
    now = now or timezone.now()
    n = 0
    for inc in Incident.objects.filter(status="open", escalated_utc__isnull=True, rule__escalate_after_min__gt=0).select_related("rule"):
        if now - inc.opened_utc >= timedelta(minutes=inc.rule.escalate_after_min):
            with transaction.atomic():
                escalate(inc, "system", auto=True)
            n += 1
    return n


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def open_count() -> int:
    return Incident.objects.filter(status__in=OPEN_STATES).count()


@login_required
def incidents_page(request):
    show = request.GET.get("show", "open")
    qs = Incident.objects.select_related("device", "driver")
    if show == "open":
        qs = qs.filter(status__in=OPEN_STATES)
    elif show in dict(Incident.STATUSES):
        qs = qs.filter(status=show)
    since = timezone.now() - timedelta(days=30)
    stats = Incident.objects.filter(opened_utc__gte=since).aggregate(
        total=Count("id"), closed=Count("id", filter=Q(status="closed")),
        false_alarm=Count("id", filter=Q(resolution="false_alarm")))
    acked = Incident.objects.filter(opened_utc__gte=since, acknowledged_utc__isnull=False).values_list("opened_utc", "acknowledged_utc")
    mins = sorted((a - o).total_seconds() / 60 for o, a in acked)
    stats["median_ack_min"] = round(mins[len(mins) // 2], 1) if mins else None
    return render(request, "tracking/incidents.html", {
        "nav": "incidents", "incidents": qs[:300], "show": show, "stats": stats, "open_count": open_count(),
        "has_rules": AlertRule.objects.filter(create_incident=True).exists(), "can_edit": request.user.is_staff,
    })


@login_required
def incident_page(request, pk):
    inc = get_object_or_404(Incident.objects.select_related("device", "driver", "rule", "alert"), pk=pk)
    if request.method == "POST":
        user, act, text = request.user.get_username(), request.POST.get("action"), request.POST.get("text", "").strip()
        with transaction.atomic():
            inc = Incident.objects.select_for_update().get(pk=pk)
            if act == "ack":
                acknowledge(inc, user, text)
            elif act == "escalate":
                escalate(inc, user, text)
            elif act == "close":
                close(inc, user, request.POST.get("resolution", "other"), text)
            elif act == "reopen":
                reopen(inc, user, text)
            elif act == "comment":
                comment(inc, user, text)
            else:
                return HttpResponse(status=400)
        messages.success(request, "Incident updated.")
        return redirect("incident", pk=pk)
    return render(request, "tracking/incident.html", {"nav": "incidents", "inc": inc, "notes": inc.notes.all(),
                                                      "resolutions": Incident.RESOLUTIONS})


# ---------------------------------------------------------------------------
# REST API
# ---------------------------------------------------------------------------
class IncidentNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = IncidentNote
        fields = ["created_utc", "user", "action", "text"]


class IncidentSerializer(serializers.ModelSerializer):
    vehicle_name = serializers.CharField(source="device.name", read_only=True)
    driver_name = serializers.CharField(source="driver.name", read_only=True, default=None)
    notes = IncidentNoteSerializer(many=True, read_only=True)

    class Meta:
        model = Incident
        fields = ["id", "kind", "title", "severity", "status", "device", "vehicle_name", "driver", "driver_name",
                  "timestamp_utc", "opened_utc", "last_seen_utc", "occurrences", "latitude", "longitude", "speed",
                  "acknowledged_by", "acknowledged_utc", "escalated_utc", "closed_by", "closed_utc", "resolution", "notes"]
        read_only_fields = fields


class IncidentViewSet(viewsets.ReadOnlyModelViewSet):
    """Incidents, newest first. ?status=open|acknowledged|escalated|closed, ?open=1, ?device=<id>.
    POST /{id}/action/ {"action": "ack|escalate|comment|close|reopen", "text": "...", "resolution": "..."}."""
    serializer_class = IncidentSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = Incident.objects.select_related("device", "driver").prefetch_related("notes")
        p = self.request.query_params
        if p.get("status") in dict(Incident.STATUSES):
            qs = qs.filter(status=p["status"])
        if p.get("open") in ("1", "true"):
            qs = qs.filter(status__in=OPEN_STATES)
        if p.get("device", "").isdigit():
            qs = qs.filter(device_id=p["device"])
        return qs

    @action(detail=True, methods=["post"], url_path="action")
    def act(self, request, pk=None):
        inc = self.get_object()
        user, act, text = request.user.get_username(), request.data.get("action"), (request.data.get("text") or "").strip()
        fns = {"ack": lambda: acknowledge(inc, user, text), "escalate": lambda: escalate(inc, user, text),
               "comment": lambda: comment(inc, user, text), "reopen": lambda: reopen(inc, user, text),
               "close": lambda: close(inc, user, request.data.get("resolution", "other"), text)}
        if act not in fns:
            return Response({"detail": "action must be one of " + ", ".join(fns)}, status=400)
        with transaction.atomic():
            fns[act]()
        inc.refresh_from_db()
        return Response(IncidentSerializer(inc).data)
