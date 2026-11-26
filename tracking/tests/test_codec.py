from django.test import SimpleTestCase

from tracking import codec
from tracking.codec import OFFICIAL_SAMPLES, ProtocolError, NeedMoreData
from tracking.simulator import make_record


class OfficialSamples(SimpleTestCase):
    def test_codec8_example1(self):
        p = codec.decode_frame(bytes.fromhex(OFFICIAL_SAMPLES["codec8_example1"]))
        self.assertEqual(p.codec, 0x08)
        self.assertEqual(len(p.records), 1)
        r = p.records[0]
        self.assertEqual(r.timestamp_ms, 0x16B40D8EA30)
        self.assertEqual(r.priority, 1)
        io = {e.avl_id: e.unsigned for e in r.io}
        self.assertEqual(io, {21: 3, 1: 1, 66: 0x5E0F, 241: 0x601A, 78: 0})
        self.assertEqual(r.event_io_id, 1)

    def test_codec8_example3_two_records(self):
        p = codec.decode_frame(bytes.fromhex(OFFICIAL_SAMPLES["codec8_example3"]))
        self.assertEqual(len(p.records), 2)

    def test_codec8e_example(self):
        p = codec.decode_frame(bytes.fromhex(OFFICIAL_SAMPLES["codec8e_example"]))
        self.assertEqual(p.codec, 0x8E)
        io = {e.avl_id: e.unsigned for e in p.records[0].io}
        self.assertEqual(io[16], 0x015E2C88)
        self.assertEqual(io[11], 0x3544C87A)
        self.assertEqual(p.records[0].event_io_id, 1)

    def test_no_fix_detected(self):
        p = codec.decode_frame(bytes.fromhex(OFFICIAL_SAMPLES["codec8_example1"]))
        self.assertFalse(p.records[0].has_valid_fix)


class Errors(SimpleTestCase):
    def test_bad_crc(self):
        f = bytearray.fromhex(OFFICIAL_SAMPLES["codec8_example2"])
        f[-1] ^= 0xFF
        with self.assertRaisesRegex(ProtocolError, "CRC"):
            codec.decode_frame(bytes(f))

    def test_truncated_needs_more(self):
        f = bytes.fromhex(OFFICIAL_SAMPLES["codec8_example2"])
        with self.assertRaises(NeedMoreData):
            codec.extract_frame(f[:-3])

    def test_bad_preamble(self):
        f = b"\x01" + bytes.fromhex(OFFICIAL_SAMPLES["codec8_example2"])[1:]
        with self.assertRaises(ProtocolError):
            codec.extract_frame(f)

    def test_oversized_length(self):
        with self.assertRaises(ProtocolError):
            codec.extract_frame(b"\x00\x00\x00\x00\x7f\xff\xff\xff" + b"\x00" * 10, max_frame=1024)

    def test_count_mismatch(self):
        rec = make_record(24.4, 54.7, 10, 90)
        data = bytes([0x8E, 1]) + codec.encode_record(rec) + bytes([2])
        frame = codec.PREAMBLE + len(data).to_bytes(4, "big") + data + codec.crc16_ibm(data).to_bytes(4, "big")
        with self.assertRaisesRegex(ProtocolError, "mismatch"):
            codec.decode_frame(frame)

    def test_unsupported_codec(self):
        data = bytes([0x0C, 1, 0, 1])
        frame = codec.PREAMBLE + len(data).to_bytes(4, "big") + data + codec.crc16_ibm(data).to_bytes(4, "big")
        with self.assertRaisesRegex(ProtocolError, "unsupported codec"):
            codec.decode_frame(frame)


class RoundTrip(SimpleTestCase):
    def test_encode_decode_8e_with_variable_io(self):
        rec = make_record(24.3912345, 54.7123456, 62, 271, 1_700_000_000_000)
        rec.io.append(codec.IOElement(385, 5, b"\x01\x02\x03\x04\x05"))  # NX element
        p = codec.decode_frame(codec.encode_frame([rec, rec], codec.CODEC_8E))
        r = p.records[0]
        self.assertAlmostEqual(r.latitude, 24.3912345, places=7)
        self.assertAlmostEqual(r.longitude, 54.7123456, places=7)
        self.assertEqual((r.speed, r.angle, r.timestamp_ms), (62, 271, 1_700_000_000_000))
        self.assertEqual([e for e in r.io if e.avl_id == 385][0].raw, b"\x01\x02\x03\x04\x05")

    def test_codec8_negative_coordinates(self):
        rec = make_record(-33.9, -70.6, 5, 10, codec_id=codec.CODEC_8)
        r = codec.decode_frame(codec.encode_frame([rec], codec.CODEC_8)).records[0]
        self.assertAlmostEqual(r.latitude, -33.9, places=6)
        self.assertAlmostEqual(r.longitude, -70.6, places=6)

    def test_imei_handshake(self):
        imei, used = codec.try_parse_imei(codec.encode_imei("356307042441013") + b"xx")
        self.assertEqual((imei, used), ("356307042441013", 17))
        with self.assertRaises(NeedMoreData):
            codec.try_parse_imei(b"\x00\x0f3563")

    def test_crc16_ibm_known_value(self):
        self.assertEqual(codec.crc16_ibm(b"123456789"), 0xBB3D)
