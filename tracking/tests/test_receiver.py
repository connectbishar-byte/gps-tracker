"""End-to-end tests: real asyncio receiver on a random port + simulated device over TCP."""
import asyncio
import socket
import threading
import time

from django.test import TransactionTestCase, override_settings, Client
from django.conf import settings
from django.contrib.auth.models import User

from tracking import codec, ingest
from tracking.models import AVLRecord, Connection, Device, RawPacket
from tracking.receiver import Receiver
from tracking.simulator import DeviceClient, make_record, run_connectivity_suite

IMEI = "356307042441013"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class ReceiverTests(TransactionTestCase):
    def setUp(self):
        self.dev = Device.objects.create(imei=IMEI, name="Bench FMC130")
        self.port = free_port()
        self.loop = asyncio.new_event_loop()
        self.rx = Receiver("127.0.0.1", self.port)
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self.rx.start())
            ready.set()
            self.loop.run_forever()
        self.t = threading.Thread(target=run, daemon=True)
        self.t.start()
        ready.wait(5)

    def tearDown(self):
        async def shutdown():
            self.rx.server.close()
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(shutdown(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.t.join(5)
        self.rx.db.shutdown(wait=True)

    def dev_client(self, imei=IMEI):
        c = DeviceClient("127.0.0.1", self.port, imei, timeout=5)
        c.connect()
        return c

    def test_unknown_imei_rejected(self):
        c = self.dev_client("111111111111111")
        self.assertEqual(c.handshake(), 0)
        c.close()
        time.sleep(0.2)
        self.assertTrue(Connection.objects.filter(claimed_imei="111111111111111", handshake="rejected").exists())

    def test_disabled_device_rejected(self):
        Device.objects.filter(pk=self.dev.pk).update(enabled=False)
        c = self.dev_client()
        self.assertEqual(c.handshake(), 0)
        c.close()

    def test_ack_equals_committed_records(self):
        c = self.dev_client()
        self.assertEqual(c.handshake(), 1)
        recs = [make_record(24.39 + i / 1000, 54.71, 40, 90, 1_760_000_000_000 + i * 1000) for i in range(5)]
        self.assertEqual(c.send_frame(codec.encode_frame(recs)), 5)
        c.close()
        time.sleep(0.2)
        self.assertEqual(AVLRecord.objects.filter(device=self.dev).count(), 5)
        pkt = RawPacket.objects.get()
        self.assertEqual((pkt.ack_status, pkt.ack_value, pkt.new_records), ("sent", 5, 5))
        self.dev.refresh_from_db()
        self.assertAlmostEqual(self.dev.last_fix_lat, 24.394, places=5)

    def test_replay_is_idempotent(self):
        frame = codec.encode_frame([make_record(24.39, 54.71, 40, 90, 1_760_000_000_000)])
        c = self.dev_client()
        c.handshake()
        self.assertEqual(c.send_frame(frame), 1)
        self.assertEqual(c.send_frame(frame), 1)
        c.close()
        time.sleep(0.2)
        self.assertEqual(AVLRecord.objects.count(), 1)
        self.assertEqual(RawPacket.objects.count(), 2)  # arrival audit retained
        self.assertEqual(RawPacket.objects.order_by("id").last().duplicate_records, 1)

    def test_bad_crc_nack_and_stored(self):
        c = self.dev_client()
        c.handshake()
        self.assertEqual(c.send_frame(codec.encode_frame([make_record(24.39, 54.71, 1, 1)], corrupt_crc=True)), 0)
        c.close()
        time.sleep(0.2)
        self.assertEqual(AVLRecord.objects.count(), 0)
        p = RawPacket.objects.get()
        self.assertEqual(p.parse_status, "error")
        self.assertFalse(p.crc_ok)

    def test_fragmented_and_coalesced(self):
        c = self.dev_client()
        c.handshake()
        f1 = codec.encode_frame([make_record(24.39, 54.71, 30, 10, 1_760_000_000_000)], codec.CODEC_8)
        self.assertEqual(c.send_frame(f1, fragment=3), 1)
        f2 = codec.encode_frame([make_record(24.40, 54.72, 30, 10, 1_760_000_001_000)])
        f3 = codec.encode_frame([make_record(24.41, 54.73, 30, 10, 1_760_000_002_000)] * 1)
        c.send_raw(f2 + f3)
        self.assertEqual((c.read_ack(), c.read_ack()), (1, 1))
        c.close()

    def test_late_record_does_not_overwrite_newer_fix(self):
        c = self.dev_client()
        c.handshake()
        c.send_frame(codec.encode_frame([make_record(24.40, 54.72, 30, 10, 1_760_000_100_000)]))
        c.send_frame(codec.encode_frame([make_record(24.30, 54.60, 30, 10, 1_760_000_000_000)]))
        c.close()
        time.sleep(0.2)
        self.dev.refresh_from_db()
        self.assertAlmostEqual(self.dev.last_fix_lat, 24.40, places=5)
        self.assertTrue(AVLRecord.objects.get(latitude__lt=24.35).late)

    def test_invalid_fix_not_used_for_map(self):
        c = self.dev_client()
        c.handshake()
        c.send_frame(codec.encode_frame([make_record(24.40, 54.72, 0, 0, 1_760_000_000_000, valid=False)]))
        c.close()
        time.sleep(0.2)
        self.dev.refresh_from_db()
        self.assertIsNone(self.dev.last_fix_ts)
        self.assertFalse(AVLRecord.objects.get().valid_fix)

    def test_db_failure_means_no_ack(self):
        fleet = dict(settings.FLEET, FAULT_INJECT_IMEIS={IMEI})
        with override_settings(FLEET=fleet):
            c = self.dev_client()
            c.handshake()
            c.sock.sendall(codec.encode_frame([make_record(24.40, 54.72, 10, 0)]))
            c.sock.settimeout(3)
            data = c.sock.recv(4)
            c.close()
        self.assertEqual(data, b"", "server must close without ACK when the DB transaction fails")
        time.sleep(0.2)
        self.assertEqual(AVLRecord.objects.count(), 0)
        self.assertTrue(RawPacket.objects.filter(parse_status="error", error__contains="not ACKed").exists())

    def test_full_connectivity_suite(self):
        steps = run_connectivity_suite("127.0.0.1", self.port, IMEI, timeout=5)
        failed = [s for s in steps if not s["ok"]]
        self.assertFalse(failed, failed)


class WebTests(TransactionTestCase):
    def setUp(self):
        self.staff = User.objects.create_user("ops", password="pw-12345678", is_staff=True)
        self.viewer = User.objects.create_user("viewer", password="pw-12345678")
        d = Device.objects.create(imei=IMEI, name="Bench")
        conn = Connection.objects.create(device=d, remote="t", handshake="accepted")
        ingest.ingest_frame(conn.id, d.id, IMEI, codec.encode_frame([make_record(24.39, 54.71, 30, 10)]))

    def test_login_required(self):
        self.assertEqual(Client().get("/").status_code, 302)
        self.assertEqual(Client().get("/api/fleet/").status_code, 302)

    def test_pages_render(self):
        c = Client()
        c.login(username="ops", password="pw-12345678")
        for url in ["/", "/live/", "/history/", "/records/", "/raw/", "/devices/", "/connectivity/", "/settings/",
                    f"/devices/{Device.objects.get().id}/", f"/records/{AVLRecord.objects.get().id}/",
                    f"/raw/{RawPacket.objects.get().id}/", "/api/fleet/", f"/api/history/?device={Device.objects.get().id}"]:
            self.assertEqual(c.get(url).status_code, 200, url)
        data = c.get("/api/fleet/").json()
        self.assertEqual(data["devices"][0]["state"], "moving")

    def test_raw_restricted_and_export_masks_imei(self):
        c = Client()
        c.login(username="viewer", password="pw-12345678")
        self.assertEqual(c.get("/raw/").status_code, 302)
        c.login(username="ops", password="pw-12345678")
        csv = c.get("/raw/?export=csv").content.decode()
        self.assertNotIn(IMEI, csv)
        self.assertIn("3563*******1013", csv)
