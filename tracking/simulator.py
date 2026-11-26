"""
FMC130 device simulator and TCP connectivity test suite.

No Django imports: usable from manage.py commands, the web "Connectivity" page and the
standalone tools/fmc_probe.py (which you can run from any PC against local or Hostinger).
"""
from __future__ import annotations

import math
import random
import socket
import time
from dataclasses import dataclass

from . import codec
from .codec import AVLRecord, CODEC_8, CODEC_8E, io_int

# A loop around Al Shamkha / Abu Dhabi (lat, lon)
DEFAULT_ROUTE = [
    (24.39020, 54.70510), (24.39310, 54.70960), (24.39640, 54.71420), (24.40020, 54.71890),
    (24.40390, 54.72300), (24.40150, 54.72780), (24.39750, 54.73150), (24.39280, 54.72870),
    (24.38900, 54.72420), (24.38610, 54.71880), (24.38510, 54.71250), (24.38720, 54.70800),
]


def _bearing(a, b) -> int:
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlon = math.radians(b[1] - a[1])
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return int((math.degrees(math.atan2(x, y)) + 360) % 360)


def _dist_m(a, b) -> float:
    r = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


@dataclass
class SimState:
    seg: int = 0
    frac: float = 0.0
    odometer_m: int = 125_000
    ignition: bool = True


def make_record(lat: float, lon: float, speed: int, angle: int, ts_ms: int | None = None, *,
                ignition: bool = True, sats: int = 11, odometer_m: int = 0, event_id: int = 0,
                codec_id: int = CODEC_8E, valid: bool = True) -> AVLRecord:
    moving = speed > 3
    io = [
        io_int(239, int(ignition), 1),
        io_int(240, int(moving), 1),
        io_int(21, random.randint(3, 5), 1),
        io_int(69, 1 if valid else 2, 1),
        io_int(200, 0, 1),
        io_int(181, random.randint(8, 15), 2),
        io_int(182, random.randint(6, 12), 2),
        io_int(66, random.randint(12100, 12600) if ignition else random.randint(11800, 12100), 2),
        io_int(67, random.randint(4000, 4150), 2),
        io_int(68, random.randint(0, 40), 2),
        io_int(24, speed, 2),
        io_int(16, odometer_m, 4),
    ]
    if codec_id == CODEC_8E:
        io.append(io_int(11, 8997103118812345678 % (2**63), 8))
    if not valid:
        lat = lon = 0.0
        sats = 0
    return AVLRecord(
        timestamp_ms=ts_ms if ts_ms is not None else int(time.time() * 1000),
        priority=1 if event_id else 0, longitude=lon, latitude=lat, altitude=random.randint(8, 22),
        angle=angle, satellites=sats, speed=speed, event_io_id=event_id, total_io=len(io), io=io,
    )


def step_route(state: SimState, seconds: float, speed_kmh: float, route=DEFAULT_ROUTE):
    """Advance along route; returns (lat, lon, angle)."""
    remaining = speed_kmh / 3.6 * seconds
    while remaining > 0:
        a, b = route[state.seg], route[(state.seg + 1) % len(route)]
        seg_len = max(_dist_m(a, b), 1)
        left = seg_len * (1 - state.frac)
        if remaining < left:
            state.frac += remaining / seg_len
            remaining = 0
        else:
            remaining -= left
            state.seg = (state.seg + 1) % len(route)
            state.frac = 0.0
    a, b = route[state.seg], route[(state.seg + 1) % len(route)]
    lat = a[0] + (b[0] - a[0]) * state.frac
    lon = a[1] + (b[1] - a[1]) * state.frac
    state.odometer_m += int(speed_kmh / 3.6 * seconds)
    return lat, lon, _bearing(a, b)


# ---------------------------------------------------------------------------
# TCP client
# ---------------------------------------------------------------------------
class DeviceClient:
    def __init__(self, host: str, port: int, imei: str, timeout: float = 10.0):
        self.host, self.port, self.imei, self.timeout = host, port, imei, timeout
        self.sock: socket.socket | None = None

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _recv_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("server closed connection")
            data += chunk
        return data

    def handshake(self) -> int:
        self.sock.sendall(codec.encode_imei(self.imei))
        return self._recv_exact(1)[0]

    def send_frame(self, frame: bytes, fragment: int = 0) -> int:
        if fragment:
            for i in range(0, len(frame), fragment):
                self.sock.sendall(frame[i:i + fragment])
                time.sleep(0.01)
        else:
            self.sock.sendall(frame)
        return int.from_bytes(self._recv_exact(4), "big")

    def send_raw(self, data: bytes):
        self.sock.sendall(data)

    def read_ack(self) -> int:
        return int.from_bytes(self._recv_exact(4), "big")


