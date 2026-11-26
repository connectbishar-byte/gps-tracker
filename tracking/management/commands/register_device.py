from django.core.management.base import BaseCommand, CommandError

from tracking.models import Device


class Command(BaseCommand):
    help = "Register (or re-enable) a device IMEI on the receiver allow-list."

    def add_arguments(self, parser):
        parser.add_argument("imei")
        parser.add_argument("name")
        parser.add_argument("--model", default="FMC130")
        parser.add_argument("--group", default="Default")
        parser.add_argument("--plate", default="")
        parser.add_argument("--firmware", default="")
        parser.add_argument("--disable", action="store_true")

    def handle(self, *args, **o):
        imei = o["imei"].strip()
        if not imei.isdigit() or not 14 <= len(imei) <= 17:
            raise CommandError("IMEI must be 14-17 digits")
        d, created = Device.objects.update_or_create(imei=imei, defaults=dict(
            name=o["name"], model=o["model"], group=o["group"], plate=o["plate"], enabled=not o["disable"],
            **({"firmware": o["firmware"]} if o["firmware"] else {})))
        self.stdout.write(self.style.SUCCESS(f"{'Registered' if created else 'Updated'} {d.name} ({d.masked_imei}), enabled={d.enabled}"))
