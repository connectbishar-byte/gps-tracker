from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from tracking import fleet as fleet_mod, incidents, rules, whatsapp
from tracking.models import AlertEvent, AlertRule, Device, Driver, Incident
from tracking.tests.test_geofence import fleet
from tracking.tests.test_rules import frame, setup_wa


@fleet()
class IncidentRuleTests(TestCase):
    def setUp(self):
        cache.clear()
        setup_wa()
        self.dev = Device.objects.create(imei="356307042441099", name="Jeep Wrangler")

    def rule(self, **kw):
        kw.setdefault("channels", ["whatsapp"])
        kw.setdefault("cooldown_min", 0)
        kw.setdefault("create_incident", True)
        return AlertRule.objects.create(name=kw.get("kind"), **kw)

    def run_frame(self, rows):
        with mock.patch.object(whatsapp, "_send_callmebot", return_value=(True, "queued")) as send, \
                self.captureOnCommitCallbacks(execute=True):
            frame(self.dev, rows)
        return send

    def test_sos_edge_and_incident_dedup(self):
        self.rule(kind="sos")
        drv = Driver.objects.create(name="Ali")
        fleet_mod.assign(self.dev, drv, when=timezone.now() - timedelta(hours=1))
        send = self.run_frame([(0, 1, {2: 0}), (0, 1, {2: 1}), (0, 1, {2: 1}), (0, 1, {2: 0}), (0, 1, {2: 1})])
        self.assertEqual(AlertEvent.objects.filter(kind="sos").count(), 2)  # two presses
        self.assertEqual(send.call_count, 2)
        inc = Incident.objects.get()
        self.assertEqual(inc.occurrences, 2)
        self.assertEqual(inc.severity, "critical")
        self.assertEqual(inc.driver, drv)
        self.assertEqual([n.action for n in inc.notes.all()], ["opened", "repeat"])

    def test_power_loss_once_until_restored(self):
        self.rule(kind="power_loss")
        self.run_frame([(0, 1, None), (0, 1, {252: 1}), (0, 1, {252: 1}), (0, 1, {252: 0}), (0, 1, {252: 1})])
        evs = list(AlertEvent.objects.filter(kind="power_loss").order_by("timestamp_utc"))
        self.assertEqual(len(evs), 2)
        self.assertIn("unplugged", evs[1].text)

    def test_power_loss_low_voltage(self):
        from tracking.models import AlertRuleState
        rule = self.rule(kind="power_loss", threshold=9)
        st = AlertRuleState.objects.create(rule=rule, device=self.dev)
        now, pending = timezone.now(), []
        rec = lambda v: {"ts": now, "lat": 24.4, "lon": 54.3, "speed": 0, "valid": True, "io": {66: v}}
        self.assertIsNotNone(rules._check(rule, self.dev, st, rec(8.2), pending))
        self.assertIsNone(rules._check(rule, self.dev, st, rec(9.5), pending))   # not yet restored (needs 10 V)
        self.assertIsNone(rules._check(rule, self.dev, st, rec(5.0), pending))
        self.assertIsNone(rules._check(rule, self.dev, st, rec(12.4), pending))  # restored
        ev = rules._check(rule, self.dev, st, rec(3.0), pending)
        self.assertIn("3.0 V (below 9 V)", ev.text)

    def test_towing_with_ignition_off(self):
        self.rule(kind="towing", duration_s=60)
        self.run_frame([(0, 0, None), (25, 0, None), (30, 0, None), (35, 0, None), (40, 1, None)])
        self.assertEqual(AlertEvent.objects.filter(kind="towing").count(), 1)
        self.assertEqual(Incident.objects.get().kind, "towing")

    def test_offline(self):
        self.rule(kind="offline", duration_s=1800)
        self.run_frame([(0, 1, None)])
        now = timezone.now()
        with mock.patch.object(whatsapp, "_send_callmebot", return_value=(True, "queued")), \
                self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(rules.check_offline(now + timedelta(minutes=10)), [])
            self.assertEqual(len(rules.check_offline(now + timedelta(minutes=40))), 1)
            self.assertEqual(rules.check_offline(now + timedelta(minutes=50)), [])  # same episode
        ev = AlertEvent.objects.get(kind="offline")
        self.assertIn("has sent no data", ev.text)
        inc = Incident.objects.get(kind="offline")
        self.run_frame([(0, 1, None)])  # back online → note, episode reset
        self.assertTrue(inc.notes.filter(text__startswith="Data received again").exists())

    def test_no_incident_when_not_ticked(self):
        self.rule(kind="crash", create_incident=False)
        self.run_frame([(50, 1, {247: 1})])
        self.assertEqual(AlertEvent.objects.count(), 1)
        self.assertFalse(Incident.objects.exists())


