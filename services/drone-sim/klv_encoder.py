"""
MISB ST0601 KLV encoder — STANAG 4609 conformant.

Produces a binary KLV Local Set packet that can be:
  - Embedded in an MPEG-TS PES packet (PID 0x0065) for full STANAG 4609 compliance
  - Treated as the plaintext payload of an ACP-240 ZTDF envelope

References:
  MISB ST 0601 v19 — UAS Datalink Local Set
  SMPTE ST 336-2007 — KLV encoding rules
"""

from __future__ import annotations
import struct
import time

# ---------------------------------------------------------------------------
# MISB ST0601 Universal Label (16 bytes)
# Used as the outer KLV key for the Local Set frame
# ---------------------------------------------------------------------------
ST0601_UL = bytes.fromhex("060E2B34020B0101 0E01030101000000".replace(" ", ""))


# ---------------------------------------------------------------------------
# BER length encoding (SMPTE ST 336)
# ---------------------------------------------------------------------------

def encode_ber_length(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    elif length < 0x100:
        return bytes([0x81, length])
    elif length < 0x10000:
        return bytes([0x82]) + struct.pack(">H", length)
    else:
        return bytes([0x83]) + struct.pack(">I", length)[1:]  # 3 bytes


# ---------------------------------------------------------------------------
# Tag helpers — each returns (tag_byte | ber_length | value_bytes)
# ---------------------------------------------------------------------------

def _tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + encode_ber_length(len(value)) + value


def tag_timestamp(tag: int = 2) -> bytes:
    """Tag 2: Unix Time Stamp (uint64, microseconds since epoch)."""
    us = int(time.time() * 1_000_000)
    return _tlv(tag, struct.pack(">Q", us))


def tag_uint8(tag: int, value: int) -> bytes:
    return _tlv(tag, struct.pack(">B", max(0, min(255, int(value)))))


def tag_uint16(tag: int, value: int) -> bytes:
    return _tlv(tag, struct.pack(">H", max(0, min(65535, int(value)))))


def tag_int16(tag: int, value: int) -> bytes:
    return _tlv(tag, struct.pack(">h", max(-32768, min(32767, int(value)))))


def tag_int32(tag: int, value: int) -> bytes:
    return _tlv(tag, struct.pack(">i", max(-2147483648, min(2147483647, int(value)))))


def tag_platform_heading(degrees: float) -> bytes:
    """Tag 5: Platform Heading Angle. 0–360 → 0–65535."""
    scaled = int(round((degrees % 360) / 360.0 * 65535))
    return tag_uint16(5, scaled)


def tag_platform_pitch(degrees: float) -> bytes:
    """Tag 6: Platform Pitch Angle. -90–90 → -32768–32767."""
    clamped = max(-90.0, min(90.0, degrees))
    scaled = int(round(clamped / 90.0 * 32767))
    return tag_int16(6, scaled)


def tag_platform_roll(degrees: float) -> bytes:
    """Tag 7: Platform Roll Angle. -180–180 → -32768–32767."""
    clamped = max(-180.0, min(180.0, degrees))
    scaled = int(round(clamped / 180.0 * 32767))
    return tag_int16(7, scaled)


def tag_sensor_lat(lat: float) -> bytes:
    """Tag 13: Sensor Latitude. -90–90 → -(2^31) to (2^31-1)."""
    scaled = int(round(lat / 90.0 * (2**31 - 1)))
    return tag_int32(13, scaled)


def tag_sensor_lon(lon: float) -> bytes:
    """Tag 14: Sensor Longitude. -180–180 → -(2^31) to (2^31-1)."""
    scaled = int(round(lon / 180.0 * (2**31 - 1)))
    return tag_int32(14, scaled)


def tag_sensor_alt(alt_m: float) -> bytes:
    """Tag 15: Sensor True Altitude. -900–19000 m → 0–65535."""
    # Linear mapping: value = (alt + 900) / 19900 * 65535
    clamped = max(-900.0, min(19000.0, alt_m))
    scaled = int(round((clamped + 900.0) / 19900.0 * 65535))
    return tag_uint16(15, scaled)


def tag_frame_center_lat(lat: float) -> bytes:
    """Tag 17: Frame Center Latitude. Same encoding as sensor lat."""
    return _tlv(17, struct.pack(">i", int(round(lat / 90.0 * (2**31 - 1)))))


def tag_frame_center_lon(lon: float) -> bytes:
    """Tag 18: Frame Center Longitude. Same encoding as sensor lon."""
    return _tlv(18, struct.pack(">i", int(round(lon / 180.0 * (2**31 - 1)))))


def tag_ground_speed(speed_ms: float) -> bytes:
    """Tag 56: Platform Ground Speed. 0–255 m/s → uint8."""
    return tag_uint8(56, int(round(speed_ms)))


def tag_version(version: int = 19) -> bytes:
    """Tag 65: UAS Datalink LS Version Number."""
    return tag_uint8(65, version)


def checksum(packet_without_checksum: bytes) -> bytes:
    """
    Tag 1: Checksum. CRC-16/CCITT of the entire packet bytes up to (not including) the checksum TLV.
    The checksum TLV itself is 4 bytes: [0x01, 0x02, CRC_high, CRC_low].
    """
    crc = 0xFFFF
    for byte in packet_without_checksum:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
        crc &= 0xFFFF
    return _tlv(1, struct.pack(">H", crc))


# ---------------------------------------------------------------------------
# High-level encoder
# ---------------------------------------------------------------------------

def encode_st0601_frame(
    lat: float,
    lon: float,
    alt_m: float,
    heading: float,
    pitch: float = 0.0,
    roll: float = 0.0,
    speed_ms: float = 0.0,
    mission_id: str = "DEMO",
) -> bytes:
    """
    Build a complete MISB ST0601 Local Set KLV frame.
    Returns raw bytes: [ST0601_UL][BER length][tags...][checksum tag].

    This is the plaintext payload that will be wrapped in a ZTDF envelope.
    """
    # Assemble the tags (body without checksum)
    body = (
        tag_version(19)
        + tag_timestamp(2)
        + tag_platform_heading(heading)
        + tag_platform_pitch(pitch)
        + tag_platform_roll(roll)
        + tag_sensor_lat(lat)
        + tag_sensor_lon(lon)
        + tag_sensor_alt(alt_m)
        + tag_frame_center_lat(lat)    # simplified: frame center = sensor position
        + tag_frame_center_lon(lon)
        + tag_ground_speed(speed_ms)
    )

    # Compute checksum over UL + length + body (as they will appear in the packet)
    # We need the full prefix before checksum to compute it
    body_with_ck_placeholder = body  # checksum tag will be appended
    # The checksum covers: UL(16) + length_of_all(ber) + body + checksum_tag_id + checksum_length
    # Per MISB ST0601: checksum is the last tag; CRC covers all bytes from UL through the checksum length byte
    ck_prefix_len = len(ST0601_UL) + len(encode_ber_length(len(body) + 4)) + len(body) + 2  # +2 for tag 0x01 + len 0x02
    partial = ST0601_UL + encode_ber_length(len(body) + 4) + body + bytes([0x01, 0x02])
    ck = checksum(partial)  # 4 bytes: [0x01, 0x02, CRC_H, CRC_L]

    full_body = body + ck
    packet = ST0601_UL + encode_ber_length(len(full_body)) + full_body
    return packet


# ---------------------------------------------------------------------------
# MPEG-TS wrapping (minimal, for STANAG 4609 structural conformance)
# ---------------------------------------------------------------------------

TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47

KLV_PID = 0x0065   # PID 101 for KLV metadata per STANAG 4609
VIDEO_PID = 0x0041  # PID 65 for video


def _ts_header(pid: int, payload_unit_start: bool, continuity_counter: int) -> bytes:
    """Build a 4-byte MPEG-TS header."""
    pusi = 0x40 if payload_unit_start else 0x00
    pid_hi = (pid >> 8) & 0x1F
    pid_lo = pid & 0xFF
    flags = 0x10 | (continuity_counter & 0x0F)  # payload only, no adaptation field
    return bytes([TS_SYNC_BYTE, pusi | pid_hi, pid_lo, flags])


def _pes_header(stream_id: int = 0xBD, payload_len: int = 0) -> bytes:
    """Build a minimal PES header for KLV metadata (stream_id 0xBD = private stream 1)."""
    # PES packet length: 0 = unbounded for video; use actual length for metadata
    pes_len = payload_len + 3 if payload_len > 0 else 0  # +3 for PES header extension
    return bytes([
        0x00, 0x00, 0x01, stream_id,        # start code + stream ID
        (pes_len >> 8) & 0xFF, pes_len & 0xFF,  # PES packet length
        0x80, 0x00, 0x00,                    # flags: no PTS/DTS, header data length 0
    ])


def wrap_klv_in_ts(klv_bytes: bytes, continuity_counter: int = 0) -> bytes:
    """
    Wrap a KLV Local Set frame in a minimal MPEG-TS structure.
    Produces one or more 188-byte TS packets on PID 0x0065.
    For demo purposes, produces a single PES-in-TS if the payload fits.
    """
    pes = _pes_header(stream_id=0xBD, payload_len=len(klv_bytes)) + klv_bytes
    # Pad or split into 188-byte packets
    packets = bytearray()
    first = True
    offset = 0
    cc = continuity_counter & 0x0F

    while offset < len(pes):
        header = _ts_header(KLV_PID, first, cc)
        available = TS_PACKET_SIZE - 4
        chunk = pes[offset:offset + available]
        if len(chunk) < available:
            chunk = chunk + bytes(available - len(chunk))  # pad with 0x00
        packets += header + chunk
        offset += available
        first = False
        cc = (cc + 1) & 0x0F

    return bytes(packets)
