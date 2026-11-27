import os
import random
import time

from django.conf import settings
from django.core.management.base import BaseCommand

from tracking import codec, ingest
from tracking.models import Connection, Device
from tracking.simulator import DEFAULT_ROUTE, SimState, make_record, step_route

DEMO = [
    ("356307042441013", "Self-test simulator", "TEST-01", "Test", "moving"),
    ("356307042441021", "Hiace 50452", "AD 50452", "Al Shamkha", "moving"),
    ("356307042441039", "Raize 99042", "AD 99042", "Al Shamkha", "idling"),
    ("356307042441047", "Unicorn 91872", "AD 91872", "Al Shamkha", "parked"),
    ("356307042441054", "Canter 31207", "AD 31207", "Baniyas", "moving"),
    ("356307042441062", "Pickup 77810", "AD 77810", "Baniyas", "nosignal"),
]


class Command(BaseCommand):
    help = ("Register demo devices and load ~2 h of synthetic history through the real ingest pipeline. "
            "Local trials only: refuses to run unless FLEET_ALLOW_DEMO_DATA=true or --i-want-demo-data is given. "
            "Remove it again with `manage.py clean_demo_data --yes`.")

    def add_arguments(self, parser):
        parser.add_argument("--devices-only", action="store_true")
        parser.add_argument("--minutes", type=int, default=120)
        parser.add_argument("--i-want-demo-data", action="store_true",
                            help="really create the fake vehicles (never on the live server)")

    def handle(self, *args, **o):
        allowed = os.environ.get("FLEET_ALLOW_DEMO_DATA", "").lower() in ("1", "true", "yes")
        if not (allowed or o["i_want_demo_data"]):
            self.stdout.write(self.style.WARNING(
                "seed_demo skipped: demo vehicles are disabled. Use --i-want-demo-data (or "
                "FLEET_ALLOW_DEMO_DATA=true) on a local test install only."))
            return
        now_ms = int(time.time() * 1000)
        for n, (imei, name, plate, group, kind) in enumerate(DEMO):
            dev, created = Device.objects.get_or_create(
                imei=imei, defaults=dict(name=name, plate=plate, group=group, model="FMC130" if n else "SIMULATOR",
                                         firmware="03.28.xx (demo)" if n else ""))
            self.stdout.write(f"{'created' if created else 'exists '} {name} ({dev.masked_imei})")
            if o["devices_only"] or dev.record_count:
                continue
            conn = Connection.objects.create(device=dev, claimed_imei=imei, remote="seed:0", source="seed", handshake="accepted")
            state = SimState(seg=(n * 2) % len(DEFAULT_ROUTE))
            minutes = o["minutes"]
            end_offset = 2 * 3600 * 1000 if kind == "nosignal" else 0
            step_s = 30
            recs = []
            for i in range(minutes * 60 // step_s):
                ts = now_ms - end_offset - (minutes * 60 - i * step_s) * 1000
                tail = i > (minutes * 60 // step_s) - 10
                if kind == "moving" or not tail:
                    speed = max(5, 50 + random.uniform(-15, 15)) if (i // 20) % 3 != 2 else 0
                    ign = True
                elif kind == "idling":
                    speed, ign = 0, True
                else:
                    speed, ign = 0, False
                lat, lon, ang = step_route(state, step_s, speed)
                recs.append(make_record(lat, lon, int(speed), ang, ts, ignition=ign, odometer_m=state.odometer_m))
            for i in range(0, len(recs), 10):
                frame = codec.encode_frame(recs[i:i + 10])
                res = ingest.ingest_frame(conn.id, dev.id, imei, frame)
                ingest.mark_ack(res.packet_id, "sent", res.ack)
            ingest.close_connection(conn.id, "seed complete")
            if kind == "nosignal":
                from datetime import datetime, timezone as tz
                last = datetime.fromtimestamp((now_ms - end_offset) / 1000, tz=tz.utc)
                Device.objects.filter(pk=dev.pk).update(last_packet_utc=last, last_connection_utc=last)
                Connection.objects.filter(pk=conn.id).update(opened_utc=last, closed_utc=last)
            self.stdout.write(f"   loaded {len(recs)} records")
        self.stdout.write(self.style.SUCCESS("Demo data ready."))
