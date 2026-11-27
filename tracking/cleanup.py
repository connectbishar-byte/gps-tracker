"""
Removing demo / simulator / test data without touching real vehicles.

`delete_all_except(keep_ids)` wipes every device that is not in `keep_ids` together with everything that
hangs off it (records, parameter values, raw packets, connections, port-terminal events, saved places),
plus rejected/unknown-IMEI traffic that belongs to no kept device. Used by `manage.py clean_demo_data`.

`remove_selftest_data(imei)` is called after every connectivity self-test so the simulated FMC130 it uses
never shows up as a vehicle on the map, dashboard, reports or driver score sheet.

Deletes run as plain SQL DELETEs in dependency order (children first) inside one transaction, so large
history tables are not loaded into memory and SQL Server's 2100-parameter limit is never hit.
"""
from __future__ import annotations

from django.db import router, transaction
from django.db.models import Q

from .models import AVLRecord, Connection, Device, IOValue, PortEvent, RawPacket, SavedPlace, TestRun

UNKNOWN_SELFTEST_IMEI = "000000000000001"  # the "must be rejected" IMEI used by run_connectivity_suite


def _raw_delete(qs) -> int:
    return qs._raw_delete(router.db_for_write(qs.model)) or 0


def _plan(doomed_devices, doomed_connections, doomed_packets, doomed_events, doomed_places):
    """Querysets in safe delete order, keyed by a human label."""
    doomed_records = AVLRecord.objects.filter(Q(device__in=doomed_devices) | Q(packet__in=doomed_packets))
    return [
        ("port-terminal events", doomed_events),
        ("parameter values (IO)", IOValue.objects.filter(Q(record__in=doomed_records) | Q(device__in=doomed_devices))),
        ("GPS records", doomed_records),
        ("raw packets", doomed_packets),
        ("connections", doomed_connections),
        ("saved places", doomed_places),
        ("vehicles / devices", doomed_devices),
    ]


def _run(plan, dry_run: bool) -> dict[str, int]:
    counts = {label: qs.count() for label, qs in plan}
    if dry_run:
        return counts
    with transaction.atomic():
        plan = list(plan)
        label, events = plan.pop(0)
        counts[label] = _raw_delete(events)
        # rows that survive but still point at something we are about to delete get that link cleared
        doomed = dict(plan)
        PortEvent.objects.filter(packet__in=doomed["raw packets"]).update(packet=None)
        PortEvent.objects.filter(connection__in=doomed["connections"]).update(connection=None)
        RawPacket.objects.filter(connection__in=doomed["connections"]).exclude(
            pk__in=doomed["raw packets"]).update(connection=None)
        for label, qs in plan:
            counts[label] = _raw_delete(qs)
    return counts


def delete_all_except(keep_ids, *, include_phone_places: bool = False, include_test_runs: bool = True,
                      dry_run: bool = True) -> dict[str, int]:
    keep_ids = list(keep_ids)
    kept_imeis = list(Device.objects.filter(pk__in=keep_ids).values_list("imei", flat=True))
    devices = Device.objects.exclude(pk__in=keep_ids)
    connections = Connection.objects.exclude(device__in=keep_ids)
    kept_conn_ids = Connection.objects.filter(device__in=keep_ids).values("pk")
    packets = RawPacket.objects.exclude(device__in=keep_ids).exclude(connection__in=kept_conn_ids)
    events = PortEvent.objects.exclude(connection__in=kept_conn_ids).exclude(imei__in=kept_imeis)
    places = SavedPlace.objects.filter(device__in=devices)
    if include_phone_places:
        places = SavedPlace.objects.exclude(device__in=keep_ids)
    plan = _plan(devices, connections, packets, events, places)
    if include_test_runs:
        plan.insert(-1, ("connectivity self-test results", TestRun.objects.all()))
    return _run(plan, dry_run)


def remove_selftest_data(imei: str) -> dict[str, int]:
    """Delete the simulated self-test device and the traffic the self-test produced (keeps the TestRun)."""
    imeis = [imei, UNKNOWN_SELFTEST_IMEI]
    devices = Device.objects.filter(imei=imei, model="SIMULATOR")
    connections = Connection.objects.filter(Q(device__in=devices) | Q(device__isnull=True, claimed_imei__in=imeis))
    packets = RawPacket.objects.filter(Q(device__in=devices) | Q(connection__in=connections))
    events = PortEvent.objects.filter(Q(imei__in=imeis) | Q(connection__in=connections) | Q(packet__in=packets))
    places = SavedPlace.objects.filter(device__in=devices)
    return _run(_plan(devices, connections, packets, events, places), dry_run=False)
