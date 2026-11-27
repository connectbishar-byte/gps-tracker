import json
import time
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.conf import settings

from tracking import codec, geofence, ingest, integrations, whatsapp
from tracking.models import Device, Geofence, GeofenceEvent, GeofenceState
from tracking.simulator import make_record

# Abu Dhabi Corniche, ~ 1 km zone
CENTER = (24.4700, 54.3400)
INSIDE = (24.4705, 54.3402)
OUTSIDE = (24.4900, 54.3700)
JUST_OUT = (24.4700 + 1020 / 111320, 54.3400)  # 20 m past a 1000 m edge


class FakeResp:
    def __init__(self, status, body):
        self.status, self.body = status, json.dumps(body).encode()

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fleet(**kw):
    return override_settings(FLEET={**settings.FLEET, "GEOFENCE_SYNC_ALERTS": True, **kw})


def send_fixes(dev, points, start_ms=None):
    conn_id, _, _ = ingest.open_connection("1.2.3.4:5000", dev.imei)
    t = start_ms or int(time.time() * 1000) - len(points) * 10_000
    recs = [make_record(lat, lon, 40, 0, t + i * 10_000) for i, (lat, lon) in enumerate(points)]
    return ingest.ingest_frame(conn_id, dev.id, dev.imei, codec.encode_frame(recs))


class GeometryTests(TestCase):
    def test_circle(self):
        f = Geofence(shape="circle", center_lat=CENTER[0], center_lon=CENTER[1], radius_m=1000)
        self.assertTrue(geofence.locate(f, *INSIDE)[0])
        inside, edge = geofence.locate(f, *JUST_OUT)
        self.assertFalse(inside)
        self.assertAlmostEqual(edge, 20, delta=2)

    def test_polygon(self):
        f = Geofence(shape="polygon", points=[[24.46, 54.33], [24.48, 54.33], [24.48, 54.35], [24.46, 54.35]])
        self.assertTrue(geofence.locate(f, *INSIDE)[0])
        self.assertFalse(geofence.locate(f, *OUTSIDE)[0])
        # concave: an L shape, point in the notch is outside
        L_shape = Geofence(shape="polygon", points=[[0, 0], [0, 2], [1, 2], [1, 1], [2, 1], [2, 0]])
        self.assertTrue(geofence.locate(L_shape, 0.5, 0.5)[0])
        self.assertFalse(geofence.locate(L_shape, 1.5, 1.5)[0])

    def test_validate(self):
        clean, errors = geofence.validate({"name": "Home", "shape": "circle", "center_lat": 24.4, "center_lon": 54.3,
                                           "radius_m": 250, "alert_enter": True})
        self.assertEqual(errors, [])
        self.assertEqual(clean["radius_m"], 250)
        _, errors = geofence.validate({"name": "", "shape": "polygon", "points": [[1, 1], [2, 2]]})
        self.assertEqual(len(errors), 2)


@fleet()
class CrossingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.dev = Device.objects.create(imei="356307042441099", name="Jeep Wrangler")
        self.fence = Geofence.objects.create(name="Office", shape="circle", center_lat=CENTER[0], center_lon=CENTER[1],
                                             radius_m=1000, alert_enter=True, alert_exit=True)
        for k, v in {"whatsapp_provider": "callmebot", "whatsapp_recipients": "+971501234567",
                     "whatsapp_callmebot_key": "123456"}.items():
            integrations.save(k, v)

    def test_enter_then_exit_sends_whatsapp(self):
        with mock.patch.object(whatsapp, "_send_callmebot", return_value=(True, "queued")) as send, \
                self.captureOnCommitCallbacks(execute=True):
            send_fixes(self.dev, [OUTSIDE, INSIDE, INSIDE, OUTSIDE])
        evs = list(GeofenceEvent.objects.order_by("timestamp_utc"))
        self.assertEqual([e.kind for e in evs], ["enter", "exit"])
        self.assertEqual(send.call_count, 2)
        text = send.call_args_list[0].args[2]
        self.assertIn("Jeep Wrangler entered Office", text)
        self.assertIn("maps.google.com", text)
        self.assertEqual({e.alert_status for e in GeofenceEvent.objects.all()}, {"sent"})

    def test_first_fix_inside_does_not_alert(self):
        send_fixes(self.dev, [INSIDE, INSIDE])
        self.assertFalse(GeofenceEvent.objects.exists())
        self.assertTrue(GeofenceState.objects.get(device=self.dev).inside)

    def test_jitter_at_edge_does_not_flap(self):
        with mock.patch.object(whatsapp, "_send_callmebot", return_value=(True, "queued")):
            send_fixes(self.dev, [OUTSIDE, INSIDE, JUST_OUT, INSIDE, JUST_OUT])
        self.assertEqual(list(GeofenceEvent.objects.values_list("kind", flat=True)), ["enter"])

    def test_old_backlog_is_logged_not_sent(self):
        with mock.patch.object(whatsapp, "_send_callmebot") as send, self.captureOnCommitCallbacks(execute=True):
            send_fixes(self.dev, [OUTSIDE, INSIDE], start_ms=int(time.time() * 1000) - 3 * 3600 * 1000)
        ev = GeofenceEvent.objects.get()
        self.assertEqual(ev.alert_status, "skipped")
        send.assert_not_called()

    def test_zone_for_other_vehicle_is_ignored(self):
        other = Device.objects.create(imei="356307042441098", name="Other")
        self.fence.devices.set([other])
        send_fixes(self.dev, [OUTSIDE, INSIDE])
        self.assertFalse(GeofenceEvent.objects.exists())

    def test_exit_alert_off_logs_only(self):
        self.fence.alert_exit = False
        self.fence.save()
        with mock.patch.object(whatsapp, "_send_callmebot", return_value=(True, "queued")) as send, \
                self.captureOnCommitCallbacks(execute=True):
            send_fixes(self.dev, [INSIDE, OUTSIDE])
        self.assertEqual(GeofenceEvent.objects.get().alert_status, "off")
        send.assert_not_called()

    def test_failed_send_is_recorded(self):
        with mock.patch.object(whatsapp, "_send_callmebot", return_value=(False, "CallMeBot said: APIKey is invalid")), \
                self.captureOnCommitCallbacks(execute=True):
            send_fixes(self.dev, [OUTSIDE, INSIDE])
        ev = GeofenceEvent.objects.get()
        self.assertEqual(ev.alert_status, "failed")
        self.assertIn("APIKey is invalid", ev.alert_detail)


class WhatsAppTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_parse_recipients(self):
        r, errors = whatsapp.parse_recipients("+971 50 123 4567, 00971551234567:abc\nnot-a-number")
        self.assertEqual([x["phone"] for x in r], ["+971501234567", "+971551234567"])
        self.assertEqual(r[1]["key"], "abc")
        self.assertEqual(len(errors), 1)

    def test_missing(self):
        self.assertEqual(whatsapp.missing(), ["choose a provider"])
        integrations.save("whatsapp_provider", "meta")
        self.assertIn("access token", whatsapp.missing())

    def test_meta_template_payload(self):
        integrations.save("whatsapp_provider", "meta")
        integrations.save("whatsapp_meta_token", "EAAtoken")
        integrations.save("whatsapp_meta_phone_id", "1112223334")
        integrations.save("whatsapp_meta_template", "gps_alert")
        integrations.save("whatsapp_recipients", "+971501234567")
        seen = {}

        def fake(req, timeout=None):
            seen["url"], seen["body"] = req.full_url, json.loads(req.data)
            return FakeResp(200, {"messages": [{"id": "wamid.x"}]})
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            res = whatsapp.send("Jeep entered Office\nat 10:00")
        self.assertTrue(res["ok"])
        self.assertIn("/1112223334/messages", seen["url"])
        self.assertEqual(seen["body"]["to"], "971501234567")
        self.assertEqual(seen["body"]["template"]["name"], "gps_alert")
        self.assertEqual(seen["body"]["template"]["components"][1]["parameters"][1]["text"], "Jeep entered Office at 10:00")

    def test_callmebot_error_page(self):
        integrations.save("whatsapp_provider", "callmebot")
        integrations.save("whatsapp_callmebot_key", "1")
        integrations.save("whatsapp_recipients", "+971501234567")
        with mock.patch.object(whatsapp, "_http", return_value=(True, "<p>APIKey is invalid.</p>")):
            res = whatsapp.send("hi")
        self.assertFalse(res["ok"])


class PageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.staff = User.objects.create_user("ops", password="pw-12345678", is_staff=True)
        self.viewer = User.objects.create_user("viewer", password="pw-12345678")
        self.dev = Device.objects.create(imei="356307042441099", name="Jeep Wrangler")
        self.c = Client()
        self.c.login(username="ops", password="pw-12345678")

    def post_json(self, url, data, client=None):
        return (client or self.c).post(url, json.dumps(data), content_type="application/json")

    def test_create_update_delete(self):
        self.assertEqual(self.c.get("/geofences/").status_code, 200)
        r = self.post_json("/geofences/api/", {"name": "Home", "shape": "polygon", "active": True, "alert_enter": True,
                                                "points": [[24.46, 54.33], [24.48, 54.33], [24.48, 54.35]],
                                                "device_ids": [self.dev.pk]})
        self.assertEqual(r.status_code, 201, r.content)
        pk = r.json()["fence"]["id"]
        GeofenceState.objects.create(geofence_id=pk, device=self.dev, inside=True)
        r = self.post_json(f"/geofences/api/{pk}/", {"name": "Home", "shape": "circle", "center_lat": 24.4,
                                                     "center_lon": 54.3, "radius_m": 300, "active": True})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertFalse(GeofenceState.objects.exists())  # moved: state starts over
        self.assertEqual(self.c.get("/geofences/api/").json()["fences"][0]["radius_m"], 300)
        self.assertEqual(self.c.post(f"/geofences/api/{pk}/delete/").status_code, 200)
        self.assertFalse(Geofence.objects.exists())

    def test_viewer_cannot_edit(self):
        v = Client()
        v.login(username="viewer", password="pw-12345678")
        self.assertEqual(v.get("/geofences/").status_code, 200)
        r = self.post_json("/geofences/api/", {"name": "X", "shape": "circle", "center_lat": 1, "center_lon": 1,
                                                "radius_m": 100}, client=v)
        self.assertEqual(r.status_code, 403)

    def test_settings_save_and_test(self):
        r = self.c.post("/settings/whatsapp/", {"whatsapp_provider": "callmebot", "whatsapp_recipients": "+971501234567",
                                                "whatsapp_callmebot_key": "999"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(integrations.get("whatsapp_callmebot_key"), "999")
        self.assertTrue(whatsapp.status()["ready"])
        page = self.c.get("/settings/").content.decode()
        self.assertIn("WhatsApp alerts", page)
        self.assertNotIn("999", page.split('id="whatsapp"')[1].split("</form>")[0].replace("+971501234567", ""))
        with mock.patch.object(whatsapp, "_send_callmebot", return_value=(True, "queued")):
            self.assertTrue(self.c.post("/settings/whatsapp/test/").json()["ok"])
        # integrations card save must not wipe the WhatsApp values
        self.c.post("/settings/integrations/", {})
        self.assertEqual(integrations.get("whatsapp_provider"), "callmebot")
