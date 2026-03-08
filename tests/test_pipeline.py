"""
Integration tests — full pipeline: ingest → SSE → unwrap per operator.

These tests require a running Signet instance. They are skipped automatically
if Signet is not reachable at SIGNET_URL.

Run with Signet up:
  pytest tests/test_pipeline.py -v

Tests verify:
1. A frame ingested via /signet/bulk-ingest appears in /signet/objects
2. /signet/stream/objects SSE pushes the object_id after ingest
3. Alice (SECRET/FVEY) can unwrap all zone labels
4. Bob (SECRET/CAN) can unwrap CAN_BASE frames but not UK_BASE or TARGET
5. Dave (PROTECTED/FVEY) can unwrap TRANSIT frames but not SECRET frames
6. Unwrap response includes labels inline (SIG-002)
7. metadata JSONB stored and returned in /signet/objects (SIG-008)
"""

import os
import sys
import time
import json
import threading
import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'services', 'drone-sim'))

from mission import CAN_BASE, UK_BASE, TARGET, LatLon, classify_position
from klv_encoder import encode_st0601_frame, wrap_klv_in_ts
from simulator import make_envelope, load_private_key, load_public_key

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SIGNET_URL = os.environ.get("SIGNET_URL", "http://localhost:4774")
ISSUER_PRIVATE_KEY_PATH = os.environ.get(
    "ISSUER_PRIVATE_KEY_PATH",
    os.path.join(os.path.dirname(__file__), '..', '..', 'iron-veil', 'deploy', 'config', 'trust', 'issuer_private.pem')
)
KAS_PUBLIC_KEY_PATH = os.environ.get(
    "KAS_PUBLIC_KEY_PATH",
    os.path.join(os.path.dirname(__file__), '..', '..', 'iron-veil', 'deploy', 'config', 'trust', 'kas_public.pem')
)

OPERATOR_CREDS = {
    "alice": ("alice", "alice"),
    "bob":   ("bob",   "bob"),
    "dave":  ("dave",  "dave"),
}

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def signet_available() -> bool:
    try:
        r = requests.get(f"{SIGNET_URL}/signet/health", timeout=2)
        return r.ok
    except Exception:
        return False


def get_jwt(username: str, password: str) -> str | None:
    try:
        r = requests.post(
            f"{SIGNET_URL}/signet/token",
            data={
                "grant_type": "password",
                "client_id": "signet-cli",
                "username": username,
                "password": password,
            },
            timeout=5,
        )
        if r.ok:
            return r.json().get("access_token")
    except Exception:
        pass
    return None


def make_test_frame(pos: LatLon, alt_m: float = 500, heading: float = 90) -> bytes:
    klv = encode_st0601_frame(lat=pos.lat, lon=pos.lon, alt_m=alt_m, heading=heading)
    return wrap_klv_in_ts(klv)


def ingest_frame(issuer_priv, kas_pub, pos: LatLon, session: requests.Session) -> dict:
    """Ingest a single frame at the given position. Returns the Signet result entry."""
    zone = classify_position(pos)
    labels = {
        "classification": zone.classification,
        "releasability": zone.releasability,
        "caveats": zone.caveats,
    }
    metadata = {
        "mission_id": "TEST-PIPELINE",
        "drone_id": "UAS-TEST",
        "frame_seq": 0,
        "sensor_ts": time.time(),
        "zone": zone.name,
        "lat": round(pos.lat, 6),
        "lon": round(pos.lon, 6),
        "alt_m": alt_m,
        "mission_time_s": 0,
    }
    payload = make_test_frame(pos)
    envelope = make_envelope(issuer_priv, kas_pub, labels, payload, metadata=metadata)
    r = session.post(f"{SIGNET_URL}/signet/bulk-ingest", json=[envelope], timeout=10)
    assert r.ok, f"bulk-ingest failed: {r.status_code} {r.text[:200]}"
    results = r.json()
    assert len(results) == 1
    return results[0]


