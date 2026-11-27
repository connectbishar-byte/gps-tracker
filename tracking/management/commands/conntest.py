import json

from django.conf import settings
from django.core.management.base import BaseCommand

from tracking import cleanup
from tracking.models import Device, TestRun
from tracking.simulator import run_connectivity_suite


class Command(BaseCommand):
    help = "Run the TCP connectivity / protocol acceptance suite against a receiver (local or Hostinger)."

    def add_arguments(self, parser):
        parser.add_argument("--host", default=settings.FLEET["SELFTEST_HOST"])
        parser.add_argument("--port", type=int, default=settings.FLEET["SELFTEST_PORT"])
        parser.add_argument("--imei", default=settings.FLEET["SELFTEST_IMEI"])
        parser.add_argument("--quick", action="store_true", help="handshake + one live point only")
        parser.add_argument("--no-register", action="store_true", help="do not auto-register the test IMEI locally")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--keep-data", action="store_true",
                            help="keep the simulated device and its traffic afterwards (default: removed)")

    def handle(self, *args, **o):
        if not o["no_register"]:
            Device.objects.get_or_create(imei=o["imei"], defaults={"name": "Self-test simulator", "model": "SIMULATOR", "group": "Test"})
        steps = run_connectivity_suite(o["host"], o["port"], o["imei"], full=not o["quick"])
        if not o["no_register"] and not o["keep_data"]:
            cleanup.remove_selftest_data(o["imei"])
        ok = all(s["ok"] for s in steps)
        summary = f"{sum(s['ok'] for s in steps)}/{len(steps)} steps passed"
        TestRun.objects.create(target=f"{o['host']}:{o['port']}", passed=ok, steps=steps, summary=summary)
        if o["json"]:
            self.stdout.write(json.dumps({"passed": ok, "steps": steps}, indent=2))
            return
        for s in steps:
            mark = self.style.SUCCESS("PASS") if s["ok"] else self.style.ERROR("FAIL")
            self.stdout.write(f"{mark}  {s['name']:<42} {s['ms']:>5} ms  {s['detail']}")
        self.stdout.write((self.style.SUCCESS if ok else self.style.ERROR)(f"\n{summary} against {o['host']}:{o['port']}"))
        if not ok:
            raise SystemExit(1)
