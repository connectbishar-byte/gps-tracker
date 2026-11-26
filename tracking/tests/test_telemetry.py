"""Vehicle panel data, per-parameter history and per-vehicle retention purge."""
import io
import time
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase
from django.utils import timezone

from tracking import codec, ingest
from tracking.models import AVLRecord, Connection, Device, IOValue, RawPacket
from tracking.simulator import make_record

IMEI_A = "356307042441013"
IMEI_B = "356307042441021"


def feed(dev, minutes_ago_list, speed=40):
    conn = Connection.objects.create(device=dev, remote="1.2.3.4:5", handshake="accepted", closed_utc=timezone.now())
    now_ms = int(time.time() * 1000)
    recs = [make_record(24.39 + i * 0.001, 54.70 + i * 0.001, speed, 90, ts_ms=now_ms - int(m * 60000))
            for i, m in enumerate(minutes_ago_list)]
    ingest.ingest_frame(conn.id, dev.id, dev.imei, codec.encode_frame(recs))
    return conn


class TelemetryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.dev = Device.objects.create(imei=IMEI_A, name="Bench")
        User.objects.create_user("viewer", password="pw-12345678")
        self.c = Client()
        self.c.login(username="viewer", password="pw-12345678")
        feed(self.dev, [20, 15, 10, 5, 1])

    def tearDown(self):
        cache.clear()

    def test_every_parameter_stored_with_vehicle_and_time(self):
        rec = AVLRecord.objects.filter(device=self.dev).first()
        rows = IOValue.objects.filter(record=rec)
        self.assertGreaterEqual(rows.count(), 15)
        for r in rows:
            self.assertEqual((r.device_id, r.timestamp_utc), (self.dev.id, rec.timestamp_utc))
        self.assertTrue(rows.filter(avl_id=72).exists())

    def test_vehicle_endpoint(self):
        d = self.c.get(f"/api/vehicle/{self.dev.id}/").json()
        self.assertEqual(d["device"]["name"], "Bench")
        self.assertEqual(d["retention_days"], 60)
        ids = {e["id"] for e in d["latest"]["io"]}
        self.assertTrue({66, 67, 72, 239, 240, 21}.issubset(ids))
        self.assertEqual(len(d["trends"]["points"]), 5)
        self.assertIsNotNone(d["trends"]["points"][-1]["temp"])
        self.assertGreater(d["today"]["distance_km"], 0)
        keys = {p["key"] for p in d["parameters"]}
        self.assertTrue({"speed", "72", "66"}.issubset(keys))
        self.assertNotIn(IMEI_A, str(d))   # IMEI masked for a viewer

    def test_parameter_history(self):
        r = self.c.get(f"/api/vehicle/{self.dev.id}/param/66/?hours=1").json()
        self.assertEqual(r["count"], 5)
        self.assertEqual(r["unit"], "V")
        self.assertTrue(11 < r["min"] <= r["avg"] <= r["max"] < 13)
        r = self.c.get(f"/api/vehicle/{self.dev.id}/param/speed/?hours=1").json()
        self.assertEqual([p[1] for p in r["points"]], [40] * 5)
        self.assertEqual(self.c.get(f"/api/vehicle/{self.dev.id}/param/drop/").status_code, 400)

    def test_rest_api_actions(self):
        staff = User.objects.create_user("ops", password="pw-12345678", is_staff=True)
        tok = Client().post("/api/v1/auth/token/", {"username": "ops", "password": "pw-12345678"},
                            content_type="application/json").json()["token"]
        h = {"HTTP_AUTHORIZATION": f"Token {tok}"}
        t = Client().get(f"/api/v1/devices/{self.dev.id}/telemetry/", **h)
        self.assertEqual(t.status_code, 200, t.content)
        self.assertIn("latest", t.json())
        p = Client().get(f"/api/v1/devices/{self.dev.id}/parameters/72/?hours=2", **h)
        self.assertEqual(p.status_code, 200, p.content)
        self.assertEqual(p.json()["count"], 5)
        self.assertEqual(Client().patch(f"/api/v1/devices/{self.dev.id}/", {"retention_days": 30},
                                        content_type="application/json", **h).status_code, 200)
        self.dev.refresh_from_db()
        self.assertEqual(self.dev.retention_days, 30)
        del staff

    def test_pages_include_panel(self):
        self.assertIn("vehicle-panel.js", self.c.get("/live/").content.decode())
        page = self.c.get(f"/devices/{self.dev.id}/").content.decode()
        self.assertIn('id="vpanel"', page)
        self.assertIn("60 days", page)


class RetentionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.a = Device.objects.create(imei=IMEI_A, name="Keeps 60 days")
        self.b = Device.objects.create(imei=IMEI_B, name="Keeps 7 days", retention_days=7)
        for dev in (self.a, self.b):
            feed(dev, [10 * 24 * 60, 30])   # one record 10 days old, one 30 minutes old
        # the old packets/connections were received 10 days ago as well
        old = timezone.now() - timedelta(days=10)
        RawPacket.objects.update(received_utc=old)
        Connection.objects.update(opened_utc=old)

    def tearDown(self):
        cache.clear()

    def counts(self, dev):
        return (AVLRecord.objects.filter(device=dev).count(), IOValue.objects.filter(device=dev).count())

    def test_dry_run_changes_nothing(self):
        before = (self.counts(self.a), self.counts(self.b))
        out = io.StringIO()
        call_command("purge_data", "--dry-run", stdout=out)
        self.assertIn("would delete", out.getvalue())
        self.assertEqual(before, (self.counts(self.a), self.counts(self.b)))

    def test_purge_uses_each_vehicles_retention(self):
        a_before = self.counts(self.a)
        call_command("purge_data", stdout=io.StringIO())
        self.assertEqual(self.counts(self.a), a_before)                 # 60 days: nothing old enough
        recs, ios = self.counts(self.b)
        self.assertEqual(recs, 1)                                       # 7 days: the 10-day-old record is gone
        self.assertEqual(ios, IOValue.objects.filter(record__device=self.b).count())
        self.assertTrue(AVLRecord.objects.filter(device=self.b, timestamp_utc__gte=timezone.now() - timedelta(hours=1)).exists())
        # its packet still holds a kept record, so it stays
        self.assertEqual(RawPacket.objects.filter(device=self.b).count(), 1)
        self.assertIsNotNone(cache.get("purge:last"))

    def test_retention_editable_on_device_page(self):
        User.objects.create_user("ops", password="pw-12345678", is_staff=True)
        c = Client()
        c.login(username="ops", password="pw-12345678")
        form = {"imei": IMEI_A, "name": "Keeps 60 days", "plate": "", "group": "Default", "model": "FMC130",
                "firmware": "", "config_revision": "", "enabled": "on", "notes": "", "retention_days": "90"}
        self.assertEqual(c.post(f"/devices/{self.a.id}/", form).status_code, 302)
        self.a.refresh_from_db()
        self.assertEqual(self.a.retention_days, 90)
        form["retention_days"] = "0"
        c.post(f"/devices/{self.a.id}/", form)
        self.a.refresh_from_db()
        self.assertEqual(self.a.retention_days, 90)   # rejected: minimum is 1
