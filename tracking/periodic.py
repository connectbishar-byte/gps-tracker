"""
Background checks that run inside the TCP receiver (every ~10 minutes, from its heartbeat loop), so they work on a
server with no cron or systemd timers. Each task runs at most once per its interval and never raises.

* fleet document expiry reminders: hourly between 08:00 and 20:00 local time (each document at most weekly)
* "No data (offline)" alert rules: every minute
* incident auto-escalation (not acknowledged in time): every minute

The receiver calls run() every heartbeat (5 s); each task throttles itself.
"""
from __future__ import annotations

import logging
import time

from django.utils import timezone

log = logging.getLogger("fleet.periodic")
_last: dict[str, float] = {}


def _due(name: str, every_s: float) -> bool:
    now = time.monotonic()
    if now - _last.get(name, -1e12) < every_s:
        return False
    _last[name] = now
    return True


def _reminders():
    from . import fleet
    if not 8 <= timezone.localtime().hour < 20:
        _last.pop("reminders", None)  # try again at 08:00
        return
    res = fleet.send_reminders()
    if res["count"]:
        log.info("document reminders: %s", res)


def _offline():
    from . import rules
    rules.check_offline()


def _escalate():
    from . import incidents
    n = incidents.auto_escalate()
    if n:
        log.info("auto-escalated %d incident(s)", n)


TASKS = [("reminders", 3600, _reminders), ("offline", 60, _offline), ("escalate", 60, _escalate)]


def run() -> None:
    for name, every, fn in TASKS:
        try:
            if _due(name, every):
                fn()
        except Exception:
            log.exception("periodic task %s failed", name)
