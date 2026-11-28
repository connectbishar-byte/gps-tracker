import time
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from tracking import codec, fleet, ingest
from tracking.models import Device, Driver, DriverAssignment, FleetDocument
from tracking.simulator import io_int, make_record


def send(dev, recs):
    conn_id, _, _ = ingest.open_connection("1.2.3.4:5000", dev.imei)
    return ingest.ingest_frame(conn_id, dev.id, dev.imei, codec.encode_frame(recs))


class AssignmentTests(TestCase):
    def setUp(self):
        self.v1 = Device.objects.create(imei="356307042441013", name="Truck 1")
        self.v2 = Device.objects.create(imei="356307042441014", name="Truck 2")
        self.a = Driver.objects.create(name="Ali", ibutton_id="01A2B3C4")
        self.b = Driver.objects.create(name="Bilal", ibutton_id="98765")

    def test_assign_closes_previous(self):
        fleet.assign(self.v1, self.a)
        fleet.assign(self.v1, self.b)
        self.assertEqual(fleet.current_assignment(self.v1).driver, self.b)
        self.assertEqual(DriverAssignment.objects.filter(device=self.v1, end_utc__isnull=True).count(), 1)
        # the same driver moving to another vehicle leaves the first one
        fleet.assign(self.v2, self.b)
        self.assertIsNone(fleet.current_assignment(self.v1))
        fleet.assign(self.v2, None)
        self.assertIsNone(fleet.current_assignment(self.v2))

    def test_ibutton_keys(self):
        self.assertIn(0x01A2B3C4, fleet._ibutton_keys("01:A2:B3:C4"))
        self.assertIn(98765, fleet._ibutton_keys("98765"))
        self.assertEqual(fleet.driver_for_ibutton(0x01A2B3C4), self.a)
        self.assertEqual(fleet.driver_for_ibutton(98765), self.b)
        self.assertIsNone(fleet.driver_for_ibutton(0))

    def test_ibutton_in_records_assigns_driver(self):
        t = int(time.time() * 1000) - 60_000
        r1 = make_record(24.47, 54.34, 30, 0, t)
        r1.io.append(io_int(78, 0x01A2B3C4, 8))
        r2 = make_record(24.471, 54.341, 30, 0, t + 10_000)
        send(self.v1, [r1, r2])
        a = fleet.current_assignment(self.v1)
        self.assertEqual(a.driver, self.a)
        self.assertEqual(a.source, "ibutton")
        # resending the same frame does not create a second assignment
        send(self.v1, [r1, r2])
        self.assertEqual(DriverAssignment.objects.filter(device=self.v1).count(), 1)

    def test_unknown_ibutton_ignored(self):
        r = make_record(24.47, 54.34, 30, 0)
        r.io.append(io_int(78, 0x55, 8))
        send(self.v1, [r])
        self.assertIsNone(fleet.current_assignment(self.v1))


class DocumentTests(TestCase):
    def setUp(self):
        self.v = Device.objects.create(imei="356307042441013", name="Truck 1")
        self.today = date(2026, 11, 28)

    def test_states(self):
        d = FleetDocument(kind="insurance", device=self.v, expiry=self.today + timedelta(days=10), remind_days=30)
        self.assertEqual(d.state(self.today), "due")
        d.expiry = self.today - timedelta(days=1)
        self.assertEqual(d.state(self.today), "expired")
        d.expiry = self.today + timedelta(days=90)
        self.assertEqual(d.state(self.today), "ok")
        d.expiry = None
        self.assertEqual(d.state(self.today), "none")

    def test_reminders_sent_once_a_week(self):
        FleetDocument.objects.create(kind="insurance", number="POL-1", device=self.v, expiry=self.today + timedelta(days=5))
        FleetDocument.objects.create(kind="registration", device=self.v, expiry=self.today + timedelta(days=200))
        with mock.patch("tracking.whatsapp.send", return_value={"ok": True, "sent": 1, "detail": "sent to 1"}) as m:
            res = fleet.send_reminders(self.today)
            self.assertEqual(res["count"], 1)
            self.assertIn("POL-1", m.call_args[0][0])
            self.assertIn("expires in 5 day(s)", m.call_args[0][0])
            self.assertEqual(fleet.send_reminders(self.today + timedelta(days=3))["count"], 0)
            self.assertEqual(fleet.send_reminders(self.today + timedelta(days=7))["count"], 1)
        self.assertEqual(m.call_count, 2)

    def test_failed_send_retries(self):
        FleetDocument.objects.create(kind="insurance", device=self.v, expiry=self.today)
        with mock.patch("tracking.whatsapp.send", return_value={"ok": False, "sent": 0, "detail": "not set up"}):
            fleet.send_reminders(self.today)
        self.assertIsNone(FleetDocument.objects.get().last_reminder_on)


class PageTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("admin", password="pw-123456", is_staff=True)
        self.viewer = User.objects.create_user("viewer", password="pw-123456")
        self.v = Device.objects.create(imei="356307042441013", name="Truck 1", plate="AD 12345")
        self.d = Driver.objects.create(name="Ali")
        self.c = Client()
        self.c.force_login(self.staff)

    def test_pages_render(self):
        FleetDocument.objects.create(kind="insurance", device=self.v, expiry=timezone.localdate())
        for url in ["/fleet/", "/fleet/?tab=documents", f"/fleet/vehicles/{self.v.pk}/", "/fleet/drivers/new/",
                    f"/fleet/drivers/{self.d.pk}/", "/fleet/documents/new/?device=1", "/fleet/documents.csv"]:
            self.assertEqual(self.c.get(url).status_code, 200, url)
        self.assertContains(self.c.get("/fleet/"), "AD 12345")

    def test_assign_and_edit(self):
        r = self.c.post("/fleet/assign/", {"device": self.v.pk, "driver": self.d.pk, "next": "/fleet/"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(fleet.current_assignment(self.v).driver, self.d)
        r = self.c.post(f"/fleet/vehicles/{self.v.pk}/", {"name": "Truck 1", "plate": "AD 1", "group": "Default",
                                                          "vehicle_type": "truck", "availability": "maintenance",
                                                          "odometer_km": "12000", "odometer_date": "2026-11-01"})
        self.assertEqual(r.status_code, 302)
        self.v.refresh_from_db()
        self.assertEqual(self.v.availability, "maintenance")
        self.assertEqual(fleet.odometer(self.v)["km"], 12000.0)

    def test_document_needs_one_owner(self):
        r = self.c.post("/fleet/documents/new/", {"kind": "permit", "remind_days": 30})
        self.assertContains(r, "Pick the vehicle or the driver")
        r = self.c.post("/fleet/documents/new/", {"kind": "licence", "driver": self.d.pk, "remind_days": 30,
                                                  "expiry": "2027-01-01"})
        self.assertEqual(r.status_code, 302)

    def test_viewer_cannot_edit(self):
        c = Client()
        c.force_login(self.viewer)
        self.assertEqual(c.get("/fleet/").status_code, 200)
        self.assertEqual(c.post("/fleet/assign/", {"device": self.v.pk, "driver": self.d.pk}).status_code, 302)  # to login
        self.assertIsNone(fleet.current_assignment(self.v))
        self.assertEqual(c.post(f"/fleet/drivers/{self.d.pk}/", {"name": "X"}).status_code, 403)

    def test_api(self):
        r = self.c.post("/api/v1/assignments/", {"driver": self.d.pk, "device": self.v.pk})
        self.assertEqual(r.status_code, 201, r.content)
        r = self.c.get("/api/v1/drivers/")
        self.assertEqual(r.json()[0]["current_vehicle"]["id"], self.v.pk)
        r = self.c.post("/api/v1/documents/", {"kind": "insurance", "device": self.v.pk, "driver": self.d.pk})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/api/v1/documents/", {"kind": "insurance", "device": self.v.pk, "expiry": "2020-01-01"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["state"], "expired")
        self.assertEqual(len(self.c.get("/api/v1/documents/?due=1").json()), 1)
