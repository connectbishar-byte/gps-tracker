"""
Alerts page (rule engine) and the Meta WhatsApp Cloud API endpoints.

  /alerts/                         rules + alert log (everyone signed in; staff edit)
  /alerts/new/ · /alerts/<id>/     add / edit a rule (staff)
  /alerts/<id>/delete/ · /test/    delete a rule / send a sample message (staff)
  /whatsapp/webhook/               Meta webhook: GET verification (VERIFY_TOKEN), POST delivery statuses + replies
  /settings/whatsapp/test-send/    test any message type from Settings (staff)
  /api/v1/whatsapp/send-text/      POST {to_number, subject, customer_name, text_name}  → prod_report template
                                   (or plain text when Settings say so / "mode": "text")
  /api/v1/whatsapp/send-template/  same, always the template · /send-message/ always plain text
  /api/v1/whatsapp/send-media/     POST {to_number, customer_name, file_type, file_url|image_url|pdf_url, message}
                                   image → image with caption, pdf → certificate_ready_docs template
  /api/v1/whatsapp/send-certificate/  same as send-media · /send-image-message/ always an image
  (all staff only, token or session)
"""
from __future__ import annotations

import json
import logging

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from rest_framework import permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from . import rules, whatsapp
from .models import AlertEvent, AlertRule, AlertRuleState, Device, Geofence, WhatsAppMessage