@fleet()
class IncidentFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        setup_wa()
        self.dev = Device.objects.create(imei="356307042441099", name="Jeep")
        self.rule = AlertRule.objects.create(name="Crash", kind="crash", channels=["whatsapp"], create_incident=True,
                                             escalate_after_min=5, escalation_recipients="+971509999999")
        ev = AlertEvent.objects.create(rule=self.rule, rule_name="Crash", kind="crash", device=self.dev,
                                       timestamp_utc=timezone.now(), text="crash", latitude=24.4, longitude=54.3)
        self.inc = incidents.open_from_alert(self.rule, self.dev, ev)
        self.user = User.objects.create_user("ops", password="pw-123456")
        self.c = Client()
        self.c.force_login(self.user)

    def test_auto_escalation(self):
        with mock.patch.object(whatsapp, "_send_callmebot", return_value=(True, "queued")) as send:
            self.assertEqual(incidents.auto_escalate(timezone.now() + timedelta(minutes=2)), 0)
            self.assertEqual(incidents.auto_escalate(timezone.now() + timedelta(minutes=6)), 1)
            self.assertEqual(incidents.auto_escalate(timezone.now() + timedelta(minutes=9)), 0)
        self.assertEqual(send.call_args[0][1], "+971509999999")
        self.inc.refresh_from_db()
        self.assertEqual(self.inc.status, "escalated")

    def test_ack_stops_escalation_and_pages(self):
        self.assertEqual(self.c.get("/incidents/").status_code, 200)
        self.assertContains(self.c.get(f"/incidents/{self.inc.pk}/"), "Acknowledge")
        self.c.post(f"/incidents/{self.inc.pk}/", {"action": "ack", "text": "Calling driver"})
        self.inc.refresh_from_db()
        self.assertEqual((self.inc.status, self.inc.acknowledged_by), ("acknowledged", "ops"))
        self.assertEqual(incidents.auto_escalate(timezone.now() + timedelta(minutes=10)), 0)
        self.c.post(f"/incidents/{self.inc.pk}/", {"action": "close", "resolution": "false_alarm"})
        self.inc.refresh_from_db()
        self.assertEqual((self.inc.status, self.inc.resolution), ("closed", "false_alarm"))
        self.assertEqual([n.action for n in self.inc.notes.all()], ["opened", "ack", "close"])

    def test_api(self):
        r = self.c.get("/api/v1/incidents/?open=1")
        self.assertEqual(r.json()["count"], 1)
        r = self.c.post(f"/api/v1/incidents/{self.inc.pk}/action/", {"action": "ack"}, content_type="application/json")
        self.assertEqual(r.json()["status"], "acknowledged")
        r = self.c.post(f"/api/v1/incidents/{self.inc.pk}/action/", {"action": "nope"}, content_type="application/json")
        self.assertEqual(r.status_code, 400)

    def test_rule_form_has_incident_fields(self):
        staff = User.objects.create_user("admin", password="pw-123456", is_staff=True)
        c = Client()
        c.force_login(staff)
        r = c.get("/alerts/new/?kind=sos")
        self.assertContains(r, "Escalation numbers")
        self.assertContains(r, 'id="id_create_incident" checked')
