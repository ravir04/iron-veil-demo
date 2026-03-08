# STANAG 4609 / MISB ST0601 Implementation Notes

---

## Overview

**STANAG 4609** (NATO Standardization Agreement 4609, Edition 4) defines the standard for motion imagery from Unmanned Air Systems (UAS) — commonly known as Full Motion Video (FMV). It mandates:

- **MPEG-2 Transport Stream** (MPEG-TS, `.ts`) as the container
- **KLV metadata** (Key-Length-Value, per SMPTE ST 336) embedded in the transport stream
- **MISB ST0601** metadata schema (UAS Datalink Local Set) for the KLV payload
- **MISB ST0102** security metadata (classification labels in KLV form — separate from ACP-240 labels)
- Asynchronous metadata: KLV packets on a dedicated PID, video on another PID

### Key Standards Referenced

| Standard | Role |
|---|---|
| STANAG 4609 Ed.4 | Top-level NATO FMV standard |
| MISB ST0601 v19 | UAS Datalink Local Set — the primary telemetry metadata schema |
| MISB ST0102 v12 | Security Metadata Local Set — classification in KLV |
| MISB ST0107 | Multipack — how to embed multiple local sets in one frame |
| SMPTE ST 336 | KLV encoding rules (Universal Label, BER length, value bytes) |
| MPEG-2 TS (ISO 13818-1) | Transport stream container |

---

## MISB ST0601 Key Tags Used in This Demo

The drone-sim implements a subset of MISB ST0601 sufficient for a live track demonstration. Tags are in decimal; values are encoded per the MISB ST0601 BER-OID and floating-point conversion specifications.

| Tag | Name | Type | Notes |
|---|---|---|---|
| 1 | Checksum | uint16 | CRC-16/CCITT of entire packet |
| 2 | Unix Time Stamp | uint64 | Microseconds since 1970-01-01 00:00:00 UTC |
| 5 | Platform Heading Angle | uint16 | 0–360° mapped to 0–65535 |
| 6 | Platform Pitch Angle | int16 | -90–90° |
| 7 | Platform Roll Angle | int16 | -180–180° |
| 13 | Sensor Latitude | int32 | -90–90° (sensor/platform lat) |
| 14 | Sensor Longitude | int32 | -180–180° |
| 15 | Sensor True Altitude | uint16 | Meters, offset -900 |
| 17 | Frame Center Latitude | int32 | Ground point below sensor |
| 18 | Frame Center Longitude | int32 | Ground point below sensor |
| 56 | Platform Ground Speed | uint8 | m/s |
| 65 | UAS Datalink LS Version Number | uint8 | Must be 19 for ST0601 v19 |

### Encoding Rules Summary

All MISB ST0601 tags use the **SDDS KLV** encoding:
- **Key**: 16-byte Universal Label prefix + 1-byte local tag (for Local Set, the key is just the 1-byte local tag inside the LS envelope)
- **Length**: BER short form (1 byte if ≤ 127 bytes) or long form
- **Value**: big-endian binary, scaled as specified per tag

**Local Set Key** (Universal Label for MISB ST0601):
```
06 0E 2B 34 02 0B 01 01 0E 01 03 01 01 00 00 00
```

---

## MPEG-TS Encapsulation

Per STANAG 4609:
- Video PID: `0x0041` (65) — H.264/AVC or H.265/HEVC
- KLV metadata PID: `0x0065` (101)
- Both PIDs carried in the same MPEG-TS multiplex
- KLV metadata is **asynchronous** — one KLV frame per ~1 second regardless of video frame rate
- PAT/PMT include stream type `0x15` (Metadata in PES packets) for the KLV PID

### Minimal MPEG-TS Structure

```
Transport Stream Packet (188 bytes each):
  [PAT packet]  - Program Association Table
  [PMT packet]  - Program Map Table, lists PID 0x0041 (video) and 0x0065 (KLV)
  [PES packet on 0x0041] - H.264 video elementary stream
  [PES packet on 0x0065] - KLV metadata (one Local Set frame per PES)
```

---

## Synthetic Data Approach

Since no real drone hardware is available, the drone-sim generates:

### 1. Synthetic KLV Telemetry
Full MISB ST0601 KLV packets with computed-from-mission-path values. All tags are binary-correct per the standard. Output can be parsed by any MISB-compliant tool (QGIS FMV, misb.js, klvdata).

### 2. Synthetic Video Frames
Generated using Python `Pillow` library:
- **1280×720 JPEG** frames with terrain colour keyed to zone
  - Green = transit/open airspace
  - Brown = exercise corridor
  - Blue = Canadian base area
  - Red = UK base area
  - Orange = target area
