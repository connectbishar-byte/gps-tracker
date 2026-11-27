from datetime import timedelta

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.utils import timezone

from tracking import driverscore, vehicledash
from tracking.models import Device, RawPacket, SavedPlace
from tracking.tests import test_driverscore as tds


class VehicleDashboardTests(TestCase):
    rec = tds.DriverScoreTests.rec
    drive = tds.DriverScoreTests.drive

    def setUp(self):
        cache.clear()
        self.dev = Device.objects.create(imei="356307042441013", name="Truck 7", plate="AD 12345")
        self.pkt = RawPacket.objects.create(device=self.dev, hex="00", size=1, sha256="x")
        self.u = User.objects.create_user("driver", password="pw-12345678")
        self.c = Client()
        self.c.login(username="driver", password="pw-12345678")
        self.n = 0

    def _build(self, days=7):
        today = timezone.localdate()
        d_from = today - timedelta(days=days - 1)
        _, _, start, end, _ = driverscore.parse_range({"from": d_from.isoformat(), "to": today.isoformat()})
        return vehicledash.build(self.dev, d_from, today, start, end)

    def test_daily_rows_and_totals_match_score_sheet(self):
        self.drive(tds._day_start(1) + timedelta(hours=8), minutes=30, kmh=60)
        self.drive(tds._day_start(3) + timedelta(hours=14), minutes=20, kmh=60)
        data = self._build()
        self.assertEqual(len(data["daily"]), 7)
        by_day = {r["date"]: r for r in data["daily"]}
        d1 = (timezone.localdate() - timedelta(days=1)).isoformat()
        d3 = (timezone.localdate() - timedelta(days=3)).isoformat()
        self.assertAlmostEqual(by_day[d1]["km"], 29, delta=1.5)
        self.assertAlmostEqual(by_day[d3]["km"], 19, delta=1.5)
        self.assertEqual(by_day[d1]["trips"], 1)
        self.assertEqual(by_day[d1]["score"], 100)
        self.assertEqual(by_day[timezone.localdate().isoformat()]["km"], 0)
        self.assertIsNone(by_day[timezone.localdate().isoformat()]["score"])
        self.assertEqual(data["totals"]["trips"], 2)
        self.assertEqual(data["totals"]["active_days"], 2)
        self.assertEqual(data["totals"]["best_day"]["date"], d1)
        # same engine as the score sheet
        sheet = self.c.get(f"/api/v1/driver-scores/vehicle/{self.dev.pk}/").json()
        self.assertAlmostEqual(sheet["distance_km"], data["totals"]["km"], delta=0.1)
        self.assertEqual(sheet["score"], data["score"]["score"])

    def test_speed_profile_and_heatmap(self):
        self.drive(tds._day_start(1) + timedelta(hours=8), minutes=30, kmh=60)
        data = self._build()
        prof = {b["label"]: b for b in data["speed_profile"]}
        self.assertGreater(prof["60–80"]["pct"], 90)
        self.assertAlmostEqual(sum(b["pct"] for b in data["speed_profile"]), 100, delta=0.5)
        d1 = (timezone.localdate() - timedelta(days=1)).isoformat()
        self.assertGreater(data["heatmap"][d1][8], 25)   # most of the 30 minutes fall in 08:00-09:00
        self.assertGreater(data["hours"][8], 25)
        self.assertIsNone(self._build(days=40)["heatmap"])  # long ranges show hours of the day instead

    def test_drivers_from_car_view(self):
        SavedPlace.objects.create(owner=self.u, device=self.dev, kind="trip", name="Morning run", latitude=24.4,
                                  longitude=54.3, distance_km=42, duration_s=3000, moving_s=2800,
                                  created_utc=timezone.now() - timedelta(hours=2))
        data = self._build()
        row = next(r for r in data["drivers"] if r["source"] == "car view")
        self.assertEqual(row["id"], self.u.pk)
        self.assertEqual(row["km"], 42)
        self.assertEqual(row["score"], 100)

    def test_page_and_api(self):
        self.drive(tds._day_start(1) + timedelta(hours=8), minutes=10, kmh=50)
        r = self.c.get(f"/vehicles/{self.dev.pk}/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Daily distance")
        self.assertContains(r, 'id="vdData"')
        r = self.c.get(f"/vehicles/{self.dev.pk}/?from=2026-11-01&to=2026-11-30")
        self.assertEqual(r.status_code, 200)
        r = self.c.get(f"/api/v1/vehicles/{self.dev.pk}/dashboard/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["daily"]), 7)
        self.assertEqual(self.c.get("/vehicles/99999/").status_code, 404)

    def test_login_required(self):
        self.assertEqual(Client().get(f"/vehicles/{self.dev.pk}/").status_code, 302)
        self.assertIn(Client().get(f"/api/v1/vehicles/{self.dev.pk}/dashboard/").status_code, (401, 403))
