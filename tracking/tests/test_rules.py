import json
import time
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase

from tracking import codec, ingest, integrations, whatsapp
from tracking.models import AlertEvent, AlertRule, Device, Geofence, WhatsAppMessage
from tracking.simulator import make_record
from tracking.tests.test_geofence import CENTER, INSIDE, OUTSIDE, FakeResp, fleet


def frame(dev, rows, start_ms=None):
    """rows: [(speed, ignition, extra_io dict)] one record every 60 s ending now."""
    conn_id, _, _ = ingest.open_connection("1.2.3.4:5000", dev.imei)
    t = start_ms or int(time.time() * 1000) - len(rows) * 60_000
    recs = []
    for i, (speed, ign, extra) in enumerate(rows):
        r = make_record(24.45 + i * 0.001, 54.37, speed, 0, t + i * 60_000, ignition=ign)
        for avl_id, val in (extra or {}).items():
            r.io.append(codec.IOElement(avl_id, 1, val.to_bytes(1, "big")))
        recs.append(r)
    return ingest.ingest_frame(conn_id, dev.id, dev.imei, codec.encode_frame(recs))


def setup_wa():
    for k, v in {"whatsapp_provider": "callmebot", "whatsapp_recipients": "+971501234567",
                 "whatsapp_callmebot_key": "123456"}.items():
        integrations.save(k, v)


@fleet()
class RuleTests(TestCase):
    def setUp(self):
        cache.clear()
        setup_wa()
        self.dev = Device.objects.create(imei="356307042441099", name="Jeep Wrangler")

    def rule(self, **kw):
        kw.setdefault("channels", ["whatsapp"])
        kw.setdefault("cooldown_min", 0)
        return AlertRule.objects.create(name=kw.get("kind"), **kw)

    def run_frame(self, rows, **kw):
        with mock.patch.object(whatsapp, "_send_callmebot", return_value=(True, "queued")) as send, \
                self.captureOnCommitCallbacks(execute=True):
            frame(self.dev, rows, **kw)
        return send

    def test_overspeed_once_per_episode(self):
        self.rule(kind="overspeed", speed_limit=100, duration_s=60)
        send = self.run_frame([(90, 1, None), (110, 1, None), (115, 1, None), (120, 1, None), (95, 1, None), (112, 1, None), (113, 1, None)])
        evs = list(AlertEvent.objects.order_by("timestamp_utc"))
        self.assertEqual(len(evs), 2)  # 110→115 (60 s), then again 112→113 after dropping below 97
        self.assertEqual(send.call_count, 2)
        self.assertIn("over speed: 115 km/h (limit 100 km/h)", evs[0].text)
        self.assertEqual(evs[0].status, "sent")

    def test_idle(self):
        self.rule(kind="idle", duration_s=300)
        send = self.run_frame([(0, 1, None)] * 7)
        self.assertEqual(AlertEvent.objects.count(), 1)
        self.assertIn("idling for 5 min", AlertEvent.objects.get().text)
        send.assert_called_once()

    def test_idle_not_with_ignition_off(self):
        self.rule(kind="idle", duration_s=60)
        self.run_frame([(0, 0, None)] * 5)
        self.assertFalse(AlertEvent.objects.exists())

    def test_crash_and_harsh(self):
        self.rule(kind="crash")
        self.rule(kind="harsh")
        self.run_frame([(60, 1, None), (55, 1, {253: 2}), (0, 1, {247: 1})])
        self.assertEqual(sorted(AlertEvent.objects.values_list("kind", flat=True)), ["crash", "harsh"])
        self.assertIn("harsh braking", AlertEvent.objects.get(kind="harsh").text)

    def test_ignition_changes(self):
        self.rule(kind="ignition_on")
        self.run_frame([(0, 0, None), (0, 1, None), (0, 1, None)])
        self.assertEqual(AlertEvent.objects.count(), 1)

    def test_vehicle_filter_and_cooldown(self):
        other = Device.objects.create(imei="356307042441098", name="Other")
        r = self.rule(kind="harsh")
        r.devices.set([other])
        self.run_frame([(50, 1, {253: 1})])
        self.assertFalse(AlertEvent.objects.exists())
        r.devices.clear()
        r.cooldown_min = 10
        r.save()
        self.run_frame([(50, 1, {253: 1}), (50, 1, {253: 1})])
        self.assertEqual(list(AlertEvent.objects.order_by("id").values_list("status", flat=True)), ["sent", "skipped"])

    def test_geofence_rule(self):
        fence = Geofence.objects.create(name="Office", shape="circle", center_lat=CENTER[0], center_lon=CENTER[1],
                                        radius_m=1000, alert_enter=False, alert_exit=False)
        self.rule(kind="geofence_enter", geofence=fence, recipients="+971559999999")
        conn_id, _, _ = ingest.open_connection("1.2.3.4:5000", self.dev.imei)
        t = int(time.time() * 1000) - 30_000
        recs = [make_record(*OUTSIDE, 40, 0, t), make_record(*INSIDE, 40, 0, t + 10_000)]
        with mock.patch.object(whatsapp, "_send_callmebot", return_value=(True, "queued")) as send, \
                self.captureOnCommitCallbacks(execute=True):
            ingest.ingest_frame(conn_id, self.dev.id, self.dev.imei, codec.encode_frame(recs))
        ev = AlertEvent.objects.get()
        self.assertIn("entered Office", ev.text)
        self.assertEqual(send.call_args.args[1], "+971559999999")

    def test_sms_channel_is_logged(self):
        self.rule(kind="crash", channels=["sms"])
        self.run_frame([(0, 1, {247: 1})])
        ev = AlertEvent.objects.get()
        self.assertIn("SMS: not available yet", ev.detail)


