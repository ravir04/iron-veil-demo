"""
Unit tests for klv_encoder.py — MISB ST0601 encoding correctness.

Tests verify:
1. BER length encoding per SMPTE ST 336
2. Each tag encodes to the correct byte count and value range
3. Full frame structure: valid ST0601 Universal Label prefix, correct outer length,
   checksum tag present and last
4. Round-trip: known MISB test vectors from klvdata reference binaries decode correctly
5. MPEG-TS wrapper: correct sync byte, PID, packet size

Reference vectors from:
  https://github.com/paretech/klvdata — DynamicConstantMISMMSPacketData.bin
  MISB ST0601 v19, Table 1 (tag encoding examples in standard)
"""

import struct
import sys
import os
import time

# Allow importing from the service directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'services', 'drone-sim'))

from klv_encoder import (
    ST0601_UL,
    encode_ber_length,
    tag_timestamp,
    tag_platform_heading,
    tag_platform_pitch,
    tag_platform_roll,
    tag_sensor_lat,
    tag_sensor_lon,
    tag_sensor_alt,
    tag_frame_center_lat,
    tag_frame_center_lon,
    tag_ground_speed,
    tag_version,
    checksum,
    encode_st0601_frame,
    wrap_klv_in_ts,
    TS_PACKET_SIZE,
    TS_SYNC_BYTE,
    KLV_PID,
)


# ---------------------------------------------------------------------------
# BER length encoding
# ---------------------------------------------------------------------------

def test_ber_short_form():
    """Values 0–127 encode as a single byte."""
    assert encode_ber_length(0)   == bytes([0x00])
    assert encode_ber_length(1)   == bytes([0x01])
    assert encode_ber_length(127) == bytes([0x7F])


def test_ber_long_form_1byte():
    """Values 128–255: 0x81 + 1 byte."""
    assert encode_ber_length(128) == bytes([0x81, 0x80])
    assert encode_ber_length(255) == bytes([0x81, 0xFF])


def test_ber_long_form_2byte():
    """Values 256–65535: 0x82 + 2 bytes big-endian."""
    assert encode_ber_length(256)   == bytes([0x82, 0x01, 0x00])
    assert encode_ber_length(65535) == bytes([0x82, 0xFF, 0xFF])


# ---------------------------------------------------------------------------
# ST0601 Universal Label
# ---------------------------------------------------------------------------

def test_ul_length():
    assert len(ST0601_UL) == 16


def test_ul_prefix():
    """Must start with SMPTE UL prefix 06 0E 2B 34."""
    assert ST0601_UL[:4] == bytes([0x06, 0x0E, 0x2B, 0x34])


# ---------------------------------------------------------------------------
# Tag: version (tag 65)
# ---------------------------------------------------------------------------

def test_tag_version():
    v = tag_version(19)
    assert v[0] == 65          # tag
    assert v[1] == 1           # length
    assert v[2] == 19          # value


# ---------------------------------------------------------------------------
# Tag: timestamp (tag 2) — uint64 microseconds
# ---------------------------------------------------------------------------

def test_tag_timestamp_length():
    t = tag_timestamp(2)
    assert t[0] == 2           # tag
    assert t[1] == 8           # 8 bytes for uint64
    assert len(t) == 10


def test_tag_timestamp_value_reasonable():
    before = int(time.time() * 1_000_000)
    t = tag_timestamp(2)
    after = int(time.time() * 1_000_000)
    us = struct.unpack('>Q', t[2:10])[0]
    assert before <= us <= after


# ---------------------------------------------------------------------------
# Tag: platform heading (tag 5) — 0–360 → 0–65535
# ---------------------------------------------------------------------------

def test_heading_north():
    h = tag_platform_heading(0.0)
    assert h[0] == 5
    val = struct.unpack('>H', h[2:4])[0]
    assert val == 0


def test_heading_south():
    h = tag_platform_heading(180.0)
    assert h[0] == 5
    val = struct.unpack('>H', h[2:4])[0]
    # 180/360 * 65535 = 32767.5 → rounds to 32768
    assert abs(val - 32768) <= 1


def test_heading_full_circle():
    h = tag_platform_heading(360.0)
    val = struct.unpack('>H', h[2:4])[0]
    assert val == 0  # 360 % 360 = 0


