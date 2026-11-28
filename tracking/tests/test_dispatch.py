from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from tracking import dispatch, fleet
from tracking.models import Device, Driver, Job


def vehicle(name, imei, lat, lon, **kw):
    return Device.objects.create(imei=imei, name=name, last_fix_lat=lat, last_fix_lon=lon,
                                 last_fix_ts=timezone.now(), **kw)


class DispatchTests(TestCase):
    def setUp(self):
        self.near = vehicle("Near", "356307042441001", 24.46, 54.37)
        self.far = vehicle("Far", "356307042441002", 25.20, 55.27)
        self.shop = vehicle("Shop", "356307042441003", 24.461, 54.371, availability="maintenance")
        self.drv = Driver.objects.create(name="Ali")
        fleet.assign(self.near, self.drv)
        self.job = Job.objects.create(customer_name="Lab A", pickup_lat=24.45, pickup_lon=54.38,
                                      dropoff_lat=24.50, dropoff_lon=54.40, dropoff_address="Lab A, Musaffah")
        self.staff = User.objects.create_user("admin", password="pw-123456", is_staff=True)
        self.c = Client()
        self.c.force_login(self.staff)

    def test_reference_and_recommend(self):
        self.assertEqual(self.job.reference, f"JOB-{self.job.pk:05d}")
        rec = dispatch.recommend(self.job)
        self.assertEqual([r["v"].name for r in rec], ["Near", "Far"])  # maintenance excluded, nearest first
        self.assertEqual(rec[0]["driver"].driver, self.drv)
        Job.objects.create(customer_name="B", device=self.near, status="picked_up")
        self.assertEqual([r["v"].name for r in dispatch.recommend(self.job)], ["Far"])  # busy excluded

    def test_flow_and_public_link(self):
        self.c.post(f"/dispatch/{self.job.pk}/action/", {"action": "assign", "device": self.near.pk})
        self.job.refresh_from_db()
        self.assertEqual((self.job.status, self.job.driver), ("assigned", self.drv))
        url = f"/t/{self.job.tracking_token}/"
        anon = Client()
        self.assertEqual(anon.get(url).status_code, 200)
        self.assertIsNone(anon.get(url + "data.json").json()["vehicle"])  # not shown before pickup
        self.c.post(f"/dispatch/{self.job.pk}/action/", {"action": "picked_up"})
        d = anon.get(url + "data.json").json()
        self.assertEqual(d["vehicle"], [24.46, 54.37])
        self.assertTrue(d["eta_minutes"])
        body = anon.get(url).content.decode()
        self.assertNotIn("Ali", body)
        self.assertNotIn("Near", body)
        self.c.post(f"/dispatch/{self.job.pk}/action/", {"action": "delivered", "pod_name": "Reception"})
        self.job.refresh_from_db()
        self.assertEqual((self.job.status, self.job.pod_name), ("delivered", "Reception"))
        self.assertEqual(self.job.pod_lat, 24.46)
        self.assertIsNone(anon.get(url + "data.json").json()["vehicle"])
        with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(hours=7)):
            self.assertEqual(anon.get(url).status_code, 404)
        self.assertEqual(anon.get("/t/nope/").status_code, 404)
        self.assertEqual(list(self.job.events.values_list("status", flat=True)), ["assigned", "picked_up", "delivered"])

    def test_cancelled_link_dies_and_driver_permissions(self):
        u = User.objects.create_user("ali", password="pw-123456")
        self.drv.user = u
        self.drv.save()
        self.job.device, self.job.driver, self.job.status = self.near, self.drv, "assigned"
        self.job.save()
        c = Client()
        c.force_login(u)
        self.assertEqual(c.post(f"/dispatch/{self.job.pk}/action/", {"action": "cancelled"}).status_code, 403)
        c.post(f"/dispatch/{self.job.pk}/action/", {"action": "picked_up"})
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "picked_up")
        other = User.objects.create_user("x", password="pw-123456")
        c.force_login(other)
        self.assertEqual(c.post(f"/dispatch/{self.job.pk}/action/", {"action": "failed"}).status_code, 403)
        self.c.post(f"/dispatch/{self.job.pk}/action/", {"action": "cancelled"})
        self.assertEqual(Client().get(f"/t/{self.job.tracking_token}/").status_code, 404)

    def test_pages_form_and_api(self):
        self.assertEqual(self.c.get("/dispatch/").status_code, 200)
        self.assertEqual(self.c.get(f"/dispatch/{self.job.pk}/").status_code, 200)
        r = self.c.post("/dispatch/new/", {"save": "1", "customer_name": "Clinic", "device": self.far.pk,
                                           "tracking_enabled": "on", "pickup_lat": "24.4"})
        self.assertContains(r, "Enter both latitude and longitude")
        r = self.c.post("/dispatch/new/", {"save": "1", "customer_name": "Clinic", "device": self.far.pk, "tracking_enabled": "on"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Job.objects.get(customer_name="Clinic").status, "assigned")
        r = self.c.post("/api/v1/jobs/", {"customer_name": "API co", "device": self.near.pk})
        self.assertEqual(r.status_code, 201, r.content)
        jid = r.json()["id"]
        self.assertIn("/t/", r.json()["tracking_url"])
        r = self.c.post(f"/api/v1/jobs/{jid}/status/", {"status": "delivered", "pod_name": "Gate"}, content_type="application/json")
        self.assertEqual(r.json()["status"], "delivered")
        self.assertEqual([r["name"] for r in self.c.get(f"/api/v1/jobs/{self.job.pk}/recommend/").json()], ["Near"])  # Far is on the Clinic job