- Overlay text: timestamp, lat/lon, altitude, heading, zone name
- Frames encoded into H.264 using `ffmpeg` subprocess

### 3. Available Real Sample Data (for testing)

The following public sources contain actual STANAG 4609 / MISB-compliant FMV files:

| Source | Format | Notes |
|---|---|---|
| **ESRI FMV sample** (Google Drive, ESRI copyright) | MPEG-TS with ST0601 | Referenced by QGIS FMV plugin; real UAS footage |
| **klvdata test file** `DynamicConstantMISMMSPacketData.bin` | Raw KLV binary | Included in [paretech/klvdata](https://github.com/paretech/klvdata) repo, `/data/` directory |
| **jmisb test resources** | Java test vectors | In [WestRidgeSystems/jmisb](https://github.com/WestRidgeSystems/jmisb) test corpus |

For demo purposes, synthetic data is preferred as it gives full control over mission path, timing, and zone transitions.

---

## Tools for Verification

| Tool | Purpose |
|---|---|
| [klvdata](https://github.com/paretech/klvdata) (Python) | Parse and verify KLV from MPEG-TS (requires ffmpeg demux first) |
| [misb.js](https://github.com/vidterra/misb.js) (JS) | Browser-side KLV parsing for COP-UI |
| [QGIS FMV](https://github.com/All4Gis/QGISFMV) (Python/QGIS) | Full motion video player with track overlay |
| [ffmpeg](https://ffmpeg.org) | MPEG-TS muxing, video encoding, KLV stream inspection |
| VLC with klv plugin | Quick visual inspection of MPEG-TS files |

### FFmpeg: Inspect KLV in MPEG-TS
```bash
# Extract raw KLV from MPEG-TS and pipe to klvdata
ffmpeg -i drone.ts -map 0:d -codec copy -f data - | python -m klvdata
```

### FFmpeg: Create MPEG-TS from raw H.264 + KLV
```bash
ffmpeg \
  -f rawvideo -pix_fmt rgb24 -s 1280x720 -r 30 -i video_frames.raw \
  -f data -i klv_frames.bin \
  -c:v libx264 -preset fast \
  -map 0:v -map 1:d \
  -metadata:s:1 handler_name="KLV" \
  drone.ts
```

---

## Relationship to ACP-240 / STANAG 5636 Labels

STANAG 4609 / MISB ST0102 defines its own KLV-based security labels (classification, caveats, releasability in KLV tags). These are **separate from** the ACP-240 ZTDF envelope labels used by Signet.

In this demo:
- **MISB ST0102 tags** are NOT embedded in the KLV stream (optional for demo simplicity)
- **ACP-240 ZTDF labels** (STANAG 5636 OCL) are applied as the envelope wrapper around the entire KLV payload
- The ACP-240 envelope is what Signet enforces — it is the authoritative security control
- The KLV content is treated as the payload (like plaintext in the matrix-proxy demo)

If full MISB ST0102 compliance is required, tags can be added to the KLV encoder in a future phase (see `UPSTREAM-REQUIREMENTS.md`).

---

## MISB ST0601 Encoding Reference (Python)

```python
import struct, time

def encode_uint16_field(tag: int, value_0_to_65535: int) -> bytes:
    v = struct.pack('>H', int(value_0_to_65535))
    return bytes([tag, len(v)]) + v

def encode_int32_field(tag: int, value: int) -> bytes:
    v = struct.pack('>i', value)
    return bytes([tag, len(v)]) + v

def encode_lat(tag: int, degrees: float) -> bytes:
    # Scale: -90 to 90 maps to -(2^31) to (2^31 - 1)
    scaled = int(round(degrees / 90.0 * (2**31 - 1)))
    return encode_int32_field(tag, scaled)

def encode_lon(tag: int, degrees: float) -> bytes:
    # Scale: -180 to 180 maps to -(2^31) to (2^31 - 1)
    scaled = int(round(degrees / 180.0 * (2**31 - 1)))
    return encode_int32_field(tag, scaled)

def encode_timestamp(tag: int = 2) -> bytes:
    # Microseconds since epoch, uint64
    us = int(time.time() * 1_000_000)
    v = struct.pack('>Q', us)
    return bytes([tag, len(v)]) + v

# MISB ST0601 Universal Label (16 bytes)
ST0601_UL = bytes.fromhex('060E2B34020B01010E01030101000000')
```
