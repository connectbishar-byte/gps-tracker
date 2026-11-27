from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from tracking import cleanup
from tracking.models import Device, SavedPlace


class Command(BaseCommand):
    help = ("Delete demo / simulator / test vehicles and all their data, keeping only the real vehicle(s) "
            "(by default anything named Jeep or Wrangler). Shows what would be deleted unless --yes is given.")

    def add_arguments(self, parser):
        parser.add_argument("--keep", default="jeep,wrangler",
                            help="comma-separated words; a device is kept when its name, plate, model, group or "
                                 "notes contains any of them (default: jeep,wrangler)")
        parser.add_argument("--keep-imei", action="append", default=[], help="also keep this IMEI (repeatable)")
        parser.add_argument("--include-phone-places", action="store_true",
                            help="also delete saved places not linked to a kept vehicle (phone drives, pins, imports)")
        parser.add_argument("--keep-test-runs", action="store_true", help="keep the connectivity self-test history")
        parser.add_argument("--yes", action="store_true", help="really delete (without it this is a dry run)")

    def handle(self, *args, **o):
        q = Q(imei__in=o["keep_imei"])
        for word in filter(None, (w.strip() for w in o["keep"].split(","))):
            for field in ("name", "plate", "model", "group", "notes"):
                q |= Q(**{f"{field}__icontains": word})
        keep = list(Device.objects.filter(q).order_by("name"))
        if not keep:
            raise CommandError("No vehicle matches --keep / --keep-imei, so nothing would be left. Nothing deleted. "
                               "Check the vehicle's name on the Devices page or pass --keep-imei <IMEI>.")

        self.stdout.write(self.style.SUCCESS("Keeping:"))
        for d in keep:
            self.stdout.write(f"  {d.name}  plate={d.plate or '-'}  IMEI {d.masked_imei}  records={d.record_count}")
        doomed = Device.objects.exclude(pk__in=[d.pk for d in keep]).order_by("name")
        self.stdout.write(self.style.WARNING(f"\nDeleting {doomed.count()} vehicle(s):"))
        for d in doomed:
            self.stdout.write(f"  {d.name}  plate={d.plate or '-'}  IMEI {d.masked_imei}  model={d.model}  "
                              f"group={d.group or '-'}  records={d.record_count}")
        phone = SavedPlace.objects.filter(device__isnull=True).count()

        counts = cleanup.delete_all_except([d.pk for d in keep], include_phone_places=o["include_phone_places"],
                                           include_test_runs=not o["keep_test_runs"], dry_run=not o["yes"])
        self.stdout.write("")
        for label, n in counts.items():
            self.stdout.write(f"  {n:>10,}  {label}")
        if phone and not o["include_phone_places"]:
            self.stdout.write(f"\n  ({phone} saved place(s) from the phone / imports are kept; add "
                              f"--include-phone-places to delete them too)")
        if o["yes"]:
            self.stdout.write(self.style.SUCCESS("\nDone. Only the kept vehicle(s) and their data remain."))
        else:
            self.stdout.write(self.style.NOTICE("\nDry run: nothing was deleted. Run again with --yes to delete."))
