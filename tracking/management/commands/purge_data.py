"""
Delete stored data older than each vehicle's retention period (Device.retention_days, default 60).

For each vehicle: parameters (tracking_iovalue), records (tracking_avlrecord), raw packets and
connections older than its retention are deleted in small batches, so SQL Server's log and locks stay
small and the receiver keeps writing while it runs. Traffic that belongs to no vehicle (rejected or
unknown IMEIs) uses FLEET_DEFAULT_RETENTION_DAYS. Port-terminal events have their own, shorter
retention (FLEET_PORT_LOG_RETENTION_HOURS) and are pruned too.

    python manage.py purge_data                 # run once (the daily systemd timer does this)
    python manage.py purge_data --dry-run       # only count what would be deleted
    python manage.py purge_data --device 3
    python manage.py purge_data --loop-hours 24 # keep running, purge every 24 h (Docker 'maintenance' service)
"""
import time
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand
from django.utils import timezone

from tracking import ingest
from tracking.models import AlertEvent, AVLRecord, Connection, Device, GeofenceEvent, IOValue, RawPacket, WhatsAppMessage

LAST_RUN_KEY = "purge:last"


def _batched_delete(qs, batch: int, dry: bool) -> int:
    if dry:
        return qs.order_by().count()
    total = 0
    while True:
        ids = list(qs.order_by().values_list("pk", flat=True)[:batch])
        if not ids:
            return total
        qs.model.objects.filter(pk__in=ids).delete()
        total += len(ids)


class Command(BaseCommand):
    help = "Delete data older than each vehicle's retention period."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--device", type=int, help="Only this device id")
        parser.add_argument("--batch", type=int, default=2000)
        parser.add_argument("--loop-hours", type=float, default=0, help="Repeat every N hours (0 = run once)")

    def handle(self, *args, **o):
        while True:
            self.run_once(o)
            if not o["loop_hours"]:
                return
            time.sleep(o["loop_hours"] * 3600)

    def run_once(self, o):
        now = timezone.now()
        dry, batch = o["dry_run"], o["batch"]
        verb = "would delete" if dry else "deleted"
        devices = Device.objects.all()
        if o["device"]:
            devices = devices.filter(pk=o["device"])
        summary = {"io": 0, "records": 0, "packets": 0, "connections": 0, "geofence": 0}
        for d in devices:
            cutoff = now - timedelta(days=d.retention_days)
            n = {
                "io": _batched_delete(IOValue.objects.filter(device=d, timestamp_utc__lt=cutoff), batch, dry),
                "records": _batched_delete(AVLRecord.objects.filter(device=d, timestamp_utc__lt=cutoff), batch, dry),
            }
            # a packet goes once none of its records are kept (records can arrive late with old timestamps)
            n["packets"] = _batched_delete(RawPacket.objects.filter(device=d, received_utc__lt=cutoff, records__isnull=True),
                                           batch, dry)
            n["connections"] = _batched_delete(Connection.objects.filter(device=d, opened_utc__lt=cutoff,
                                                                         closed_utc__isnull=False), batch, dry)
            n["geofence"] = _batched_delete(GeofenceEvent.objects.filter(device=d, timestamp_utc__lt=cutoff), batch, dry)
            n["geofence"] += _batched_delete(AlertEvent.objects.filter(device=d, timestamp_utc__lt=cutoff), batch, dry)
            for k, v in n.items():
                summary[k] += v
            if any(n.values()):
                self.stdout.write(f"{d.name}: keep {d.retention_days} days (before {cutoff:%Y-%m-%d %H:%M} UTC) - {verb} "
                                  f"{n['records']} records, {n['io']} parameter values, {n['packets']} raw packets, "
                                  f"{n['connections']} connections, {n['geofence']} geofence crossings / alerts")
        if not o["device"]:
            cutoff = now - timedelta(days=settings.FLEET["DEFAULT_RETENTION_DAYS"])
            orphan_p = _batched_delete(RawPacket.objects.filter(device__isnull=True, received_utc__lt=cutoff), batch, dry)
            orphan_c = _batched_delete(Connection.objects.filter(device__isnull=True, opened_utc__lt=cutoff,
                                                                 closed_utc__isnull=False), batch, dry)
            summary["packets"] += orphan_p
            summary["connections"] += orphan_c
            if orphan_p or orphan_c:
                self.stdout.write(f"No vehicle (rejected / unknown IMEIs): {verb} {orphan_p} raw packets, {orphan_c} connections")
            _batched_delete(WhatsAppMessage.objects.filter(created_utc__lt=cutoff), batch, dry)
            if not dry:
                summary["port_events"] = ingest.prune_events(settings.FLEET["PORT_LOG_RETENTION_HOURS"])
        line = (f"Purge {'(dry run) ' if dry else ''}done: {verb} {summary['records']} records, {summary['io']} parameter values, "
                f"{summary['packets']} raw packets, {summary['connections']} connections")
        self.stdout.write(self.style.SUCCESS(line))
        if not dry and not o["device"]:
            cache.set(LAST_RUN_KEY, {"at": now.isoformat(), "summary": summary}, None)
