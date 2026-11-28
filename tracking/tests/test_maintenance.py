from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase

from tracking import maintenance
from tracking.models import Device, Expense, MaintenancePlan, WorkOrder
from tracking.tests.test_fleet import send
from tracking.simulator import make_record


class DueTests(TestCase):
    def setUp(self):
        self.v = Device.objects.create(imei="356307042441013", name="Truck 1", odometer_km=20000)
        self.today = date(2026, 11, 28)

    def test_km_and_days(self):
        p = MaintenancePlan(device=self.v, name="Oil", every_km=10000, every_days=180, last_done_km=10500,
                            last_done_date=self.today - timedelta(days=100))
        s = maintenance.plan_status(p, 20200, self.today)
        self.assertEqual((s["km_left"], s["state"]), (300, "due"))
        self.assertEqual(maintenance.plan_status(p, 21000, self.today)["state"], "overdue")
        self.assertEqual(maintenance.plan_status(p, 15000, self.today)["state"], "ok")
        p.last_done_date = self.today - timedelta(days=200)
        self.assertEqual(maintenance.plan_status(p, 15000, self.today)["state"], "overdue")
        self.assertEqual(maintenance.plan_status(MaintenancePlan(device=self.v, name="x", every_km=5000), 1, self.today)["state"], "unknown")

    def test_done_work_order_moves_plan_and_reminders(self):
        p = MaintenancePlan.objects.create(device=self.v, name="Oil", every_km=10000, last_done_km=10200)
        with mock.patch("tracking.whatsapp.send", return_value={"ok": True, "sent": 1, "detail": "ok"}) as m:
            res = maintenance.send_reminders(self.today)
            self.assertEqual(res["count"], 1)
            self.assertIn("Truck 1: Oil in 200 km", m.call_args[0][0])
            self.assertEqual(maintenance.send_reminders(self.today)["count"], 0)  # weekly
        wo = WorkOrder.objects.create(device=self.v, plan=p, title="Oil", status="done", completed=self.today, odometer_km=20050)
        maintenance.apply_done(wo)
        p.refresh_from_db()
        self.assertEqual((p.last_done_km, p.last_done_date, p.last_reminder_on), (20050, self.today, None))

    def test_tracker_odometer_preferred(self):
        r = make_record(24.47, 54.34, 30, 0, odometer_m=123456000)
        send(self.v, [r])
        from tracking import fleet
        self.assertEqual(fleet.odometer(self.v)["km"], 123456.0)


class FuelTests(TestCase):
    def test_consumption_and_flag(self):
        v = Device.objects.create(imei="356307042441013", name="Truck 1")
        d0 = date(2026, 11, 1)
        fills = [(0, 50, 10000), (1, 40, 10500), (2, 40, 11000), (3, 40, 11500), (4, 30, 11800), (5, 60, 12100)]
        for i, l, km in fills:
            Expense.objects.create(device=v, category="fuel", amount=Decimal("100"), litres=l, odometer_km=km, date=d0 + timedelta(days=i))
        rows = maintenance.fuel_rows(v)
        by_date = {r["e"].date: r for r in rows}
        self.assertEqual(by_date[d0 + timedelta(days=1)]["l100"], 8.0)
        self.assertEqual(by_date[d0 + timedelta(days=4)]["l100"], 10.0)
        self.assertTrue(by_date[d0 + timedelta(days=5)]["flag"])  # 20 L/100 vs median 8
        self.assertFalse(by_date[d0 + timedelta(days=4)]["flag"])


class PageTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("admin", password="pw-123456", is_staff=True)
        self.v = Device.objects.create(imei="356307042441013", name="Truck 1")
        self.c = Client()
        self.c.force_login(self.staff)
        send(self.v, [make_record(24.47 + i * 0.002, 54.34, 40, 0) for i in range(3)])

    def test_pages_and_forms(self):
        r = self.c.post("/maintenance/plans/new/", {"device": self.v.pk, "name": "Oil", "every_km": 10000, "remind_km": 500,
                                                    "remind_days": 14, "active": "on", "last_done_km": 0})
        self.assertEqual(r.status_code, 302)
        r = self.c.post("/maintenance/plans/new/", {"device": self.v.pk, "name": "x", "remind_km": 500, "remind_days": 14})
        self.assertContains(r, "Set every N km")
        r = self.c.post("/maintenance/expenses/new/", {"device": self.v.pk, "date": "2026-11-20", "category": "fuel", "amount": "120"})
        self.assertContains(r, "Enter the litres")
        r = self.c.post("/maintenance/expenses/new/", {"device": self.v.pk, "date": "2026-11-20", "category": "toll", "amount": "4"})
        self.assertEqual(r.status_code, 302)
        r = self.c.post("/maintenance/orders/new/", {"device": self.v.pk, "title": "Brakes", "status": "done", "cost": "800"})
        self.assertEqual(WorkOrder.objects.get().completed is not None, True)
        for tab in ["due", "plans", "orders", "expenses", "fuel"]:
            self.assertEqual(self.c.get(f"/maintenance/?tab={tab}").status_code, 200)

    def test_reports(self):
        Expense.objects.create(device=self.v, category="toll", amount=Decimal("8"))
        for kind, _ in maintenance.REPORTS:
            r = self.c.get(f"/reports/?report={kind}")
            self.assertEqual(r.status_code, 200, kind)
            r = self.c.get(f"/reports/?report={kind}&format=csv")
            self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("Truck 1", self.c.get("/reports/?report=costs&format=csv").content.decode())

    def test_api(self):
        r = self.c.post("/api/v1/expenses/", {"device": self.v.pk, "category": "fuel", "amount": "90", "litres": 30})
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["created_by"], "admin")
        r = self.c.post("/api/v1/maintenance-plans/", {"device": self.v.pk, "name": "Oil", "every_days": 90, "last_done_date": "2026-01-01"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.c.get("/api/v1/maintenance-plans/").json()[0]["status"]["state"], "overdue")