class MetaFunctionTests(TestCase):
    def setUp(self):
        cache.clear()
        integrations.save("whatsapp_meta_token", "EAAtoken")
        integrations.save("whatsapp_meta_phone_id", "1112223334")
        self.staff = User.objects.create_user("ops", password="pw-12345678", is_staff=True)
        self.c = Client()
        self.c.login(username="ops", password="pw-12345678")

    def capture(self, status=200, body=None):
        seen = {}

        def fake(req, timeout=None):
            seen["url"], seen["body"] = req.full_url, json.loads(req.data)
            seen["auth"] = req.get_header("Authorization")
            return FakeResp(status, body or {"messages": [{"id": "wamid.ABC"}]})
        return seen, mock.patch("urllib.request.urlopen", side_effect=fake)

    def test_send_text(self):
        seen, p = self.capture()
        with p:
            res, code = whatsapp.send_text("+971 50 786 4838", "Customer", "Meltice Technology", "Your daily report is ready.")
        self.assertEqual(code, 200)
        self.assertEqual(res["status"], "success")
        self.assertEqual(seen["body"]["to"], "971507864838")
        self.assertEqual(seen["body"]["text"]["body"], "*Meltice Technology*\n\nDear Customer,\n\nYour daily report is ready.")
        self.assertEqual(seen["auth"], "Bearer EAAtoken")
        self.assertEqual(WhatsAppMessage.objects.get().wamid, "wamid.ABC")

    def test_send_media_image_and_pdf(self):
        seen, p = self.capture()
        with p:
            res, code = whatsapp.send_media("971507864838", "Customer", "https://x.com/c.jpg", "Your certificate is ready.")
        self.assertEqual(seen["body"]["image"], {"link": "https://x.com/c.jpg", "caption": "Dear Customer,\n\nYour certificate is ready."})
        with p:
            whatsapp.send_media("971507864838", "Ali", "https://x.com/c.pdf", "Hi", "pdf")
        self.assertEqual(seen["body"]["document"]["filename"], "Ali_Certificate.pdf")
        self.assertEqual(whatsapp.send_media("97150", "", "https://x", "")[1], 400)
        self.assertEqual(whatsapp.send_media("971507864838", "", "http://x", "")[1], 400)
        self.assertEqual(whatsapp.send_media("971507864838", "", "https://x", "", "gif")[1], 400)

    def test_meta_error_is_returned(self):
        seen, p = self.capture(401, {"error": {"message": "Invalid OAuth access token.", "code": 190}})
        with mock.patch("urllib.request.urlopen", side_effect=__import__("urllib.error").error.HTTPError(
                "u", 401, "Unauthorized", {}, __import__("io").BytesIO(b'{"error":{"message":"Invalid OAuth access token."}}'))):
            res, code = whatsapp.send_text("971507864838", text="x")
        self.assertEqual(code, 401)
        self.assertIn("Invalid OAuth", res["message"])

    def test_webhook_verify_and_status(self):
        r = Client().get("/whatsapp/webhook/", {"hub.mode": "subscribe", "hub.verify_token": "meltice_whatsapp_2026",
                                                "hub.challenge": "12345"})
        self.assertEqual(r.content, b"12345")
        self.assertEqual(Client().get("/whatsapp/webhook/", {"hub.mode": "subscribe", "hub.verify_token": "wrong",
                                                             "hub.challenge": "1"}).status_code, 403)
        body = {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.ABC", "status": "failed", "recipient_id": "971507864838",
                "errors": [{"code": 131047, "title": "Re-engagement message"}]}]}}]}]}
        r = Client().post("/whatsapp/webhook/", json.dumps(body), content_type="application/json")
        self.assertEqual(r.status_code, 200)
        m = WhatsAppMessage.objects.get()
        self.assertEqual(m.status, "failed")
        self.assertIn("24 h", m.detail)

    def test_webhook_signature(self):
        integrations.save("whatsapp_meta_app_secret", "s3cret")
        r = Client().post("/whatsapp/webhook/", "{}", content_type="application/json", HTTP_X_HUB_SIGNATURE_256="sha256=bad")
        self.assertEqual(r.status_code, 403)

    def test_api_and_settings_buttons(self):
        seen, p = self.capture()
        with p:
            r = self.c.post("/api/v1/whatsapp/send-text/", {"to_number": "971507864838", "subject": "Meltice Technology",
                                                             "customer_name": "Customer", "text_name": "Your daily report is ready."},
                            content_type="application/json")
            self.assertEqual(r.status_code, 200, r.content)
            r = self.c.post("/api/v1/whatsapp/send-certificate/", {"to_number": "971507864838", "customer_name": "Customer",
                                                                    "file_type": "image", "file_url": "https://yourdomain.com/certificate.jpg",
                                                                    "message": "Your certificate is ready."}, content_type="application/json")
            self.assertEqual(r.status_code, 200, r.content)
            self.assertEqual(self.c.post("/settings/whatsapp/test-text/", {"to_number": "971507864838"}).json()["status"], "success")
        viewer = User.objects.create_user("v", password="pw-12345678")
        vc = Client()
        vc.login(username="v", password="pw-12345678")
        self.assertEqual(vc.post("/api/v1/whatsapp/send-text/", {}, content_type="application/json").status_code, 403)


