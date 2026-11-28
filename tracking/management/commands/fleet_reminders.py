"""
Send the WhatsApp reminder for fleet documents (registration, insurance, licences, permits...) that are expired or
inside their reminder window. The TCP receiver already does this hourly in the daytime; this command is for cron
or a manual run.

    python manage.py fleet_reminders            # send now
    python manage.py fleet_reminders --dry-run  # only print the message
"""
from django.core.management.base import BaseCommand

from tracking import fleet


class Command(BaseCommand):
    help = "Send WhatsApp reminders for expiring fleet documents."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        res = fleet.send_reminders(dry_run=opts["dry_run"])
        self.stdout.write(f"{res['count']} document(s). {res['detail']}")