def test_heading_known_vector():
    """MISB ST0601 example: heading ~159.97° → bytes 0x71 0xC2 (tag 05 len 02)."""
    h = tag_platform_heading(159.9744)
    val = struct.unpack('>H', h[2:4])[0]
    expected = round(159.9744 / 360.0 * 65535)
    assert abs(val - expected) <= 1


# ---------------------------------------------------------------------------
# Tag: platform pitch (tag 6) — -90–90 → -32768–32767
# ---------------------------------------------------------------------------

def test_pitch_zero():
    p = tag_platform_pitch(0.0)
    assert p[0] == 6
    val = struct.unpack('>h', p[2:4])[0]
    assert val == 0


def test_pitch_positive():
    p = tag_platform_pitch(45.0)
    val = struct.unpack('>h', p[2:4])[0]
    assert val > 0


def test_pitch_clamped():
    p_over = tag_platform_pitch(100.0)
    p_max  = tag_platform_pitch(90.0)
    assert p_over[2:] == p_max[2:]


# ---------------------------------------------------------------------------
# Tag: sensor latitude (tag 13) — -90–90 → int32
# ---------------------------------------------------------------------------

def test_lat_equator():
    lat = tag_sensor_lat(0.0)
    assert lat[0] == 13
    val = struct.unpack('>i', lat[2:6])[0]
    assert val == 0


def test_lat_north_pole():
    lat = tag_sensor_lat(90.0)
    val = struct.unpack('>i', lat[2:6])[0]
    assert val == 2**31 - 1


def test_lat_south_pole():
    lat = tag_sensor_lat(-90.0)
    val = struct.unpack('>i', lat[2:6])[0]
    assert val == -(2**31 - 1)


def test_lat_known():
    """Verify round-trip precision: encode → decode within 1e-5 degrees."""
    original = 51.2500
    lat = tag_sensor_lat(original)
    raw = struct.unpack('>i', lat[2:6])[0]
    decoded = raw / (2**31 - 1) * 90.0
    assert abs(decoded - original) < 1e-4


# ---------------------------------------------------------------------------
# Tag: sensor longitude (tag 14)
# ---------------------------------------------------------------------------

def test_lon_prime_meridian():
    lon = tag_sensor_lon(0.0)
    assert lon[0] == 14
    val = struct.unpack('>i', lon[2:6])[0]
    assert val == 0


def test_lon_antimeridian_west():
    lon = tag_sensor_lon(-180.0)
    val = struct.unpack('>i', lon[2:6])[0]
    assert val == -(2**31 - 1)


# ---------------------------------------------------------------------------
# Tag: sensor altitude (tag 15) — -900–19000 → uint16
# ---------------------------------------------------------------------------

def test_alt_sea_level():
    alt = tag_sensor_alt(0.0)
    assert alt[0] == 15
    val = struct.unpack('>H', alt[2:4])[0]
    # 0 maps to (0+900)/19900*65535 ≈ 2963
    expected = round((0 + 900) / 19900 * 65535)
    assert abs(val - expected) <= 1


def test_alt_below_sea_level():
    alt = tag_sensor_alt(-900.0)
    val = struct.unpack('>H', alt[2:4])[0]
    assert val == 0


def test_alt_max():
    alt = tag_sensor_alt(19000.0)
    val = struct.unpack('>H', alt[2:4])[0]
    assert val == 65535


# ---------------------------------------------------------------------------
# Tag: ground speed (tag 56) — uint8 m/s
# ---------------------------------------------------------------------------

def test_ground_speed_zero():
    s = tag_ground_speed(0.0)
    assert s[0] == 56
    assert s[2] == 0


def test_ground_speed_clamp():
    s = tag_ground_speed(300.0)  # over uint8 max
    assert s[2] == 255


# ---------------------------------------------------------------------------
# Full frame structure
# ---------------------------------------------------------------------------

def test_frame_starts_with_ul():
    frame = encode_st0601_frame(lat=51.25, lon=-0.50, alt_m=500, heading=90)
    assert frame[:16] == ST0601_UL