class RulePageTests(TestCase):
    def setUp(self):
        cache.clear()
        User.objects.create_user("ops", password="pw-12345678", is_staff=True)
        self.dev = Device.objects.create(imei="356307042441099", name="Jeep Wrangler")
        self.c = Client()
        self.c.login(username="ops", password="pw-12345678")

    def test_create_edit_test_delete(self):
        self.assertEqual(self.c.get("/alerts/").status_code, 200)
        self.assertEqual(self.c.get("/alerts/new/?kind=overspeed").status_code, 200)
        r = self.c.post("/alerts/new/", {"name": "", "kind": "overspeed", "active": "on", "speed_limit": "",
                                         "duration_min": "1", "channels": ["whatsapp"], "cooldown_min": "10"})
        self.assertEqual(r.status_code, 200)  # speed limit missing
        r = self.c.post("/alerts/new/", {"name": "", "kind": "overspeed", "active": "on", "speed_limit": "120",
                                         "duration_min": "0.5", "channels": ["whatsapp"], "devices": [self.dev.pk],
                                         "recipients": "+971501234567", "cooldown_min": "10"})
        self.assertEqual(r.status_code, 302, r.content[:2000])
        rule = AlertRule.objects.get()
        self.assertEqual((rule.name, rule.duration_s, rule.channels), ("Over speed", 30, ["whatsapp"]))
        self.assertContains(self.c.get("/alerts/"), "over 120 km/h")
        setup_wa()
        with mock.patch.object(whatsapp, "_send_callmebot", return_value=(True, "queued")) as send:
            d = self.c.post(f"/alerts/{rule.pk}/test/").json()
        self.assertTrue(d["ok"])
        self.assertIn("TEST", send.call_args.args[2])
        self.c.post(f"/alerts/{rule.pk}/toggle/")
        self.assertFalse(AlertRule.objects.get().active)
        self.c.post(f"/alerts/{rule.pk}/delete/")
        self.assertFalse(AlertRule.objects.exists())
        self.assertContains(self.c.get("/settings/"), "Test the two functions")