log = logging.getLogger("fleet.alerts")
staff_required = user_passes_test(lambda u: u.is_active and u.is_staff)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
class RuleForm(forms.ModelForm):
    duration_min = forms.FloatField(label="Minutes", required=False, min_value=0, max_value=1440)
    channels = forms.MultipleChoiceField(choices=AlertRule.CHANNELS, required=False,
                                         widget=forms.CheckboxSelectMultiple)

    class Meta:
        model = AlertRule
        fields = ["name", "kind", "active", "devices", "geofence", "speed_limit", "channels", "recipients",
                  "message", "cooldown_min", "threshold", "io_id", "create_incident", "escalate_after_min",
                  "escalation_recipients"]
        widgets = {"devices": forms.CheckboxSelectMultiple, "recipients": forms.Textarea(attrs={"rows": 2}),
                   "escalation_recipients": forms.Textarea(attrs={"rows": 2}),
                   "message": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["devices"].queryset = Device.objects.exclude(model="SIMULATOR")
        self.fields["geofence"].queryset = Geofence.objects.all()
        self.fields["geofence"].empty_label = "Any zone"
        if self.instance.pk:
            self.initial["duration_min"] = round(self.instance.duration_s / 60, 2)
        else:
            self.initial.setdefault("channels", ["whatsapp"])
        self.fields["name"].required = False
        self.fields["escalate_after_min"].required = False
        for name, f in self.fields.items():
            if isinstance(f.widget, (forms.CheckboxSelectMultiple, forms.CheckboxInput)):
                f.widget.attrs["class"] = "form-check-input"
                continue
            f.widget.attrs.setdefault("class", "form-select form-select-sm" if isinstance(f.widget, forms.Select) else "form-control form-control-sm")

    def clean(self):
        c = super().clean()
        kind = c.get("kind")
        if not (c.get("name") or "").strip() and kind:
            c["name"] = dict(AlertRule.KINDS).get(kind, kind)
        if kind == "overspeed" and not c.get("speed_limit"):
            self.add_error("speed_limit", "Enter the speed limit in km/h.")
        c["escalate_after_min"] = c.get("escalate_after_min") or 0
        if kind == "offline" and not c.get("duration_min"):
            c["duration_min"] = 60
        if kind == "idle" and not c.get("duration_min"):
            self.add_error("duration_min", "Enter how many minutes of idling should raise the alert.")
        _, errors = whatsapp.parse_recipients(c.get("recipients") or "")
        for e in errors:
            self.add_error("recipients", e)
        _, errors = whatsapp.parse_recipients(c.get("escalation_recipients") or "")
        for e in errors:
            self.add_error("escalation_recipients", e)
        if c.get("escalate_after_min") and not c.get("create_incident"):
            self.add_error("escalate_after_min", "Escalation needs “Open an incident” ticked.")
        return c

    def save(self, commit=True):
        rule = super().save(commit=False)
        rule.name = self.cleaned_data.get("name") or rule.name
        rule.duration_s = int(round((self.cleaned_data.get("duration_min") or 0) * 60))
        rule.channels = list(self.cleaned_data.get("channels") or [])
        if rule.kind not in ("geofence_enter", "geofence_exit"):
            rule.geofence = None
        if commit:
            rule.save()
            self.save_m2m()
        return rule


@login_required
def alerts_page(request):
    rule_list = list(AlertRule.objects.prefetch_related("devices").select_related("geofence"))
    kinds = dict(AlertRule.KINDS)
    for r in rule_list:
        r.kind_label = kinds.get(r.kind, r.kind)
    events = AlertEvent.objects.select_related("device").order_by("-id")[:200]
    for e in events:
        e.kind_label = kinds.get(e.kind, e.kind)
    return render(request, "tracking/alerts.html", {
        "nav": "alerts", "rules": rule_list, "events": events, "can_edit": request.user.is_staff,
        "wa": whatsapp.status(), "tab": request.GET.get("tab", "rules"),
    })


@login_required
@staff_required
def rule_edit(request, pk=None):
    rule = get_object_or_404(AlertRule, pk=pk) if pk else None
    if request.method == "POST":
        form = RuleForm(request.POST, instance=rule)
        if form.is_valid():
            new = form.save(commit=False)
            if not new.pk:
                new.created_by = request.user.get_username()
            new.save()
            form.save_m2m()
            AlertRuleState.objects.filter(rule=new).delete()  # settings changed: start every vehicle fresh
            messages.success(request, f"Rule “{new.name}” saved.")
            return redirect("alerts")
    else:
        initial = {}
        if request.GET.get("kind") in dict(AlertRule.KINDS):
            initial["kind"] = request.GET["kind"]
            initial["create_incident"] = initial["kind"] in AlertRule.INCIDENT_KINDS
        form = RuleForm(instance=rule, initial=initial)
    return render(request, "tracking/alert_rule.html", {
        "nav": "alerts", "form": form, "rule": rule, "defaults": rules.DEFAULT_TEXT,
        "placeholders": rules.PLACEHOLDERS, "wa": whatsapp.status(),
    })


@login_required
@staff_required
@require_POST
def rule_delete(request, pk):
    rule = get_object_or_404(AlertRule, pk=pk)
    rule.delete()
    messages.success(request, f"Rule “{rule.name}” deleted.")
    return redirect("alerts")


@login_required
@staff_required
@require_POST
def rule_toggle(request, pk):
    rule = get_object_or_404(AlertRule, pk=pk)
    rule.active = not rule.active
    rule.save(update_fields=["active", "updated_utc"])
    return redirect("alerts")


@login_required
@staff_required
@require_POST
def rule_test(request, pk):
    return JsonResponse(rules.send_test(get_object_or_404(AlertRule, pk=pk)))


# ---------------------------------------------------------------------------
# Meta WhatsApp Cloud API
# ---------------------------------------------------------------------------
@csrf_exempt
def whatsapp_webhook(request):
    if request.method == "GET":
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge", "")
        if mode == "subscribe" and token and token == whatsapp.verify_token():
            return HttpResponse(challenge, content_type="text/plain")
        return HttpResponseForbidden("Verification failed")
    if request.method != "POST":
        return HttpResponse(status=405)
    raw = request.body
    if not whatsapp.check_signature(raw, request.headers.get("X-Hub-Signature-256", "")):
        log.warning("WhatsApp webhook: bad signature")
        return HttpResponseForbidden("Bad signature")
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return HttpResponse(status=400)
    whatsapp.handle_webhook(body if isinstance(body, dict) else {})
    return HttpResponse("EVENT_RECEIVED")


def _text_args(data) -> dict:
    return {"to_number": data.get("to_number") or data.get("to") or "",
            "customer_name": str(data.get("customer_name") or "").strip(),
            "subject": str(data.get("subject") or "").strip(),
            "text": str(data.get("text_name") or data.get("text") or data.get("message") or "").strip()}


def _media_args(data) -> dict:
    file_url = str(data.get("file_url") or data.get("image_url") or data.get("pdf_url") or "").strip()
    file_type = str(data.get("file_type") or ("pdf" if data.get("pdf_url") else "image")).strip().lower()
    return {"to_number": data.get("to_number") or data.get("to") or "",
            "customer_name": str(data.get("customer_name") or "").strip(), "file_url": file_url,
            "message": str(data.get("message") or "").strip(), "file_type": file_type}


def send_what(what: str, data, source: str) -> tuple[dict, int]:
    """One entry point for every Meta send: template | text | image | pdf_template | pdf."""
    t, m = _text_args(data), _media_args(data)
    if what == "template":
        return whatsapp.send_template_text(t["to_number"], t["subject"], t["customer_name"], t["text"], source=source)
    if what == "text":
        return whatsapp.send_text(**t, source=source)
    if what == "pdf_template":
        return whatsapp.send_template_document(m["to_number"], m["customer_name"], m["file_url"], m["message"],
                                               source=source)
    if what in ("image", "pdf"):
        return whatsapp.send_media(**(m | {"file_type": what}), source=source)
    return {"status": "error", "message": "Unknown message type"}, 400


@login_required
@staff_required
@require_POST
def settings_test_send(request):
    data = request.POST.copy()
    data.setdefault("subject", "Meltice Technology")
    if not (data.get("text_name") or "").strip():
        data["text_name"] = "Test message from the GPS tracker. WhatsApp alerts work."
    result, _ = send_what(request.POST.get("what", "template"), data, "test")
    return JsonResponse(result)


def _api(what_fn):
    @api_view(["POST"])
    @permission_classes([permissions.IsAdminUser])
    def view(request):
        data = request.data if isinstance(request.data, dict) else {}
        result, code = send_what(what_fn(data), data, "api")
        return Response(result, status=code)
    return view


# Text: the approved prod_report template unless Settings say plain text, or the body says "mode": "text".
api_send_text = _api(lambda d: "text" if (d.get("mode") or whatsapp.config()["meta_mode"]) == "text" else "template")
api_send_template = _api(lambda d: "template")
api_send_message = _api(lambda d: "text")
# Files: an image goes as an image with caption; a PDF goes through the certificate_ready_docs template
# unless the body says "template": false (then a plain document with caption).
api_send_media = _api(lambda d: "image" if _media_args(d)["file_type"] == "image" else
                      ("pdf" if d.get("template") in (False, "false", "0") else "pdf_template"))
api_send_image = _api(lambda d: "image")


def recent_messages(limit: int = 30):
    return WhatsAppMessage.objects.all()[:limit]