# ---------------------------------------------------------------------------
# Connectivity / acceptance test suite
# ---------------------------------------------------------------------------
def run_connectivity_suite(host: str, port: int, imei: str, *, unknown_imei: str = "000000000000001",
                           full: bool = True, timeout: float = 10.0) -> list[dict]:
    """Runs the protocol acceptance tests from the scope against a live receiver."""
    steps: list[dict] = []

    def step(name, fn):
        t0 = time.perf_counter()
        try:
            ok, detail = fn()
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        steps.append({"name": name, "ok": bool(ok), "detail": detail, "ms": round((time.perf_counter() - t0) * 1000)})
        return ok

    def dns():
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return True, "resolved to " + ", ".join(sorted({i[4][0] for i in infos}))

    def tcp():
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True, f"TCP {host}:{port} open"

    if not step("DNS resolution", dns):
        return steps
    if not step("TCP port reachable", tcp):
        return steps

    def unknown():
        c = DeviceClient(host, port, unknown_imei, timeout)
        c.connect()
        try:
            r = c.handshake()
            return r == 0, f"server replied 0x{r:02X} (expect 0x00)"
        finally:
            c.close()

    step("Unknown IMEI rejected", unknown)

    client = DeviceClient(host, port, imei, timeout)

    def accept():
        client.connect()
        r = client.handshake()
        return r == 1, f"server replied 0x{r:02X} (expect 0x01)"

    if not step("Registered IMEI accepted", accept):
        client.close()
        return steps

    def official(name, expect):
        def fn():
            ack = client.send_frame(bytes.fromhex(codec.OFFICIAL_SAMPLES[name]))
            return ack == expect, f"ACK {ack} (expect {expect})"
        return fn

    def live_point(codec_id):
        def fn():
            lat, lon = DEFAULT_ROUTE[0]
            rec = make_record(lat, lon, 0, 0, codec_id=codec_id, ignition=False)
            ack = client.send_frame(codec.encode_frame([rec], codec_id))
            return ack == 1, f"ACK {ack}; position {lat:.5f},{lon:.5f} at {time.strftime('%H:%M:%S', time.gmtime())} UTC"
        return fn

    step("Live position (Codec 8E, current time)", live_point(CODEC_8E))
    if full:
        step("Official Codec 8 sample #1", official("codec8_example1", 1))
        step("Official Codec 8 sample #3 (2 records)", official("codec8_example3", 2))
        step("Official Codec 8E sample", official("codec8e_example", 1))

        def fragmented():
            rec = make_record(*DEFAULT_ROUTE[1], 35, 45, codec_id=CODEC_8)
            ack = client.send_frame(codec.encode_frame([rec], CODEC_8), fragment=7)
            return ack == 1, f"frame sent in 7-byte chunks, ACK {ack}"
        step("Fragmented TCP frame", fragmented)

        def coalesced():
            now = int(time.time() * 1000)
            f1 = codec.encode_frame([make_record(*DEFAULT_ROUTE[2], 40, 50, now + 1)])
            f2 = codec.encode_frame([make_record(*DEFAULT_ROUTE[3], 42, 52, now + 2),
                                     make_record(*DEFAULT_ROUTE[4], 44, 55, now + 3)])
            client.send_raw(f1 + f2)
            a1, a2 = client.read_ack(), client.read_ack()
            return (a1, a2) == (1, 2), f"two frames in one write, ACKs {a1},{a2} (expect 1,2)"
        step("Coalesced TCP frames", coalesced)

        def bad_crc():
            frame = codec.encode_frame([make_record(*DEFAULT_ROUTE[5], 30, 60)], corrupt_crc=True)
            ack = client.send_frame(frame)
            return ack == 0, f"ACK {ack} (expect 0, record must not be stored)"
        step("Bad CRC rejected", bad_crc)

        def replay():
            ack = client.send_frame(bytes.fromhex(codec.OFFICIAL_SAMPLES["codec8_example1"]))
            return ack == 1, f"ACK {ack}; server should store arrival but no duplicate record"
        step("Replay is idempotent (ACK)", replay)

    client.close()

    if full:
        def truncated():
            c = DeviceClient(host, port, imei, 3)
            c.connect()
            try:
                c.handshake()
                frame = bytes.fromhex(codec.OFFICIAL_SAMPLES["codec8_example2"])
                c.send_raw(frame[:-10])
                try:
                    data = c.sock.recv(4)
                except socket.timeout:
                    return True, "no ACK for incomplete frame (waited 3 s)"
                return data == b"", f"unexpected reply {data.hex()}" if data else "connection closed, no ACK"
            finally:
                c.close()
        step("Truncated frame not acknowledged", truncated)
    return steps