# ---------------------------------------------------------------------------
# Skip marker
# ---------------------------------------------------------------------------

skip_no_signet = pytest.mark.skipif(
    not signet_available(),
    reason="Signet not reachable at SIGNET_URL — start Signet first"
)

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@skip_no_signet
def test_bulk_ingest_returns_object_id():
    """SIG-005: bulk-ingest returns per-envelope {object_id, ok}."""
    issuer_priv = load_private_key(ISSUER_PRIVATE_KEY_PATH)
    kas_pub = load_public_key(KAS_PUBLIC_KEY_PATH)
    session = requests.Session()
    result = ingest_frame(issuer_priv, kas_pub, CAN_BASE, session)
    assert result.get("ok") is True
    assert "object_id" in result


@skip_no_signet
def test_ingested_object_appears_in_list():
    """SIG-001: object appears in GET /signet/objects after ingest."""
    issuer_priv = load_private_key(ISSUER_PRIVATE_KEY_PATH)
    kas_pub = load_public_key(KAS_PUBLIC_KEY_PATH)
    session = requests.Session()
    before_ts = int(time.time() * 1000) - 1000
    result = ingest_frame(issuer_priv, kas_pub, CAN_BASE, session)
    object_id = result["object_id"]

    r = session.get(
        f"{SIGNET_URL}/signet/objects",
        params={"since": before_ts, "issuer": "drone-sim-dev"},
        timeout=5,
    )
    assert r.ok
    ids = [item["object_id"] for item in r.json()]
    assert object_id in ids, f"object_id {object_id} not found in /signet/objects"


@skip_no_signet
def test_metadata_stored_and_returned():
    """SIG-008: metadata JSONB round-trips through Signet objects list."""
    issuer_priv = load_private_key(ISSUER_PRIVATE_KEY_PATH)
    kas_pub = load_public_key(KAS_PUBLIC_KEY_PATH)
    session = requests.Session()
    before_ts = int(time.time() * 1000) - 1000
    result = ingest_frame(issuer_priv, kas_pub, CAN_BASE, session)
    object_id = result["object_id"]

    r = session.get(
        f"{SIGNET_URL}/signet/objects",
        params={"since": before_ts, "mission_id": "TEST-PIPELINE", "drone_id": "UAS-TEST"},
        timeout=5,
    )
    assert r.ok
    items = {item["object_id"]: item for item in r.json()}
    assert object_id in items
    meta = items[object_id].get("metadata", {})
    assert meta.get("mission_id") == "TEST-PIPELINE"
    assert meta.get("zone") == "CAN_BASE"


@skip_no_signet
def test_sse_notifies_after_ingest():
    """SIG-003: SSE stream pushes notification for a newly ingested object."""
    issuer_priv = load_private_key(ISSUER_PRIVATE_KEY_PATH)
    kas_pub = load_public_key(KAS_PUBLIC_KEY_PATH)
    session = requests.Session()
    received_ids = []

    # Open SSE stream in background thread
    def listen():
        try:
            with requests.get(
                f"{SIGNET_URL}/signet/stream/objects",
                stream=True, timeout=15,
            ) as resp:
                for line in resp.iter_lines():
                    if line.startswith(b"data:"):
                        payload = json.loads(line[5:].strip())
                        received_ids.append(payload.get("object_id"))
        except Exception:
            pass

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    time.sleep(0.5)  # let SSE connect

    result = ingest_frame(issuer_priv, kas_pub, CAN_BASE, session)
    object_id = result["object_id"]

    # Wait up to 5s for notification
    deadline = time.time() + 5
    while time.time() < deadline:
        if object_id in received_ids:
            break
        time.sleep(0.1)

    assert object_id in received_ids, "SSE did not deliver the ingested object_id within 5s"


# ---------------------------------------------------------------------------
# Operator policy tests — Alice / Bob / Dave
# ---------------------------------------------------------------------------

