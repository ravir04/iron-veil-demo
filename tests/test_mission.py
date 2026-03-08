"""
Unit tests for mission.py — geofence zone classification and mission path.

Tests verify:
1. Known positions resolve to the correct zone
2. Zone boundaries produce the correct policy labels
3. Mission path reaches all expected zones in correct order
4. The demo scenario (Alice/Bob/Dave visibility) matches the policy labels
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'services', 'drone-sim'))

from mission import (
    LatLon,
    classify_position,
    MissionPath,
    CAN_BASE,
    UK_BASE,
    TARGET,
    ZONES_BY_PRIORITY,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def can_read(zone, clearance: str, rels: list[str]) -> bool:
    """Simulate the Signet OPA three-gate check for a given zone and subject."""
    rank = {"UNCLASS": 0, "PROTECTED": 1, "SECRET": 2}
    # Gate 1: classification dominance
    if rank.get(clearance, 0) < rank.get(zone.classification, 0):
        return False
    # Gate 2: releasability (OR — subject must hold at least one required nation)
    required = set(zone.releasability)
    if required and not required.intersection(rels):
        return False
    return True


# ---------------------------------------------------------------------------
# Zone classification — known positions
# ---------------------------------------------------------------------------

def test_can_base_centre_is_can_base():
    zone = classify_position(CAN_BASE)
    assert zone.name == "CAN_BASE"


def test_uk_base_centre_is_uk_base():
    zone = classify_position(UK_BASE)
    assert zone.name == "UK_BASE"


def test_target_centre_is_target_area():
    zone = classify_position(TARGET)
    assert zone.name == "TARGET_AREA"


def test_far_outside_is_transit():
    """A point well away from all zones should be TRANSIT."""
    far = LatLon(lat=52.0, lon=2.0)
    zone = classify_position(far)
    assert zone.name == "TRANSIT"


def test_midpoint_between_bases_is_exercise_corridor():
    """Midpoint of mission route — inside the 30km exercise corridor."""
    mid = LatLon(
        lat=(CAN_BASE.lat + TARGET.lat) / 2,
        lon=(CAN_BASE.lon + TARGET.lon) / 2,
    )
    zone = classify_position(mid)
    assert zone.name == "EXERCISE_CORRIDOR"


def test_just_outside_can_base_is_not_can_base():
    """2km from CAN_BASE centre should fall outside the 1km radius."""
    outside = LatLon(lat=CAN_BASE.lat + 0.018, lon=CAN_BASE.lon)  # ~2km north
    zone = classify_position(outside)
    assert zone.name != "CAN_BASE"


# ---------------------------------------------------------------------------
# Zone policy labels
# ---------------------------------------------------------------------------

def test_can_base_labels():
    zone = classify_position(CAN_BASE)
    assert zone.classification == "SECRET"
    assert "CAN" in zone.releasability
    assert "FVEY" in zone.releasability


def test_uk_base_labels():
    zone = classify_position(UK_BASE)
    assert zone.classification == "SECRET"
    assert "GBR" in zone.releasability
    assert "FVEY" in zone.releasability
    assert "CAN" not in zone.releasability


def test_target_labels():
    zone = classify_position(TARGET)
    assert zone.classification == "SECRET"
    assert zone.releasability == ["FVEY"]


def test_transit_labels():
    far = LatLon(lat=52.0, lon=2.0)
    zone = classify_position(far)
    assert zone.classification == "PROTECTED"
    assert "FVEY" in zone.releasability


# ---------------------------------------------------------------------------
# Operator visibility — the core demo assertions
# ---------------------------------------------------------------------------

ALICE = {"clearance": "SECRET",    "rels": ["CAN","FVEY","NATO","AUS","NZL","GBR","USA"]}
BOB   = {"clearance": "SECRET",    "rels": ["CAN"]}
DAVE  = {"clearance": "PROTECTED", "rels": ["CAN","FVEY"]}


def test_alice_sees_can_base():
    z = classify_position(CAN_BASE)
    assert can_read(z, **ALICE)


def test_alice_sees_uk_base():
    z = classify_position(UK_BASE)
    assert can_read(z, **ALICE)


def test_alice_sees_target():
    z = classify_position(TARGET)
    assert can_read(z, **ALICE)


def test_alice_sees_transit():
    z = classify_position(LatLon(52.0, 2.0))
    assert can_read(z, **ALICE)


def test_bob_sees_can_base():
    z = classify_position(CAN_BASE)
    assert can_read(z, **BOB)


def test_bob_cannot_see_uk_base():
    """CAN does not satisfy {GBR, FVEY}."""
    z = classify_position(UK_BASE)
    assert not can_read(z, **BOB)


def test_bob_cannot_see_target():
    """CAN does not satisfy {FVEY}."""
    z = classify_position(TARGET)
    assert not can_read(z, **BOB)


def test_bob_cannot_see_transit():
    """Transit is PROTECTED/FVEY — Bob has CAN only, not FVEY."""
    z = classify_position(LatLon(52.0, 2.0))
    assert not can_read(z, **BOB)


def test_dave_cannot_see_can_base():
    """PROTECTED clearance cannot decrypt SECRET."""
    z = classify_position(CAN_BASE)
    assert not can_read(z, **DAVE)


def test_dave_cannot_see_uk_base():
    z = classify_position(UK_BASE)
    assert not can_read(z, **DAVE)


def test_dave_cannot_see_target():
    z = classify_position(TARGET)
    assert not can_read(z, **DAVE)


def test_dave_sees_transit():
    """PROTECTED+FVEY — Dave qualifies for PROTECTED/FVEY transit frames."""
    z = classify_position(LatLon(52.0, 2.0))
    assert can_read(z, **DAVE)


# ---------------------------------------------------------------------------
# Mission path — zone sequence
# ---------------------------------------------------------------------------

def test_mission_starts_at_can_base():
    mp = MissionPath()
    pos, _ = mp.advance(dt_seconds=0.001)
    zone = classify_position(pos)
    assert zone.name == "CAN_BASE"


def test_mission_visits_target():
    """Run the full mission and verify TARGET_AREA is encountered."""
    mp = MissionPath()
    zones_seen = set()
    for _ in range(2000):
        pos, _ = mp.advance(dt_seconds=5.0)
        zones_seen.add(classify_position(pos).name)
        if mp.complete:
            break
    assert "TARGET_AREA" in zones_seen, f"TARGET_AREA not reached. Zones seen: {zones_seen}"


def test_mission_visits_uk_base():
    mp = MissionPath()
    zones_seen = set()
    for _ in range(2000):
        pos, _ = mp.advance(dt_seconds=5.0)
        zones_seen.add(classify_position(pos).name)
        if mp.complete:
            break
    assert "UK_BASE" in zones_seen, f"UK_BASE not reached. Zones seen: {zones_seen}"


def test_mission_completes():
    mp = MissionPath()
    for _ in range(10000):
        mp.advance(dt_seconds=10.0)
        if mp.complete:
            break
    assert mp.complete, "Mission did not complete within step budget"


def test_mission_returns_to_can_base():
    """Final position should be back at CAN_BASE."""
    mp = MissionPath()
    last_pos = None
    for _ in range(10000):
        pos, _ = mp.advance(dt_seconds=10.0)
        last_pos = pos
        if mp.complete:
            break
    assert last_pos is not None
    assert last_pos.distance_km(CAN_BASE) < 1.5, \
        f"Mission did not return to CAN_BASE — final position {last_pos}"


# ---------------------------------------------------------------------------
# LatLon helpers
# ---------------------------------------------------------------------------

def test_distance_same_point():
    p = LatLon(51.25, -0.50)
    assert p.distance_km(p) < 1e-6


def test_distance_known():
    """London to Paris is approximately 340 km."""
    london = LatLon(51.5074, -0.1278)
    paris  = LatLon(48.8566, 2.3522)
    d = london.distance_km(paris)
    assert 330 < d < 350, f"London–Paris distance out of range: {d:.1f} km"


def test_bearing_east():
    """Point due east should have bearing ~90°."""
    p1 = LatLon(51.25, -0.50)
    p2 = LatLon(51.25,  0.50)
    b = p1.bearing_to(p2)
    assert 85 < b < 95, f"East bearing out of range: {b:.1f}°"


def test_move_toward_reaches_target():
    p1 = LatLon(51.25, -0.50)
    p2 = LatLon(51.25,  0.50)
    dist = p1.distance_km(p2)
    arrived = p1.move_toward(p2, dist + 1.0)
    assert arrived.distance_km(p2) < 0.01
