"""
Synthetic FMV sample data generator — no Signet or keys required.

Generates two artefacts:

1. fixtures/mission_recording.jsonl
   Pre-baked mission replay file. Each line is one frame:
   {object_id, ts, zone, lat, lon, alt_m, mission_time_s, classification, releasability}
   Used by: simulator.py --replay  (skips re-encryption, just re-wraps)

2. fixtures/mission_klv.ts
   Raw MPEG-TS file containing MISB ST0601 KLV frames for the full mission,
   one per second, no encryption. Useful for:
   - Verifying KLV encoding with external tools (klvdata, QGIS FMV, VLC)
   - Inspecting the mission telemetry without standing up any services
   - Providing real test data to the Signet and Iron-Veil teams

3. fixtures/mission_summary.json
   Human-readable mission summary: total frames, zone breakdown, operator
   visibility counts — useful for verifying the demo will produce the
   expected visible/redacted counts for each operator.

Usage:
  cd iron-veil-demo
  python tests/generate_sample_data.py

Output:
  tests/fixtures/mission_recording.jsonl   (~330 lines, one per second)
  tests/fixtures/mission_klv.ts            (~60 KB MPEG-TS)
  tests/fixtures/mission_summary.json
"""

from __future__ import annotations
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'services', 'drone-sim'))

from mission import MissionPath, classify_position, LatLon
from klv_encoder import encode_st0601_frame, wrap_klv_in_ts

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), 'fixtures')
MISSION_ID = "FVEX-26"
DRONE_ID   = "UAS-001"
FRAME_INTERVAL_S = 1.0

# Operator profiles (mirrors demo scenario)
OPERATORS = {
    "alice": {"clearance": "SECRET",    "rels": {"CAN","FVEY","NATO","AUS","NZL","GBR","USA"}},
    "bob":   {"clearance": "SECRET",    "rels": {"CAN"}},
    "dave":  {"clearance": "PROTECTED", "rels": {"CAN","FVEY"}},
}

RANK = {"UNCLASS": 0, "PROTECTED": 1, "SECRET": 2}


def can_read(zone, clearance: str, rels: set) -> bool:
    if RANK.get(clearance, 0) < RANK.get(zone.classification, 0):
        return False
    required = set(zone.releasability)
    if required and not required.intersection(rels):
        return False
    return True


