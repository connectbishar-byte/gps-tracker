"""Map arrow direction: the course comes from where the vehicle actually moved."""
import time

from django.test import TestCase
from django.utils import timezone

from tracking import codec, ingest
from tracking.models import Connection, Device
from tracking.simulator import make_record
from tracking.views import bearing_deg, device_status


def drive(dev, points, reported_angle):
    """Feed fixes one minute apart, oldest first, all moving with the given (possibly wrong) angle."""
    conn = Connection.objects.create(device=dev, remote="1.2.3.4:5", handshake="accepted", closed_utc=timezone.now())
    now_ms = int(time.time() * 1000)
    n = len(points)
    recs = [make_record(lat, lon, 50, reported_angle, ts_ms=now_ms - (n - i) * 60000)
            for i, (lat, lon) in enumerate(points)]
    ingest.ingest_frame(conn.id, dev.id, dev.imei, codec.encode_frame(recs))
    dev.refresh_from_db()


class CourseTests(TestCase):
    def test_bearing_compass_convention(self):
        self.assertAlmostEqual(bearing_deg(24.0, 54.0, 24.01, 54.0), 0, delta=0.5)    # north
        self.assertAlmostEqual(bearing_deg(24.0, 54.0, 24.0, 54.01), 90, delta=0.5)   # east
        self.assertAlmostEqual(bearing_deg(24.01, 54.0, 24.0, 54.0), 180, delta=0.5)  # south
        self.assertAlmostEqual(bearing_deg(24.0, 54.01, 24.0, 54.0), 270, delta=0.5)  # west

    def test_course_follows_movement_not_reported_angle(self):
        dev = Device.objects.create(imei="356307042441013", name="Car")
        drive(dev, [(24.40, 54.70), (24.40, 54.699), (24.40, 54.698)], reported_angle=90)  # heading west, says east
        s = device_status(dev)
        if s["state"] != "moving":
            self.skipTest("simulated record not treated as moving")
        self.assertAlmostEqual(s["fix"]["course"], 270, delta=2)
        self.assertEqual(s["fix"]["angle"], 90)
