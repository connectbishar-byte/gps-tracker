import io
import json
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase

from tracking import places
from tracking.models import Device, SavedPlace

TRIP = {
    "kind": "trip", "name": "", "source": "phone", "latitude": 24.4539, "longitude": 54.3773, "accuracy_m": 6,
    "address": "Corniche Road, Al Khalidiyah, Abu Dhabi", "started_utc": "2026-11-27T06:00:00Z",
    "ended_utc": "2026-11-27T06:30:00Z", "start_latitude": 24.40, "start_longitude": 54.50,
    "start_address": "Al Shamkha", "distance_km": 21.4, "duration_s": 1800, "moving_s": 1500,
    "max_speed_kmh": 118, "avg_speed_kmh": 51.4, "driver_score": 74, "driver_rating": "fair",
    "harsh_accel": 1, "harsh_brake": 2, "harsh_turn": 0, "overspeed_s": 30,
    "track": [[24.40, 54.50], [24.42, 54.45], [24.4539, 54.3773]],
}


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class PlacesTests(TestCase):
    def setUp(self):
        cache.clear()
        self.u = User.objects.create_user("driver", password="pw-12345678")
        self.other = User.objects.create_user("other", password="pw-12345678")
        self.staff = User.objects.create_user("ops", password="pw-12345678", is_staff=True)
        self.c = Client(enforce_csrf_checks=False)
        self.c.login(username="driver", password="pw-12345678")

    def post(self, body, client=None):
        return (client or self.c).post("/api/v1/places/", json.dumps(body), content_type="application/json")

    def test_pages_render(self):
        Device.objects.create(imei="356307042441013", name="Truck 7")
        r = self.c.get("/drive/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Truck 7")
        self.assertContains(r, "drive.js")
        self.assertEqual(self.c.get("/places/").status_code, 200)
        self.assertEqual(Client().get("/drive/").status_code, 302)  # sign-in needed

    def test_stop_saves_trip_with_summary_and_default_name(self):
        r = self.post(TRIP)
        self.assertEqual(r.status_code, 201, r.content)
        p = SavedPlace.objects.get()
        self.assertEqual(p.owner, self.u)
        self.assertEqual(p.driver_rating, "fair")
        self.assertEqual(len(p.track), 3)
        self.assertTrue(p.name.startswith("Corniche Road · "))
        self.assertIn("google.com/maps/dir/", r.json()["directions_url"])
        r = self.c.get("/places/")
        self.assertContains(r, "Corniche Road")

    def test_bad_data_rejected(self):
        self.assertEqual(self.post(dict(TRIP, latitude=123)).status_code, 400)
        self.assertEqual(self.post(dict(TRIP, driver_rating="crazy")).status_code, 400)
        self.assertEqual(self.post(dict(TRIP, track=[[1]])).status_code, 400)

    def test_long_track_is_thinned(self):
        track = [[24 + i / 1e5, 54] for i in range(5000)]
        self.assertEqual(self.post(dict(TRIP, track=track)).status_code, 201)
        t = SavedPlace.objects.get().track
        self.assertEqual(len(t), places.MAX_TRACK_POINTS)
        self.assertEqual(t[0], track[0])
        self.assertEqual(t[-1], [round(track[-1][0], 6), 54])

    def test_users_see_only_their_places_staff_see_all(self):
        self.post(TRIP)
        mine = SavedPlace.objects.get()
        o = Client(); o.login(username="other", password="pw-12345678")
        self.assertEqual(o.get("/api/v1/places/").json()["count"], 0)
        self.assertEqual(o.patch(f"/api/v1/places/{mine.id}/", {"name": "x"}, content_type="application/json").status_code, 404)
        self.assertEqual(o.post(f"/places/{mine.id}/delete/").status_code, 404)
        s = Client(); s.login(username="ops", password="pw-12345678")
        self.assertContains(s.get("/places/"), "by driver")
        r = self.c.patch(f"/api/v1/places/{mine.id}/", {"name": "Home", "notes": "gate 3"}, content_type="application/json")
        self.assertEqual(r.status_code, 200)
        mine.refresh_from_db()
        self.assertEqual((mine.name, mine.notes), ("Home", "gate 3"))
        self.assertEqual(self.c.post(f"/places/{mine.id}/delete/").status_code, 302)
        self.assertFalse(SavedPlace.objects.exists())

    def test_exports(self):
        self.post(TRIP)
        self.post(dict(TRIP, kind="pin", name="Fuel <station>", track=[]))
        kml = self.c.get("/places/export.kml")
        self.assertEqual(kml["Content-Type"], "application/vnd.google-earth.kml+xml")
        body = kml.content.decode()
        self.assertIn("<coordinates>54.377300,24.453900,0</coordinates>", body)
        self.assertIn("Fuel &lt;station&gt;", body)
        self.assertIn("<LineString>", body)
        csv_ = self.c.get("/places/export.csv").content.decode("utf-8-sig")
        self.assertTrue(csv_.startswith("name,latitude,longitude"))
        self.assertIn("24.453900,54.377300", csv_)
        gj = json.loads(self.c.get("/places/export.geojson").content)
        self.assertEqual(gj["features"][0]["geometry"]["type"], "Point")
        self.assertEqual(self.c.get("/places/export.exe").status_code, 404)
        one = SavedPlace.objects.filter(kind="pin").get()
        self.assertNotIn("<LineString>", self.c.get(f"/places/export.kml?ids={one.id}").content.decode())

    def test_import_round_trip_kml_csv_geojson(self):
        self.post(TRIP)
        kml = self.c.get("/places/export.kml").content
        csv_ = self.c.get("/places/export.csv").content
        gj = self.c.get("/places/export.geojson").content
        for name, data in (("a.kml", kml), ("b.csv", csv_), ("c.geojson", gj)):
            r = self.c.post("/places/import/", {"file": SimpleUploadedFile(name, data)})
            self.assertEqual(r.status_code, 302)
        imported = SavedPlace.objects.filter(kind="import")
        self.assertEqual(imported.count(), 3)
        for p in imported:
            self.assertAlmostEqual(p.latitude, 24.4539, places=4)
            self.assertAlmostEqual(p.longitude, 54.3773, places=4)
            self.assertEqual(p.owner, self.u)

    def test_import_rejects_entities_and_junk(self):
        evil = b'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY a "aaaa">]><kml><Placemark><name>&a;</name></Placemark></kml>'
        self.c.post("/places/import/", {"file": SimpleUploadedFile("x.kml", evil)})
        self.c.post("/places/import/", {"file": SimpleUploadedFile("x.csv", b"foo,bar\n1,2\n")})
        self.c.post("/places/import/", {"file": SimpleUploadedFile("x.exe", b"MZ")})
        self.assertFalse(SavedPlace.objects.exists())

    def test_geocode_caches_and_respects_rate_limit(self):
        doc = {"display_name": "Corniche Rd, Al Khalidiyah, Abu Dhabi, UAE",
               "address": {"road": "Corniche Road", "suburb": "Al Khalidiyah", "city": "Abu Dhabi"}}
        with mock.patch("urllib.request.urlopen", return_value=_Resp(json.dumps(doc).encode())) as m:
            r = self.c.get("/places/geocode/?lat=24.45391&lon=54.37731")
            self.assertEqual(r.json()["name"], "Corniche Road, Al Khalidiyah, Abu Dhabi")
            self.assertEqual(m.call_args[0][0].get_header("User-agent"), places.USER_AGENT)
            r = self.c.get("/places/geocode/?lat=24.45392&lon=54.37732")   # same ~10 m cell: cached
            self.assertEqual(r.status_code, 200)
            self.assertEqual(m.call_count, 1)
            r = self.c.get("/places/geocode/?lat=25.2&lon=55.27")   # new cell inside the 1 s window
            self.assertEqual(r.status_code, 503)
        self.assertEqual(self.c.get("/places/geocode/?lat=abc&lon=1").status_code, 400)
