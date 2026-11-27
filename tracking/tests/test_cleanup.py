import io
import time

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from tracking import cleanup, codec, ingest
from tracking.models import AVLRecord, Connection, Device, IOValue, PortEvent, RawPacket, SavedPlace, TestRun
from tracking.simulator import make_record


def _load(dev, n=5):
    conn_id, _, ok = ingest.open_connection("1.2.3.4:5000", dev.imei)
    assert ok
    now = int(time.time() * 1000)
    recs = [make_record(24.4 + i / 1000, 54.5, 40, 90, now - (n - i) * 30000, ignition=True, odometer_m=i * 100)
            for i in range(n)]
    res = ingest.ingest_frame(conn_id, dev.id, dev.imei, codec.encode_frame(recs))
    ingest.mark_ack(res.packet_id, "sent", res.ack)
    ingest.close_connection(conn_id, "done")
    PortEvent.objects.create(kind="connect", imei=dev.imei, connection_id=conn_id)
    return conn_id


class CleanDemoDataTests(TestCase):
    def setUp(self):
        call_command("seed_demo", "--minutes", "5", "--i-want-demo-data", stdout=io.StringIO())
        self.jeep = Device.objects.create(imei="350000000000999", name="Jeep Wrangler", plate="AD 1234")
        _load(self.jeep)
        self.jeep.refresh_from_db()
        ingest.open_connection("9.9.9.9:1", "111111111111111")          # rejected unknown IMEI
        PortEvent.objects.create(kind="refused", imei="111111111111111")
        demo = Device.objects.exclude(pk=self.jeep.pk).first()
        SavedPlace.objects.create(name="demo stop", latitude=24.4, longitude=54.5, device=demo)
        SavedPlace.objects.create(name="jeep stop", latitude=24.4, longitude=54.5, device=self.jeep)
        SavedPlace.objects.create(name="phone pin", latitude=24.4, longitude=54.5)
        TestRun.objects.create(target="x:1", passed=True, steps=[], summary="ok")
        self.jeep_records = AVLRecord.objects.filter(device=self.jeep).count()
        self.jeep_io = IOValue.objects.filter(record__device=self.jeep).count()

    def test_dry_run_deletes_nothing(self):
        before = (Device.objects.count(), AVLRecord.objects.count(), IOValue.objects.count())
        out = io.StringIO()
        call_command("clean_demo_data", stdout=out)
        self.assertIn("Dry run", out.getvalue())
        self.assertIn("Jeep Wrangler", out.getvalue())
        self.assertEqual(before, (Device.objects.count(), AVLRecord.objects.count(), IOValue.objects.count()))

    def test_real_run_keeps_only_jeep(self):
        call_command("clean_demo_data", "--yes", stdout=io.StringIO())
        self.assertEqual(list(Device.objects.values_list("name", flat=True)), ["Jeep Wrangler"])
        self.assertEqual(AVLRecord.objects.count(), self.jeep_records)
        self.assertEqual(IOValue.objects.count(), self.jeep_io)
        self.assertTrue(self.jeep_records > 0 and self.jeep_io > 0)
        self.assertFalse(RawPacket.objects.exclude(device=self.jeep).exists())
        self.assertFalse(Connection.objects.exclude(device=self.jeep).exists())
        self.assertEqual(set(PortEvent.objects.values_list("imei", flat=True)), {self.jeep.imei})
        self.assertEqual(set(SavedPlace.objects.values_list("name", flat=True)), {"jeep stop", "phone pin"})
        self.assertFalse(TestRun.objects.exists())
        self.jeep.refresh_from_db()
        self.assertEqual(self.jeep.record_count, self.jeep_records)

    def test_phone_places_option(self):
        call_command("clean_demo_data", "--yes", "--include-phone-places", stdout=io.StringIO())
        self.assertEqual(list(SavedPlace.objects.values_list("name", flat=True)), ["jeep stop"])

    def test_refuses_when_nothing_would_be_kept(self):
        with self.assertRaises(CommandError):
            call_command("clean_demo_data", "--yes", "--keep", "nonexistent", stdout=io.StringIO())
        self.assertTrue(Device.objects.count() > 1)


class SeedAndSelftestTests(TestCase):
    def test_seed_demo_refuses_by_default(self):
        call_command("seed_demo", stdout=io.StringIO())
        self.assertFalse(Device.objects.exists())

    def test_selftest_traffic_removed(self):
        jeep = Device.objects.create(imei="350000000000999", name="Jeep Wrangler")
        _load(jeep)
        sim = Device.objects.create(imei="356307042441013", name="Self-test simulator", model="SIMULATOR")
        _load(sim)
        ingest.open_connection("x", cleanup.UNKNOWN_SELFTEST_IMEI)
        cleanup.remove_selftest_data("356307042441013")
        self.assertEqual(list(Device.objects.values_list("name", flat=True)), ["Jeep Wrangler"])
        self.assertFalse(Connection.objects.exclude(device=jeep).exists())
        self.assertFalse(AVLRecord.objects.exclude(device=jeep).exists())
        self.assertTrue(AVLRecord.objects.filter(device=jeep).exists())
