import io
import json
import zipfile
from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from tracking import driverscore
from tracking.models import AVLRecord, Device, IOValue, RawPacket, SavedPlace


def _day_start(days_ago=1):
    d = timezone.localdate() - timedelta(days=days_ago)
    return timezone.make_aware(timezone.datetime.combine(d, timezone.datetime.min.time()))


class DriverScoreTests(TestCase):
    def setUp(self):
        cache.clear()
        self.dev = Device.objects.create(imei="356307042441013", name="Truck 7", plate="AD 12345")
        self.pkt = RawPacket.objects.create(device=self.dev, hex="00", size=1, sha256="x")
        self.u = User.objects.create_user("driver", password="pw-12345678")
        self.other = User.objects.create_user("other", password="pw-12345678")
        self.staff = User.objects.create_user("ops", password="pw-12345678", is_staff=True)
        self.c = Client()
        self.c.login(username="driver", password="pw-12345678")
        self.n = 0

    def rec(self, t, speed, lat, lon, ign=None, green=None, crash=False, angle=90):
        self.n += 1
        r = AVLRecord.objects.create(device=self.dev, packet=self.pkt, index=0, timestamp_utc=t, latitude=lat,
                                     longitude=lon, altitude=10, angle=angle, satellites=10, speed=speed, priority=0,
                                     event_io_id=253 if green else 0, valid_fix=True, fingerprint=f"f{self.n}")
        for avl, val in ((239, None if ign is None else int(ign)), (253, green), (247, 1 if crash else None)):
            if val is not None:
                IOValue.objects.create(record=r, device=self.dev, timestamp_utc=t, avl_id=avl, byte_length=1,
                                       raw_hex="01", value=val, mapping_version="t")
        return r

    def drive(self, start, minutes=30, kmh=60, over_from=None, green=(), ign=True):
        """One record every 10 s heading east; ignition on, then off after the drive."""
        lat, lon = 24.45, 54.30
        steps = minutes * 6
        for i in range(steps + 1):
            t = start + timedelta(seconds=10 * i)
            spd = 0 if i < 6 else (130 if over_from is not None and i >= over_from else kmh)
            g = None
            for gi, gt in green:
                if gi == i:
                    g = gt
            self.rec(t, spd, lat, lon, ign=True if ign else None, green=g)
            lon += spd / 3600 * 10 / 101.0  # ~101 km per degree of longitude at 24.45° N
        self.rec(start + timedelta(seconds=10 * (steps + 1)), 0, lat, lon, ign=False if ign else None)

    # -- engine ------------------------------------------------------------
    def test_clean_drive_scores_100(self):
        start = _day_start(1) + timedelta(hours=8)
        self.drive(start, minutes=30, kmh=60)
        trips, notes = driverscore.vehicle_trips(self.dev, _day_start(1), _day_start(0), driverscore.DEFAULTS)
        self.assertEqual(len(trips), 1)
        tr = trips[0]
        self.assertAlmostEqual(tr.m.km, 29, delta=2)
        self.assertEqual(tr.m.harsh_brake, 0)
        self.assertAlmostEqual(tr.m.idle_s, 60, delta=11)  # first minute standing with ignition on
        self.assertTrue(notes["ignition"])
        res = driverscore.score(tr.m, driverscore.DEFAULTS)
        self.assertEqual(res["score"], 100)
        self.assertEqual(res["rating"], "good")

    def test_tracker_green_driving_and_overspeed_cost_points(self):
        start = _day_start(1) + timedelta(hours=8)
        # 2 harsh brakes, 1 harsh accel, 1 cornering, last third of the drive over 120 km/h
        self.drive(start, minutes=30, kmh=80, over_from=120, green=[(20, 2), (40, 2), (60, 1), (80, 3)])
        s = driverscore.build_sheet("vehicle", self.dev, _day_start(1), _day_start(0), driverscore.DEFAULTS)
        t, r = s["total"], s["result"]
        self.assertEqual((t.harsh_accel, t.harsh_brake, t.harsh_turn), (1, 2, 1))
        self.assertEqual(s["notes"]["harsh_source"], "tracker")
        self.assertGreater(t.overspeed_s, 0)
        by = {b["key"]: b for b in r["breakdown"]}
        self.assertGreater(by["harsh_brake"]["points"], 0)
        self.assertGreater(by["overspeed"]["points"], 0)
        self.assertEqual(by["crash"]["points"], 0)
        self.assertLess(r["score"], 100)
        self.assertEqual(r["score"], max(0, round(100 - sum(b["points"] for b in r["breakdown"]))))

    def test_weights_are_configurable(self):
        start = _day_start(1) + timedelta(hours=8)
        self.drive(start, minutes=20, kmh=70, green=[(20, 2), (30, 2)])
        base = driverscore.build_sheet("vehicle", self.dev, _day_start(1), _day_start(0), driverscore.get_config())
        cfg, errors = driverscore.clean_config({**driverscore.DEFAULTS, "w_harsh_brake": "0"})
        self.assertEqual(errors, [])
        driverscore.save_config(cfg)
        free = driverscore.build_sheet("vehicle", self.dev, _day_start(1), _day_start(0), driverscore.get_config())
        self.assertGreater(free["result"]["score"], base["result"]["score"])
        _, errors = driverscore.clean_config({"good_min": "50", "fair_min": "70"})
        self.assertTrue(errors)
        _, errors = driverscore.clean_config({"w_crash": "abc"})
        self.assertTrue(errors)

    def test_no_ignition_splits_on_long_stops(self):
        start = _day_start(1) + timedelta(hours=8)
        self.drive(start, minutes=10, kmh=60, ign=False)
        self.drive(start + timedelta(hours=1), minutes=10, kmh=60, ign=False)
        trips, notes = driverscore.vehicle_trips(self.dev, _day_start(1), _day_start(0), driverscore.DEFAULTS)
        self.assertFalse(notes["ignition"])
        self.assertEqual(len(trips), 2)
        self.assertIsNone(trips[0].m.idle_s)

    def test_estimated_harsh_braking_from_dense_records(self):
        t0 = _day_start(1) + timedelta(hours=9)
        speeds = [0, 10, 20, 30, 40, 50, 60, 70] + [70] * 90 + [40, 20, 0]  # 70 -> 40 km/h in 1 s = 8.3 m/s²
        lon = 54.3
        for i, s in enumerate(speeds):
            self.rec(t0 + timedelta(seconds=i), s, 24.45, lon, ign=True)
            lon += s / 3600 / 101.0
        self.rec(t0 + timedelta(seconds=200), 0, 24.45, lon, ign=False)
        trips, notes = driverscore.vehicle_trips(self.dev, _day_start(1), _day_start(0), driverscore.DEFAULTS)
        self.assertEqual(notes["harsh_source"], "estimated")
        self.assertGreaterEqual(sum(t.m.harsh_brake for t in trips), 1)

    def test_no_driving_has_no_score(self):
        s = driverscore.build_sheet("vehicle", self.dev, _day_start(1), _day_start(0), driverscore.DEFAULTS)
        self.assertIsNone(s["result"]["score"])
        self.assertEqual(s["result"]["rating_label"], "No driving")

    def test_car_view_trips_are_scored_for_the_driver(self):
        now = timezone.now()
        SavedPlace.objects.create(owner=self.u, kind="trip", name="Home", latitude=24.45, longitude=54.37,
                                  started_utc=now - timedelta(minutes=40), ended_utc=now - timedelta(minutes=10),
                                  distance_km=25, duration_s=1800, moving_s=1500, max_speed_kmh=110,
                                  harsh_accel=1, harsh_brake=2, overspeed_s=0, created_utc=now - timedelta(minutes=10))
        s = driverscore.build_sheet("user", self.u, now - timedelta(days=1), now + timedelta(hours=1), driverscore.DEFAULTS)
        self.assertEqual(s["total"].trips, 1)
        self.assertEqual(s["total"].harsh_brake, 2)
        # 2 brakes * 4 * 100/25 + 1 accel * 3 * 100/25 = 32 + 12 = 44 points
        self.assertEqual(s["result"]["score"], 56)
        self.assertEqual(s["result"]["rating"], "aggressive")

    # -- pages and exports -------------------------------------------------
    def test_pages_and_exports(self):
        self.drive(_day_start(1) + timedelta(hours=8), minutes=15, kmh=60, green=[(30, 2)])
        d = (timezone.localdate() - timedelta(days=1)).isoformat()
        qs = f"?from={d}&to={timezone.localdate().isoformat()}"
        r = self.c.get("/drivers/" + qs)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Truck 7")
        self.assertNotContains(r, "Scoring weights")  # not staff
        r = self.c.get(f"/drivers/vehicle/{self.dev.pk}/" + qs)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Score breakdown by parameter")
        self.assertContains(r, "Harsh braking")
        for url in ("/drivers/" + qs, f"/drivers/vehicle/{self.dev.pk}/" + qs):
            x = self.c.get(url + "&format=xlsx")
            self.assertEqual(x.status_code, 200)
            self.assertIn("spreadsheetml", x["Content-Type"])
            z = zipfile.ZipFile(io.BytesIO(x.content))
            self.assertIn("xl/worksheets/sheet1.xml", z.namelist())
            self.assertIn("Truck 7", z.read("xl/worksheets/sheet1.xml").decode())
            c = self.c.get(url + "&format=csv")
            self.assertEqual(c.status_code, 200)
            self.assertIn("Truck 7", c.content.decode("utf-8-sig"))
        self.assertEqual(self.c.get("/drivers/?from=bad").status_code, 200)
        self.assertEqual(self.c.get("/drivers/truck/1/").status_code, 404)

    def test_xlsx_opens_in_openpyxl_when_available(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest("openpyxl not installed")
        self.drive(_day_start(1) + timedelta(hours=8), minutes=15, kmh=60)
        d = (timezone.localdate() - timedelta(days=1)).isoformat()
        x = self.c.get(f"/drivers/vehicle/{self.dev.pk}/?from={d}&format=xlsx")
        wb = openpyxl.load_workbook(io.BytesIO(x.content))
        self.assertEqual(wb.sheetnames, ["Score sheet", "Trips", "Scoring settings"])
        self.assertEqual(wb["Trips"].max_row, 2)

    def test_car_view_driver_sheet_is_private(self):
        self.assertEqual(self.c.get(f"/drivers/user/{self.u.pk}/").status_code, 200)
        self.assertEqual(self.c.get(f"/drivers/user/{self.other.pk}/").status_code, 404)
        staff = Client()
        staff.login(username="ops", password="pw-12345678")
        self.assertEqual(staff.get(f"/drivers/user/{self.other.pk}/").status_code, 200)

    def test_settings_staff_only(self):
        r = self.c.post("/drivers/settings/", {"w_harsh_brake": "9"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(driverscore.get_config()["w_harsh_brake"], 4.0)
        staff = Client()
        staff.login(username="ops", password="pw-12345678")
        self.assertContains(staff.get("/settings/"), "Save driver score settings")
        data = {k: v for k, v in driverscore.DEFAULTS.items()}
        data.update({"w_harsh_brake": "9", "speed_limit_kmh": "100"})
        r = staff.post("/drivers/settings/", data)
        self.assertEqual(r.status_code, 302)
        cfg = driverscore.get_config()
        self.assertEqual(cfg["w_harsh_brake"], 9.0)
        self.assertEqual(cfg["speed_limit_kmh"], 100)
        staff.post("/drivers/settings/", {"reset": "1"})
        self.assertEqual(driverscore.get_config()["w_harsh_brake"], 4.0)

    def test_api(self):
        self.drive(_day_start(1) + timedelta(hours=8), minutes=15, kmh=60)
        d = (timezone.localdate() - timedelta(days=1)).isoformat()
        r = self.c.get(f"/api/v1/driver-scores/?from={d}")
        self.assertEqual(r.status_code, 200)
        names = [x["name"] for x in r.json()["results"]]
        self.assertIn("Truck 7 · AD 12345", names)
        r = self.c.get(f"/api/v1/driver-scores/vehicle/{self.dev.pk}/?from={d}")
        body = json.loads(r.content)
        self.assertEqual(body["trips"], 1)
        self.assertEqual(len(body["trips_detail"]), 1)
        self.assertEqual(Client().get("/api/v1/driver-scores/").status_code in (401, 403), True)
