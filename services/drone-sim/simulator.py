"""
Drone COP Simulator — Iron-Veil Demo

Generates synthetic STANAG 4609-conformant drone telemetry and POSTs each frame
as an ACP-240 ZTDF envelope to Signet's /signet/bulk-ingest endpoint.

Policy labels are determined by the drone's current geofence zone, so each frame
carries the correct classification and releasability for that position in the mission.

Usage:
  python simulator.py                     # Run standard mission
  python simulator.py --interval 1.0      # Frame interval in seconds (default: 1.0)
  python simulator.py --replay file.jsonl # Replay a recorded mission
  python simulator.py --record file.jsonl # Record mission to file while running
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timezone

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as apad
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import jcs

from mission import MissionPath, classify_position, LatLon
from klv_encoder import encode_st0601_frame, wrap_klv_in_ts
from video_generator import generate_ts_segment, generate_ts_segment_simple, SEGMENT_DURATION_S

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SIGNET_BULK_INGEST_URL = os.environ.get("SIGNET_BULK_INGEST_URL", "http://localhost:4774/signet/bulk-ingest")
ISSUER_PRIVATE_KEY_PATH = os.environ.get("ISSUER_PRIVATE_KEY_PATH", "config/trust/issuer_private.pem")
KAS_PUBLIC_KEY_PATH = os.environ.get("KAS_PUBLIC_KEY_PATH", "config/trust/kas_public.pem")
ISSUER_ID = os.environ.get("ISSUER_ID", "drone-sim-dev")
KAS_URL = os.environ.get("KAS_URL", "http://localhost:4774")
FRAME_INTERVAL = float(os.environ.get("FRAME_INTERVAL", "1.0"))
MISSION_ID = os.environ.get("MISSION_ID", "FVEX-26")
DRONE_ID = os.environ.get("DRONE_ID", "UAS-001")

# Bulk ingest: accumulate up to BULK_SIZE frames before flushing.
# At 1 frame/sec this is effectively immediate (bulk of 1).
# Increase FRAME_INTERVAL or BULK_SIZE for higher-throughput testing.
BULK_SIZE = int(os.environ.get("BULK_SIZE", "1"))

# Whether to use real FFmpeg MPEG-TS video (True) or legacy KLV-only TS packets (False)
# Set VIDEO_ENABLED=false to run without FFmpeg (e.g. for unit tests or CI)
VIDEO_ENABLED = os.environ.get("VIDEO_ENABLED", "true").lower() not in ("false", "0", "no")

# ---------------------------------------------------------------------------
# Key loading
# ---------------------------------------------------------------------------

def load_private_key(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def load_public_key(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_public_key(f.read())


# ---------------------------------------------------------------------------
# ACP-240 ZTDF envelope builder
# ---------------------------------------------------------------------------

def make_envelope(
    issuer_priv,
    kas_pub,
    labels: dict,
    plaintext: bytes,
    metadata: dict | None = None,
    object_id: str | None = None,
) -> dict:
    """Build an ACP-240A ZTDF-compliant envelope for drone telemetry."""
    if object_id is None:
        object_id = str(uuid.uuid4())
    policy_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 1. DEK + AES-256-GCM
    dek = os.urandom(32)
    nonce = os.urandom(12)
    ct = AESGCM(dek).encrypt(nonce, plaintext, None)

    # 2. Policy object + policyBinding HMAC
    policy_obj = {"uuid": policy_id, "body": {"dataAttributes": [], "dissem": []}}
    policy_b64 = base64.b64encode(
        json.dumps(policy_obj, separators=(",", ":"), sort_keys=True).encode()
    ).decode()
    pb_hash = base64.b64encode(
        hmac.new(dek, policy_b64.encode(), hashlib.sha256).digest()
    ).decode()

    # 3. JWE: RSA-OAEP-256 wrap of DEK
    enc_dek = kas_pub.encrypt(
        dek, apad.OAEP(mgf=apad.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    )
    jwe_hdr = base64.urlsafe_b64encode(b'{"alg":"RSA-OAEP-256","enc":"A256GCM"}').rstrip(b"=").decode()
    jwe_ek = base64.urlsafe_b64encode(enc_dek).rstrip(b"=").decode()
    wrapped_key = f"{jwe_hdr}.{jwe_ek}.."

    # 4. Integrity signature
    root_sig = base64.b64encode(hmac.new(dek, ct, hashlib.sha256).digest()).decode()

    # 5. STANAG 5636 OCL
    releasability = labels.get("releasability", [])
    caveats = labels.get("caveats", [])
    cls = labels.get("classification", "UNCLASS")
    catl = [{"name": "Releasable To", "type": "P", "vals": sorted(releasability)}]
    if caveats:
        catl.append({"name": "Caveats", "type": "R", "vals": sorted(caveats)})

    # 6. ZTDF core (signed surface)
    core = {
        "schemaVersion": "1.0.0",
        "assertions": [{
            "appliesToState": "encrypted",
            "id": str(uuid.uuid4()),
            "scope": "tdo",
            "statement": {
                "format": "object",
                "schema": "urn:nato:stanag:5636:A:1:elements:json",
                "value": {"ocl": {"catl": catl, "cls": cls, "dcr": now, "pol": policy_id}}
            },
            "type": "handling"
        }],
        "encryptionInformation": {
            "integrityInformation": {
                "rootSignature": {"alg": "HS256", "sig": root_sig},
                "segmentHashAlg": "GMAC",
                "segmentSizeDefault": 1000000,
                "segments": [{
                    "encryptedSegmentSize": len(ct),
                    "hash": base64.b64encode(ct[-16:]).decode(),
                    "segmentSize": len(plaintext)
                }]
            },
            "keyAccess": [{
                "kid": "kas-dev-1",
                "policyBinding": {"alg": "HS256", "hash": pb_hash},
                "protocol": "kas",
                "sid": policy_id,
                "type": "wrapped",
                "url": KAS_URL,
                "wrappedKey": wrapped_key
            }],
            "method": {
                "algorithm": "AES-256-GCM",
                "isStreamable": True,
                "iv": base64.b64encode(nonce).decode()
            },
            "policy": policy_b64,
            "type": "split"
        },
        "payload": {
            "isEncrypted": True,
            "mimeType": "video/MP2T",
            "protocol": "zip",
            "type": "reference",
            "url": "0.payload"
        }
    }

    # 7. RSA-PSS signature
    core_bytes = jcs.canonicalize(core)
    manifest_hash = "sha256:" + hashlib.sha256(core_bytes).hexdigest()
    sig = issuer_priv.sign(
        core_bytes,
        apad.PSS(mgf=apad.MGF1(hashes.SHA256()), salt_length=apad.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )

    envelope = {
        **core,
        "ciphertext_b64": base64.b64encode(ct).decode(),
        "issuer": {"issuer_id": ISSUER_ID},
        "manifest_hash": manifest_hash,
        "object_id": object_id,
        "signature_b64": base64.b64encode(sig).decode(),
    }

    # 8. SIG-008: optional mission metadata (stored unencrypted alongside object in Signet)
    if metadata:
        envelope["metadata"] = metadata

    return envelope


# ---------------------------------------------------------------------------
# Bulk ingest flush
# ---------------------------------------------------------------------------

def flush_bulk(batch: list[dict], session: requests.Session) -> list[dict]:
    """POST a batch to /signet/bulk-ingest. Returns per-envelope results."""
    if not batch:
        return []
    try:
        r = session.post(SIGNET_BULK_INGEST_URL, json={"envelopes": batch}, timeout=10)
        if r.ok:
            body = r.json()
            # Signet returns {"results": [...]} or bare [...]
            return body.get("results", body) if isinstance(body, dict) else body
        else:
            return [{"object_id": e["object_id"], "ok": False, "error": f"http_{r.status_code}"} for e in batch]
    except Exception as ex:
        return [{"object_id": e["object_id"], "ok": False, "error": str(ex)} for e in batch]


# ---------------------------------------------------------------------------
# Main simulation loop
# ---------------------------------------------------------------------------

def run_mission(args):
    issuer_priv = load_private_key(ISSUER_PRIVATE_KEY_PATH)
    kas_pub = load_public_key(KAS_PUBLIC_KEY_PATH)
    session = requests.Session()

    mission = MissionPath()
    mission_time = 0.0
    frame_seq = 0
    pending_batch: list[dict] = []
    record_file = open(args.record, "w") if args.record else None

    print(f"[drone-sim] Mission: {MISSION_ID}  Drone: {DRONE_ID}")
    print(f"[drone-sim] Frame interval: {args.interval}s  Bulk size: {BULK_SIZE}")
    print(f"[drone-sim] Segment duration: {SEGMENT_DURATION_S}s  Video: {'FFmpeg MPEG-TS' if VIDEO_ENABLED else 'KLV-only'}")
    print(f"[drone-sim] Ingesting to: {SIGNET_BULK_INGEST_URL}")

    try:
        while not mission.complete:
            t_start = time.monotonic()

            pos, wp = mission.advance(dt_seconds=args.interval)
            zone = classify_position(pos)
            heading = mission.heading()

            # MISB ST0601 KLV frame
            klv_bytes = encode_st0601_frame(
                lat=pos.lat,
                lon=pos.lon,
                alt_m=wp.altitude_m,
                heading=heading,
                pitch=0.0,
                roll=0.0,
                speed_ms=wp.speed_ms,
            )

            if VIDEO_ENABLED:
                # Full STANAG 4609 MPEG-TS segment: H.264 video + KLV muxed
                vid_meta = {
                    "zone": zone.name,
                    "classification": zone.classification,
                    "lat": round(pos.lat, 6),
                    "lon": round(pos.lon, 6),
                    "alt_m": wp.altitude_m,
                    "mission_time_s": int(mission_time),
                    "frame_seq": frame_seq,
                }
                try:
                    ts_payload = generate_ts_segment(vid_meta, klv_bytes, duration_s=SEGMENT_DURATION_S)
                except Exception as e:
                    print(f"[drone-sim] WARN: FFmpeg segment failed ({e}), falling back to KLV-only TS")
                    ts_payload = wrap_klv_in_ts(klv_bytes, continuity_counter=frame_seq % 16)
            else:
                # KLV-only MPEG-TS packet (no video codec required — for testing)
                ts_payload = wrap_klv_in_ts(klv_bytes, continuity_counter=frame_seq % 16)

            # ACP-240 labels from geofence zone
            labels = {
                "classification": zone.classification,
                "releasability": zone.releasability,
                "caveats": zone.caveats,
            }

            # SIG-008: mission metadata (unencrypted, stored in Signet objects table)
            metadata = {
                "mission_id": MISSION_ID,
                "drone_id": DRONE_ID,
                "frame_seq": frame_seq,
                "sensor_ts": time.time(),
                "zone": zone.name,
                "lat": round(pos.lat, 6),
                "lon": round(pos.lon, 6),
                "alt_m": wp.altitude_m,
                "mission_time_s": int(mission_time),
                "segment_duration_s": SEGMENT_DURATION_S,
                "mime_type": "video/MP2T",
            }

            envelope = make_envelope(issuer_priv, kas_pub, labels, ts_payload, metadata=metadata)
            pending_batch.append(envelope)

            if record_file:
                record_file.write(json.dumps({
                    "object_id": envelope["object_id"],
                    "ts": time.time(),
                    **metadata,
                    "classification": zone.classification,
                    "releasability": zone.releasability,
                }) + "\n")
                record_file.flush()

            # Flush when batch is full or on last frame
            if len(pending_batch) >= BULK_SIZE or mission.complete:
                results = flush_bulk(pending_batch, session)
                for env, res in zip(pending_batch, results):
                    meta = env.get("metadata", {})
                    status = "OK" if res.get("ok") else f"FAIL({res.get('error','')})"
                    print(
                        f"[T+{meta.get('mission_time_s',0):5.0f}s] "
                        f"{meta.get('zone','?'):20s} "
                        f"{labels['classification']:10s} "
                        f"{','.join(labels['releasability']):12s} "
                        f"lat={meta.get('lat',0):.4f} lon={meta.get('lon',0):.4f}  → {status}"
                    )
                pending_batch.clear()

            frame_seq += 1
            mission_time += args.interval

            elapsed = time.monotonic() - t_start
            time.sleep(max(0.0, args.interval - elapsed))

    finally:
        # Flush any remaining frames
        if pending_batch:
            flush_bulk(pending_batch, session)
        if record_file:
            record_file.close()

    print(f"[drone-sim] Mission complete. {frame_seq} frames ingested.")


# ---------------------------------------------------------------------------
# Replay mode
# ---------------------------------------------------------------------------

def run_replay(args):
    issuer_priv = load_private_key(ISSUER_PRIVATE_KEY_PATH)
    kas_pub = load_public_key(KAS_PUBLIC_KEY_PATH)
    session = requests.Session()

    print(f"[drone-sim] Replaying from {args.replay}")
    with open(args.replay) as f:
        entries = [json.loads(line) for line in f if line.strip()]

    pending_batch: list[dict] = []

    for entry in entries:
        klv_bytes = encode_st0601_frame(
            lat=entry["lat"],
            lon=entry["lon"],
            alt_m=entry.get("alt_m", 500),
            heading=0.0,
            speed_ms=50.0,
        )
        ts_payload = wrap_klv_in_ts(klv_bytes)

        labels = {
            "classification": entry["classification"],
            "releasability": entry["releasability"],
            "caveats": [],
        }
        metadata = {
            "mission_id": entry.get("mission_id", MISSION_ID),
            "drone_id": entry.get("drone_id", DRONE_ID),
            "frame_seq": entry.get("frame_seq", 0),
            "sensor_ts": entry.get("ts", time.time()),
            "zone": entry.get("zone", "UNKNOWN"),
            "lat": entry["lat"],
            "lon": entry["lon"],
            "alt_m": entry.get("alt_m", 500),
            "mission_time_s": entry.get("mission_time_s", 0),
        }

        envelope = make_envelope(issuer_priv, kas_pub, labels, ts_payload, metadata=metadata)
        pending_batch.append(envelope)

        if len(pending_batch) >= BULK_SIZE:
            results = flush_bulk(pending_batch, session)
            for res in results:
                print(f"  replay → {'OK' if res.get('ok') else res.get('error')}")
            pending_batch.clear()
            time.sleep(args.interval)

    if pending_batch:
        flush_bulk(pending_batch, session)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Iron-Veil Demo Drone Simulator")
    ap.add_argument("--interval", type=float, default=FRAME_INTERVAL)
    ap.add_argument("--replay", help="Replay a recorded mission JSONL file")
    ap.add_argument("--record", help="Record mission to JSONL file")
    args = ap.parse_args()

    if args.replay:
        run_replay(args)
    else:
        run_mission(args)


if __name__ == "__main__":
    main()