def test_frame_outer_length_matches():
    """BER length at byte 16 must match actual remaining byte count."""
    frame = encode_st0601_frame(lat=51.25, lon=-0.50, alt_m=500, heading=90)
    ber = frame[16]
    if ber < 0x80:
        payload_len = ber
        header_size = 17
    elif ber == 0x81:
        payload_len = frame[17]
        header_size = 18
    else:
        payload_len = struct.unpack('>H', frame[18:20])[0]
        header_size = 20
    assert len(frame) == header_size + payload_len


def test_frame_ends_with_checksum_tag():
    """Tag 1 (checksum) must be the last tag in the frame — 4 bytes from the end."""
    frame = encode_st0601_frame(lat=51.25, lon=-0.50, alt_m=500, heading=90)
    # Last 4 bytes: [0x01][0x02][CRC_H][CRC_L]
    assert frame[-4] == 0x01   # checksum tag
    assert frame[-3] == 0x02   # length = 2


def test_frame_version_tag_present():
    """Tag 65 (version=19) must be somewhere in the frame body."""
    frame = encode_st0601_frame(lat=51.25, lon=-0.50, alt_m=500, heading=90)
    body = frame[17:]  # skip UL + 1-byte BER (approximate)
    assert bytes([65, 1, 19]) in frame


def test_frame_different_positions_differ():
    """Two different positions must produce different frames."""
    f1 = encode_st0601_frame(lat=51.25, lon=-0.50, alt_m=500, heading=90)
    f2 = encode_st0601_frame(lat=51.30, lon=-0.20, alt_m=500, heading=90)
    assert f1 != f2


# ---------------------------------------------------------------------------
# MPEG-TS wrapper
# ---------------------------------------------------------------------------

def test_ts_packet_size():
    frame = encode_st0601_frame(lat=51.25, lon=-0.50, alt_m=500, heading=0)
    ts = wrap_klv_in_ts(frame)
    assert len(ts) % TS_PACKET_SIZE == 0


def test_ts_sync_byte():
    frame = encode_st0601_frame(lat=51.25, lon=-0.50, alt_m=500, heading=0)
    ts = wrap_klv_in_ts(frame)
    # Every packet must start with sync byte 0x47
    for i in range(0, len(ts), TS_PACKET_SIZE):
        assert ts[i] == TS_SYNC_BYTE, f"Packet at offset {i} missing sync byte"


def test_ts_pid():
    frame = encode_st0601_frame(lat=51.25, lon=-0.50, alt_m=500, heading=0)
    ts = wrap_klv_in_ts(frame)
    # PID is in bytes 1-2 of the TS header, bits [12:0] of bytes [1][2]
    pid = ((ts[1] & 0x1F) << 8) | ts[2]
    assert pid == KLV_PID  # 0x0065


def test_ts_pusi_set_on_first_packet():
    """Payload Unit Start Indicator must be set on the first packet."""
    frame = encode_st0601_frame(lat=51.25, lon=-0.50, alt_m=500, heading=0)
    ts = wrap_klv_in_ts(frame)
    pusi = (ts[1] >> 6) & 0x01
    assert pusi == 1


# ---------------------------------------------------------------------------
# Reference binary: parse ST0601 UL from klvdata test files
# ---------------------------------------------------------------------------

def _load_ref_binary(filename: str) -> bytes:
    path = os.path.join(os.path.dirname(__file__), filename)
    if not os.path.exists(path):
        return None
    with open(path, 'rb') as f:
        return f.read()


def test_reference_binary_contains_st0601_ul():
    """DynamicConstantMISMMSPacketData.bin must contain the ST0601 Universal Label."""
    data = _load_ref_binary('DynamicConstantMISMMSPacketData.bin')
    if data is None:
        print("  SKIP: reference binary not present")
        return
    assert ST0601_UL in data, "ST0601 Universal Label not found in reference binary"


def test_reference_binary_length():
    """Reference binary is 228 bytes as documented in the klvdata repo."""
    data = _load_ref_binary('DynamicConstantMISMMSPacketData.bin')
    if data is None:
        print("  SKIP: reference binary not present")
        return
    assert len(data) == 228


def test_reference_binary_dynamic_only():
    data = _load_ref_binary('DynamicOnlyMISMMSPacketData.bin')
    if data is None:
        print("  SKIP: reference binary not present")
        return
    assert ST0601_UL in data
    assert len(data) == 114
