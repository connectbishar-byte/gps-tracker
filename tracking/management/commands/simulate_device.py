import random
import threading
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from tracking import codec
from tracking.simulator import DEFAULT_ROUTE, DeviceClient, SimState, make_record, step_route


class Command(BaseCommand):
    help = "Simulate an FMC130 sending Codec 8/8E AVL data to a receiver over TCP."

    def add_arguments(self, parser):
        parser.add_argument("--host", default=settings.FLEET["SELFTEST_HOST"])
        parser.add_argument("--port", type=int, default=settings.FLEET["SELFTEST_PORT"])
        parser.add_argument("--imei", default=settings.FLEET["SELFTEST_IMEI"],
                            help="IMEI to present (comma-separated for several vehicles)")
        parser.add_argument("--codec", choices=["8", "8e"], default="8e")
        parser.add_argument("--interval", type=float, default=5.0, help="seconds between frames")
        parser.add_argument("--batch", type=int, default=1, help="records per frame")
        parser.add_argument("--speed", type=float, default=45.0, help="average speed km/h")
        parser.add_argument("--count", type=int, default=0, help="frames to send (0 = forever)")
        parser.add_argument("--no-fix-every", type=int, default=0, help="every Nth record has no GNSS fix")

    def handle(self, *args, **o):
        imeis = [i.strip() for i in o["imei"].split(",") if i.strip()]
        threads = [threading.Thread(target=self.run_one, args=(imei, n, o), daemon=True) for n, imei in enumerate(imeis)]
        for t in threads:
            t.start()
        try:
            while any(t.is_alive() for t in threads):
                time.sleep(0.5)
        except KeyboardInterrupt:
            self.stdout.write("Simulator stopped.")

    def run_one(self, imei, n, o):
        cid = codec.CODEC_8E if o["codec"] == "8e" else codec.CODEC_8
        state = SimState(seg=(n * 3) % len(DEFAULT_ROUTE))
        sent = 0
        rec_no = 0
        while o["count"] == 0 or sent < o["count"]:
            client = DeviceClient(o["host"], o["port"], imei)
            try:
                client.connect()
                r = client.handshake()
                if r != 1:
                    self.stderr.write(f"[{imei}] handshake rejected (0x{r:02X}) - is the IMEI registered and enabled?")
                    return
                self.stdout.write(f"[{imei}] connected to {o['host']}:{o['port']}")
                while o["count"] == 0 or sent < o["count"]:
                    recs = []
                    for _ in range(o["batch"]):
                        rec_no += 1
                        cyc = rec_no % 80                    # 60 drive, 10 idle, 10 parked
                        phase = 0 if cyc < 60 else (1 if cyc < 70 else 2)
                        speed = max(5, o["speed"] + random.uniform(-10, 10)) if phase == 0 else 0
                        lat, lon, ang = step_route(state, o["interval"] / o["batch"], speed)
                        valid = not (o["no_fix_every"] and rec_no % o["no_fix_every"] == 0)
                        recs.append(make_record(lat, lon, int(speed), ang, ignition=phase != 2,
                                                odometer_m=state.odometer_m, codec_id=cid, valid=valid))
                    ack = client.send_frame(codec.encode_frame(recs, cid))
                    sent += 1
                    self.stdout.write(f"[{imei}] frame {sent}: {len(recs)} record(s) -> ACK {ack}  "
                                      f"{recs[-1].latitude:.5f},{recs[-1].longitude:.5f} {recs[-1].speed} km/h")
                    time.sleep(o["interval"])
            except (OSError, ConnectionError) as exc:
                self.stderr.write(f"[{imei}] connection problem: {exc}; retrying in 5 s")
                time.sleep(5)
            finally:
                client.close()