def generate():
    os.makedirs(FIXTURES_DIR, exist_ok=True)

    jsonl_path   = os.path.join(FIXTURES_DIR, 'mission_recording.jsonl')
    ts_path      = os.path.join(FIXTURES_DIR, 'mission_klv.ts')
    summary_path = os.path.join(FIXTURES_DIR, 'mission_summary.json')

    mission = MissionPath()
    mission_time = 0.0
    frame_seq = 0
    base_ts = time.time()

    # Counters
    zone_counts: dict[str, int] = {}
    op_visible: dict[str, int] = {op: 0 for op in OPERATORS}

    frames = []

    print(f"Generating mission frames at {FRAME_INTERVAL_S}s interval...")

    while not mission.complete:
        pos, wp = mission.advance(dt_seconds=FRAME_INTERVAL_S)
        zone = classify_position(pos)
        heading = mission.heading()

        # KLV frame
        klv = encode_st0601_frame(
            lat=pos.lat,
            lon=pos.lon,
            alt_m=wp.altitude_m,
            heading=heading,
            pitch=0.0,
            roll=0.0,
            speed_ms=wp.speed_ms,
        )
        ts_packet = wrap_klv_in_ts(klv, continuity_counter=frame_seq % 16)

        # JSONL record
        record = {
            "object_id": str(uuid.uuid4()),
            "ts": base_ts + mission_time,
            "mission_id": MISSION_ID,
            "drone_id": DRONE_ID,
            "frame_seq": frame_seq,
            "zone": zone.name,
            "lat": round(pos.lat, 6),
            "lon": round(pos.lon, 6),
            "alt_m": wp.altitude_m,
            "mission_time_s": int(mission_time),
            "classification": zone.classification,
            "releasability": zone.releasability,
            "caveats": zone.caveats,
            "klv_hex": klv.hex(),        # raw KLV for verification
            "ts_hex": ts_packet.hex(),   # MPEG-TS wrapped
        }
        frames.append(record)

        zone_counts[zone.name] = zone_counts.get(zone.name, 0) + 1
        for op, profile in OPERATORS.items():
            if can_read(zone, profile["clearance"], profile["rels"]):
                op_visible[op] += 1

        frame_seq += 1
        mission_time += FRAME_INTERVAL_S

    total = len(frames)
    print(f"  Generated {total} frames")

    # Write JSONL (without klv_hex/ts_hex for the replay file — keep it lean)
    with open(jsonl_path, 'w') as f:
        for rec in frames:
            replay_rec = {k: v for k, v in rec.items() if k not in ('klv_hex', 'ts_hex')}
            f.write(json.dumps(replay_rec) + '\n')
    print(f"  Wrote {jsonl_path}")

    # Write raw MPEG-TS (all frames concatenated — one continuous stream)
    with open(ts_path, 'wb') as f:
        for rec in frames:
            f.write(bytes.fromhex(rec['ts_hex']))
    ts_size_kb = os.path.getsize(ts_path) / 1024
    print(f"  Wrote {ts_path} ({ts_size_kb:.1f} KB)")

    # Write summary
    summary = {
        "mission_id": MISSION_ID,
        "drone_id": DRONE_ID,
        "total_frames": total,
        "duration_seconds": total * FRAME_INTERVAL_S,
        "frame_interval_s": FRAME_INTERVAL_S,
        "zone_frame_counts": zone_counts,
        "operator_visible_frames": op_visible,
        "operator_redacted_frames": {op: total - v for op, v in op_visible.items()},
        "operator_visibility_pct": {op: round(v / total * 100, 1) for op, v in op_visible.items()},
        "zone_labels": {
            zone_name: {
                "classification": classify_position(LatLon(
                    next(r["lat"] for r in frames if r["zone"] == zone_name),
                    next(r["lon"] for r in frames if r["zone"] == zone_name),
                )).classification,
                "releasability": classify_position(LatLon(
                    next(r["lat"] for r in frames if r["zone"] == zone_name),
                    next(r["lon"] for r in frames if r["zone"] == zone_name),
                )).releasability,
            }
            for zone_name in sorted(zone_counts.keys())
        },
    }
    with open(summary_path, 'w') as f:
        json.dump(summary, f, indent=2)
    print(f"  Wrote {summary_path}")

    # Print operator visibility table
    print()
    print("Operator visibility summary:")
    print(f"  {'Operator':<10} {'Visible':>8} {'Redacted':>10} {'%':>6}")
    print(f"  {'-'*36}")
    for op in OPERATORS:
        v = op_visible[op]
        r = total - v
        pct = v / total * 100
        print(f"  {op:<10} {v:>8} {r:>10} {pct:>5.1f}%")

    print()
    print("Zone breakdown:")
    print(f"  {'Zone':<25} {'Frames':>8} {'Classification':<15} {'Releasability'}")
    print(f"  {'-'*70}")
    for zone_name, count in sorted(zone_counts.items()):
        sample = next(r for r in frames if r["zone"] == zone_name)
        z = classify_position(LatLon(sample["lat"], sample["lon"]))
        print(f"  {zone_name:<25} {count:>8} {z.classification:<15} {','.join(z.releasability)}")

    print()
    print("Inspect with klvdata:")
    print(f"  pip install klvdata")
    print(f"  python -c \"")
    print(f"    import klvdata, io")
    print(f"    data = open('tests/fixtures/mission_klv.ts','rb').read()")
    print(f"    # Extract KLV — the TS file has raw PES payload starting after 4-byte TS header + 9-byte PES header")
    print(f"    # Use ffmpeg to demux first:")
    print(f"    # ffmpeg -i tests/fixtures/mission_klv.ts -map 0:d -codec copy -f data - | python -m klvdata")
    print(f"  \"")


if __name__ == '__main__':
    generate()