@skip_no_signet
def test_alice_unwraps_can_base():
    _assert_can_unwrap("alice", CAN_BASE, expect=True)


@skip_no_signet
def test_alice_unwraps_uk_base():
    _assert_can_unwrap("alice", UK_BASE, expect=True)


@skip_no_signet
def test_alice_unwraps_target():
    _assert_can_unwrap("alice", TARGET, expect=True)


@skip_no_signet
def test_alice_unwraps_transit():
    _assert_can_unwrap("alice", LatLon(52.0, 2.0), expect=True)


@skip_no_signet
def test_bob_unwraps_can_base():
    _assert_can_unwrap("bob", CAN_BASE, expect=True)


@skip_no_signet
def test_bob_cannot_unwrap_uk_base():
    _assert_can_unwrap("bob", UK_BASE, expect=False)


@skip_no_signet
def test_bob_cannot_unwrap_target():
    _assert_can_unwrap("bob", TARGET, expect=False)


@skip_no_signet
def test_bob_cannot_unwrap_transit():
    """Transit is PROTECTED/FVEY. Bob holds CAN only — releasability mismatch."""
    _assert_can_unwrap("bob", LatLon(52.0, 2.0), expect=False)


@skip_no_signet
def test_dave_cannot_unwrap_can_base():
    _assert_can_unwrap("dave", CAN_BASE, expect=False)


@skip_no_signet
def test_dave_cannot_unwrap_uk_base():
    _assert_can_unwrap("dave", UK_BASE, expect=False)


@skip_no_signet
def test_dave_cannot_unwrap_target():
    _assert_can_unwrap("dave", TARGET, expect=False)


@skip_no_signet
def test_dave_unwraps_transit():
    """Dave is PROTECTED/FVEY — can read PROTECTED/FVEY transit frames."""
    _assert_can_unwrap("dave", LatLon(52.0, 2.0), expect=True)


@skip_no_signet
def test_unwrap_response_includes_labels():
    """SIG-002: unwrap response must include labels:{cls, releasability, caveats}."""
    issuer_priv = load_private_key(ISSUER_PRIVATE_KEY_PATH)
    kas_pub = load_public_key(KAS_PUBLIC_KEY_PATH)
    session = requests.Session()
    result = ingest_frame(issuer_priv, kas_pub, CAN_BASE, session)
    object_id = result["object_id"]

    jwt = get_jwt("alice", "alice")
    headers = {"Authorization": f"Bearer {jwt}"} if jwt else {}
    r = requests.get(f"{SIGNET_URL}/signet/unwrap/{object_id}", headers=headers, timeout=5)
    assert r.ok
    body = r.json()
    assert "labels" in body, f"No 'labels' in unwrap response: {list(body.keys())}"
    labels = body["labels"]
    assert "cls" in labels or "classification" in labels
    assert "releasability" in labels


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _assert_can_unwrap(username: str, pos: LatLon, expect: bool):
    issuer_priv = load_private_key(ISSUER_PRIVATE_KEY_PATH)
    kas_pub = load_public_key(KAS_PUBLIC_KEY_PATH)
    session = requests.Session()

    result = ingest_frame(issuer_priv, kas_pub, pos, session)
    object_id = result["object_id"]
    zone = classify_position(pos)

    password = OPERATOR_CREDS[username][1]
    jwt = get_jwt(username, password)
    headers = {"Authorization": f"Bearer {jwt}"} if jwt else {}

    r = requests.get(f"{SIGNET_URL}/signet/unwrap/{object_id}", headers=headers, timeout=5)

    if expect:
        assert r.status_code == 200, (
            f"{username} should be able to unwrap {zone.name} "
            f"({zone.classification}/{zone.releasability}) but got {r.status_code}: {r.text[:200]}"
        )
    else:
        assert r.status_code == 403, (
            f"{username} should NOT be able to unwrap {zone.name} "
            f"({zone.classification}/{zone.releasability}) but got {r.status_code}"
        )
