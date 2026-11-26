#!/usr/bin/env python3
"""
Standalone FMC130 / Teltonika TCP connectivity probe.

Runs from ANY machine with Python 3.9+ (no Django, no pip install). Use it to prove that the
receiver is reachable from outside - e.g. from a phone hotspot against your router's public IP,
a TCP tunnel, or gps.melticetech.com on Hostinger.

    python tools/fmc_probe.py gps.melticetech.com 5027 --imei 356307042441013
    python tools/fmc_probe.py 127.0.0.1 5027 --quick

The IMEI must be registered and enabled on the target server (Devices page), otherwise the
"Registered IMEI accepted" step fails - which is itself a useful allow-list check.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from tracking.simulator import run_connectivity_suite  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host")
    ap.add_argument("port", type=int, nargs="?", default=5027)
    ap.add_argument("--imei", default="356307042441013")
    ap.add_argument("--quick", action="store_true", help="handshake + one live point only")
    ap.add_argument("--timeout", type=float, default=10)
    a = ap.parse_args()
    print(f"Probing {a.host}:{a.port} as IMEI {a.imei[:4]}…{a.imei[-4:]}\n")
    steps = run_connectivity_suite(a.host, a.port, a.imei, full=not a.quick, timeout=a.timeout)
    for s in steps:
        print(f"{'PASS' if s['ok'] else 'FAIL'}  {s['name']:<42} {s['ms']:>5} ms  {s['detail']}")
    ok = all(s["ok"] for s in steps)
    print(f"\n{sum(s['ok'] for s in steps)}/{len(steps)} steps passed - {'OK' if ok else 'PROBLEMS FOUND'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
