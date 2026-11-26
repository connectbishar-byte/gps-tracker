"""
Teltonika AVL protocol: framing, CRC, Codec 8 / Codec 8 Extended decoding and encoding.

Pure Python, no Django imports, so it can be reused by the receiver, the simulator
and the standalone connectivity probe in tools/.

Reference: https://wiki.teltonika-gps.com/view/Codec

TCP session:
    device -> server : 2-byte big-endian IMEI length + ASCII IMEI
    server -> device : 0x01 accept | 0x00 reject
    device -> server : AVL frame  (00000000 | data length (4) | data | CRC (4))
    server -> device : 4-byte big-endian number of accepted records
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from typing import Iterable

CODEC_8 = 0x08
CODEC_8E = 0x8E
SUPPORTED_CODECS = {CODEC_8: "8", CODEC_8E: "8E"}

PREAMBLE = b"\x00\x00\x00\x00"
HEADER_LEN = 8          # preamble + data length
CRC_LEN = 4
MIN_DATA_LEN = 1 + 1 + 1  # codec + n1 + n2 with zero records (never valid, but bounds check)


class ProtocolError(Exception):
    """Frame is structurally invalid (bad preamble, length, CRC, codec, record counts...)."""


class NeedMoreData(Exception):
    """Buffer does not yet hold a complete frame."""


# ---------------------------------------------------------------------------
# CRC-16/IBM (a.k.a. CRC-16/ARC): poly 0xA001 reflected, init 0x0000
# ---------------------------------------------------------------------------
def crc16_ibm(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class IOElement:
    avl_id: int
    length: int           # value length in bytes
    raw: bytes            # raw big-endian bytes as received

    @property
    def unsigned(self) -> int:
        return int.from_bytes(self.raw, "big", signed=False)

    @property
    def signed(self) -> int:
        return int.from_bytes(self.raw, "big", signed=True)


@dataclass
class AVLRecord:
    timestamp_ms: int
    priority: int
    longitude: float
    latitude: float
    altitude: int
    angle: int
    satellites: int
    speed: int
    event_io_id: int
    total_io: int
    io: list[IOElement] = field(default_factory=list)
    raw: bytes = b""

    @property
    def has_valid_fix(self) -> bool:
        """Teltonika sends 0/0 with 0 satellites (or last known with 0 sats) when there is no fix."""
        if self.satellites == 0:
            return False
        if self.latitude == 0 and self.longitude == 0:
            return False
        return -90 <= self.latitude <= 90 and -180 <= self.longitude <= 180

    def fingerprint(self, imei: str) -> str:
        """Canonical identity of a record, used for idempotent replay handling."""
        h = hashlib.sha256()
        h.update(imei.encode())
        h.update(self.raw if self.raw else repr(self).encode())
        return h.hexdigest()


@dataclass
class AVLPacket:
    codec: int
    records: list[AVLRecord]
    crc: int
    data_length: int
    frame: bytes

    @property
    def codec_name(self) -> str:
        return SUPPORTED_CODECS.get(self.codec, hex(self.codec))


# ---------------------------------------------------------------------------
# Handshake helpers
# ---------------------------------------------------------------------------
def encode_imei(imei: str) -> bytes:
    raw = imei.encode("ascii")
    return struct.pack(">H", len(raw)) + raw


def try_parse_imei(buf: bytes) -> tuple[str, int]:
    """Return (imei, bytes consumed). Raise NeedMoreData / ProtocolError."""
    if len(buf) < 2:
        raise NeedMoreData()
    (length,) = struct.unpack(">H", buf[:2])
    if length == 0 or length > 32:
        raise ProtocolError(f"invalid IMEI length {length}")
    if len(buf) < 2 + length:
        raise NeedMoreData()
    raw = buf[2:2 + length]
    try:
        imei = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProtocolError("IMEI is not ASCII") from exc
    if not imei.isdigit():
        raise ProtocolError(f"IMEI contains non-digits: {imei!r}")
    return imei, 2 + length


def encode_ack(count: int) -> bytes:
    return struct.pack(">I", count)


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------
def extract_frame(buf: bytes, max_frame: int = 65536) -> tuple[bytes, int]:
    """
    Find one complete AVL frame at the start of buf.
    Returns (frame_bytes, consumed). Raises NeedMoreData if incomplete,
    ProtocolError if the header is invalid (caller should drop the connection).
    """
    if len(buf) < HEADER_LEN:
        raise NeedMoreData()
    if buf[:4] != PREAMBLE:
        raise ProtocolError(f"bad preamble {buf[:4].hex()}")
    (data_len,) = struct.unpack(">I", buf[4:8])
    total = HEADER_LEN + data_len + CRC_LEN
    if data_len < MIN_DATA_LEN or total > max_frame:
        raise ProtocolError(f"data length {data_len} out of bounds (max frame {max_frame})")
    if len(buf) < total:
        raise NeedMoreData()
    return bytes(buf[:total]), total


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------
class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise ProtocolError(f"truncated data at offset {self.pos} (need {n} bytes)")
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack(">H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack(">I", self.take(4))[0]

    def i32(self) -> int:
        return struct.unpack(">i", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack(">Q", self.take(8))[0]


def decode_frame(frame: bytes) -> AVLPacket:
    """Decode a complete frame produced by extract_frame. Validates CRC and counts."""
    if len(frame) < HEADER_LEN + CRC_LEN:
        raise ProtocolError("frame too short")
    if frame[:4] != PREAMBLE:
        raise ProtocolError("bad preamble")
    (data_len,) = struct.unpack(">I", frame[4:8])
    if len(frame) != HEADER_LEN + data_len + CRC_LEN:
        raise ProtocolError("frame length does not match header")
    data = frame[8:8 + data_len]
    (crc_field,) = struct.unpack(">I", frame[-4:])
    calc = crc16_ibm(data)
    if (crc_field & 0xFFFF) != calc:
        raise ProtocolError(f"CRC mismatch: frame {crc_field & 0xFFFF:04X}, calculated {calc:04X}")

    r = _Reader(data)
    codec = r.u8()
    if codec not in SUPPORTED_CODECS:
        raise ProtocolError(f"unsupported codec 0x{codec:02X}")
    n1 = r.u8()
    if n1 == 0:
        raise ProtocolError("record count is zero")
    extended = codec == CODEC_8E
    records = []
    for _ in range(n1):
        start = r.pos
        rec = _decode_record(r, extended)
        rec.raw = data[start:r.pos]
        records.append(rec)
    n2 = r.u8()
    if n1 != n2:
        raise ProtocolError(f"record count mismatch: {n1} vs {n2}")
    if r.pos != len(data):
        raise ProtocolError(f"{len(data) - r.pos} trailing bytes after records")
    return AVLPacket(codec=codec, records=records, crc=crc_field, data_length=data_len, frame=frame)


def _decode_record(r: _Reader, extended: bool) -> AVLRecord:
    ts = r.u64()
    priority = r.u8()
    lon = r.i32() / 1e7
    lat = r.i32() / 1e7
    altitude = struct.unpack(">h", r.take(2))[0]
    angle = r.u16()
    sats = r.u8()
    speed = r.u16()

    read_id = r.u16 if extended else r.u8
    read_cnt = r.u16 if extended else r.u8

    event_id = read_id()
    total_io = read_cnt()
    io: list[IOElement] = []
    for size in (1, 2, 4, 8):
        n = read_cnt()
        for _ in range(n):
            avl_id = read_id()
            io.append(IOElement(avl_id, size, r.take(size)))
    if extended:
        nx = r.u16()
        for _ in range(nx):
            avl_id = r.u16()
            length = r.u16()
            io.append(IOElement(avl_id, length, r.take(length)))
    if len(io) != total_io:
        raise ProtocolError(f"I/O count mismatch: header {total_io}, parsed {len(io)}")
    return AVLRecord(ts, priority, lon, lat, altitude, angle, sats, speed, event_id, total_io, io)


# ---------------------------------------------------------------------------
# Encoding (simulator / tests / probe)
# ---------------------------------------------------------------------------
def encode_record(rec: AVLRecord, codec: int = CODEC_8E) -> bytes:
    extended = codec == CODEC_8E
    wid = ">H" if extended else ">B"
    out = bytearray()
    out += struct.pack(">QB", rec.timestamp_ms, rec.priority)
    out += struct.pack(">ii", round(rec.longitude * 1e7), round(rec.latitude * 1e7))
    out += struct.pack(">hHBH", rec.altitude, rec.angle, rec.satellites, rec.speed)
    out += struct.pack(wid, rec.event_io_id)
    out += struct.pack(wid, len(rec.io))
    by_size: dict[int, list[IOElement]] = {1: [], 2: [], 4: [], 8: []}
    variable: list[IOElement] = []
    for e in rec.io:
        if e.length in by_size and len(e.raw) == e.length:
            by_size[e.length].append(e)
        elif extended:
            variable.append(e)
        else:
            raise ValueError(f"Codec 8 cannot carry {e.length}-byte element {e.avl_id}")
    for size in (1, 2, 4, 8):
        out += struct.pack(wid, len(by_size[size]))
        for e in by_size[size]:
            out += struct.pack(wid, e.avl_id) + e.raw
    if extended:
        out += struct.pack(">H", len(variable))
        for e in variable:
            out += struct.pack(">HH", e.avl_id, len(e.raw)) + e.raw
    return bytes(out)


def encode_frame(records: Iterable[AVLRecord], codec: int = CODEC_8E, corrupt_crc: bool = False) -> bytes:
    records = list(records)
    data = bytes([codec, len(records)]) + b"".join(encode_record(r, codec) for r in records) + bytes([len(records)])
    crc = crc16_ibm(data)
    if corrupt_crc:
        crc ^= 0xFFFF
    return PREAMBLE + struct.pack(">I", len(data)) + data + struct.pack(">I", crc)


def io_int(avl_id: int, value: int, size: int, signed: bool = False) -> IOElement:
    return IOElement(avl_id, size, value.to_bytes(size, "big", signed=signed))


# Official sample frames from the Teltonika Codec wiki page (used by tests and the probe).
OFFICIAL_SAMPLES = {
    "codec8_example1": "000000000000003608010000016B40D8EA30010000000000000000000000000000000105021503010101425E0F01F10000601A014E0000000000000000010000C7CF",
    "codec8_example2": "000000000000002808010000016B40D9AD80010000000000000000000000000000000103021503010101425E100000010000F22A",
    "codec8_example3": "000000000000004308020000016B40D57B480100000000000000000000000000000001010101000000000000016B40D5C198010000000000000000000000000000000101010101000000020000252C",
    "codec8e_example": "000000000000004A8E010000016B412CEE000100000000000000000000000000000000010005000100010100010011001D00010010015E2C880002000B000000003544C87A000E000000001DD7E06A00000100002994",
}
