"""
Mission path definition, geofence zones, and policy labelling.

The mission models a drone flying:
  Canadian Base → Exercise Corridor → UK Base → Target Area → return

All coordinates are synthetic and placed over a generic terrain for demo purposes.
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Coordinate types
# ---------------------------------------------------------------------------

@dataclass
class LatLon:
    lat: float  # degrees, -90 to 90
    lon: float  # degrees, -180 to 180

    def distance_km(self, other: "LatLon") -> float:
        """Haversine distance in km."""
        R = 6371.0
        lat1, lon1 = math.radians(self.lat), math.radians(self.lon)
        lat2, lon2 = math.radians(other.lat), math.radians(other.lon)
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return R * 2 * math.asin(math.sqrt(a))

    def bearing_to(self, other: "LatLon") -> float:
        """True bearing from self to other, degrees 0–360."""
        lat1 = math.radians(self.lat)
        lat2 = math.radians(other.lat)
        dlon = math.radians(other.lon - self.lon)
        x = math.sin(dlon) * math.cos(lat2)
        y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
        bearing = math.degrees(math.atan2(x, y))
        return (bearing + 360) % 360

    def move_toward(self, target: "LatLon", km: float) -> "LatLon":
        """Return a point km along the great-circle path toward target."""
        d_total = self.distance_km(target)
        if d_total < 1e-6:
            return LatLon(target.lat, target.lon)
        frac = min(km / d_total, 1.0)
        lat = self.lat + frac * (target.lat - self.lat)
        lon = self.lon + frac * (target.lon - self.lon)
        return LatLon(lat, lon)


# ---------------------------------------------------------------------------
# Geofence zones
# ---------------------------------------------------------------------------

@dataclass
class CircleZone:
    name: str
    centre: LatLon
    radius_km: float
    classification: str          # UNCLASS | PROTECTED | SECRET
    releasability: list[str]     # e.g. ["CAN", "FVEY"]
    caveats: list[str] = field(default_factory=list)
    priority: int = 0            # Higher = checked first when zones overlap

    def contains(self, pos: LatLon) -> bool:
        return self.centre.distance_km(pos) <= self.radius_km


# Synthetic mission waypoints
CAN_BASE    = LatLon(lat=51.2500, lon=-0.5000)
UK_BASE     = LatLon(lat=51.2700, lon=-0.2000)
TARGET      = LatLon(lat=51.3000, lon=+0.2500)  # ~20 km east of UK base

# Return-leg TRANSIT waypoint — swings north of the exercise corridor so the
# drone passes through uncontrolled airspace. This is where Dave first sees the
# drone and where the "PROTECTED" policy label applies.
TRANSIT_RTB = LatLon(lat=51.3800, lon=-0.1500)  # north of the exercise area

# Geofence definitions — checked in priority order (highest first)
#
# EXERCISE_CORRIDOR is a tight 5km band around the direct CAN_BASE→TARGET line.
# The outbound leg flies through it; the return leg swings north via TRANSIT_RTB,
# which lies outside the corridor — producing TRANSIT (PROTECTED) frames for Dave.
ZONES: list[CircleZone] = [
    CircleZone(
        name="CAN_BASE",
        centre=CAN_BASE,
        radius_km=1.0,
        classification="SECRET",
        releasability=["CAN", "FVEY"],
        priority=10,
    ),
    CircleZone(
        name="UK_BASE",
        centre=UK_BASE,
        radius_km=1.0,
        classification="SECRET",
        releasability=["GBR", "FVEY"],
        priority=10,
    ),
    CircleZone(
        name="TARGET_AREA",
        centre=TARGET,
        radius_km=2.0,
        classification="SECRET",
        releasability=["FVEY"],
        priority=9,
    ),
    CircleZone(
        name="EXERCISE_CORRIDOR",
        centre=LatLon(
            lat=(CAN_BASE.lat + TARGET.lat) / 2,
            lon=(CAN_BASE.lon + TARGET.lon) / 2,
        ),
        radius_km=5.0,   # tight band — outbound leg only; return leg swings north
        classification="SECRET",
        releasability=["FVEY"],
        priority=5,
    ),
    # Default/fallback — outside all controlled zones (PROTECTED)
    # This is what Dave sees on the return leg via TRANSIT_RTB
    CircleZone(
        name="TRANSIT",
        centre=LatLon(lat=0.0, lon=0.0),
        radius_km=1e9,
        classification="PROTECTED",
        releasability=["FVEY"],
        priority=0,
    ),
]

ZONES_BY_PRIORITY = sorted(ZONES, key=lambda z: z.priority, reverse=True)


def classify_position(pos: LatLon) -> CircleZone:
    """Return the highest-priority zone that contains this position."""
    for zone in ZONES_BY_PRIORITY:
        if zone.contains(pos):
            return zone
    # Should never reach here (TRANSIT always matches)
    return ZONES_BY_PRIORITY[-1]


# ---------------------------------------------------------------------------
# Mission path
# ---------------------------------------------------------------------------

@dataclass
class MissionWaypoint:
    position: LatLon
    label: str
    altitude_m: float = 500.0
    speed_ms: float = 50.0   # m/s (~180 km/h)


# Standard mission:
#   Outbound — CAN_BASE → UK_BASE → TARGET  (through EXERCISE_CORRIDOR)
#   Return   — TARGET → TRANSIT_RTB → CAN_BASE  (swings north, through TRANSIT)
#
# The northern swing is the key demo moment:
#   - Dave's screen shows the drone for the first time as it enters TRANSIT
#   - Bob's screen stays dark (TRANSIT is FVEY-only, Bob holds CAN)
#   - Alice sees the entire track throughout
STANDARD_MISSION: list[MissionWaypoint] = [
    MissionWaypoint(CAN_BASE,    "TAKEOFF",     altitude_m=0.0,   speed_ms=0.0),
    # Demo-speed: 366 m/s gives ~300s total mission (5 min at 1×, ~75s at 4×).
    # Total cruise distance ≈ 109.9 km; 109900 / 300 ≈ 366 m/s.
    # The speed is synthetic — actual KLV metadata carries it but it's not displayed.
    # Zone transitions at: T+4s (exit CAN), T+56s (UK_BASE), T+62s (CORRIDOR),
    #   T+87s (TRANSIT), T+139s (TARGET), T+152s (Dave's first frame), T+300s (RTB CAN)
    MissionWaypoint(CAN_BASE,    "CLIMB",       altitude_m=500.0, speed_ms=366.0),
    MissionWaypoint(UK_BASE,     "CRUISE",      altitude_m=500.0, speed_ms=366.0),
    MissionWaypoint(TARGET,      "APPROACH",    altitude_m=300.0, speed_ms=366.0),
    MissionWaypoint(TARGET,      "LOITER_1",    altitude_m=300.0, speed_ms=100.0),
    MissionWaypoint(TARGET,      "LOITER_2",    altitude_m=300.0, speed_ms=100.0),
    # Return leg swings north through TRANSIT airspace — Dave's first visibility
    MissionWaypoint(TRANSIT_RTB, "RTB_TRANSIT", altitude_m=500.0, speed_ms=366.0),
    MissionWaypoint(CAN_BASE,    "RTB_FINAL",   altitude_m=500.0, speed_ms=366.0),
    MissionWaypoint(CAN_BASE,    "LANDING",     altitude_m=0.0,   speed_ms=0.0),
]


class MissionPath:
    """
    Interpolates drone position along the STANDARD_MISSION waypoints
    at a given speed, yielding one LatLon per call to advance(dt_seconds).
    """

    def __init__(self, mission: list[MissionWaypoint] = STANDARD_MISSION):
        self._waypoints = mission
        self._seg = 0          # current segment index
        self._pos = LatLon(mission[0].position.lat, mission[0].position.lon)
        self._complete = False

    @property
    def complete(self) -> bool:
        return self._complete

    @property
    def current_waypoint(self) -> MissionWaypoint:
        return self._waypoints[min(self._seg, len(self._waypoints) - 1)]

    def advance(self, dt_seconds: float = 1.0) -> tuple[LatLon, MissionWaypoint]:
        """
        Advance the drone by dt_seconds at the current waypoint speed.
        Returns (current_position, current_waypoint).
        """
        if self._complete:
            return self._pos, self.current_waypoint

        wp = self._waypoints[self._seg]
        target = wp.position
        dist_km = wp.speed_ms * dt_seconds / 1000.0

        dist_to_target = self._pos.distance_km(target)
        if dist_to_target <= dist_km or dist_to_target < 0.01:
            # Arrived at this waypoint — advance to next
            self._pos = LatLon(target.lat, target.lon)
            self._seg += 1
            if self._seg >= len(self._waypoints):
                self._complete = True
                self._seg = len(self._waypoints) - 1
        else:
            self._pos = self._pos.move_toward(target, dist_km)

        return self._pos, wp

    def heading(self) -> float:
        """Current heading toward next waypoint, degrees 0–360."""
        if self._complete or self._seg >= len(self._waypoints) - 1:
            return 0.0
        return self._pos.bearing_to(self._waypoints[self._seg].position)
