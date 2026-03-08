# Iron-Veil Demo — Scenario Reference

## Setting

A combined FVEY military exercise (FVEX-26). A surveillance drone operates from a Canadian forward operating base, flies over a UK base, proceeds to a military target 20 km from the bases, then returns to the Canadian base. The entire mission is streamed as Full Motion Video with embedded MISB ST0601 telemetry.

Three operators in a joint coalition operations centre watch the same **Drone COP** — a live map showing the drone track and a video feed. Because data is protected by ACP-240 DCS, each operator's COP shows only what their clearance and nationality permit. The server cannot be reconfigured to grant more access — enforcement is cryptographic.

---

## Mission Path

```
[Canadian Base]  →  [Combined Exercise Corridor]  →  [UK Base]  →  [Target]  →  return
     takeoff              cruise altitude                 overfly      loiter     RTB
```

Approximate coordinates (synthetic, for demo):

| Waypoint | Lat | Lon | Notes |
|---|---|---|---|
| Canadian Base | 51.2500° N | 000.5000° W | Takeoff / landing |
| Exercise Corridor Entry | 51.2600° N | 000.3500° W | CAN→FVEY boundary |
| UK Base | 51.2700° N | 000.2000° W | UK sovereign geofence |
| Exercise Corridor Exit | 51.2800° N | 000.0500° W | UK→target boundary |
| Target | 51.3000° N | 000.2500° E | 20 km from UK base |
| Return path mirrors outbound | — | — | Drone reverses course |

---

## Geofence Zones and Labels

Each zone is defined by a bounding polygon in `services/drone-sim/mission.py`. The simulator evaluates the drone's current position against zone polygons in priority order.

| Zone | Shape | Classification | Releasability | Rationale |
|---|---|---|---|---|
| `CAN_BASE` | Circle r=1km around Canadian Base | SECRET | CAN, FVEY | Canadian sovereign military area |
| `UK_BASE` | Circle r=1km around UK Base | SECRET | GBR, FVEY | UK sovereign military area |
| `EXERCISE_CORRIDOR` | Rectangle connecting base areas | SECRET | FVEY | Joint exercise airspace |
| `TARGET_AREA` | Circle r=2km around target | SECRET | FVEY | Active military target |
| `TRANSIT` | Everything else | PROTECTED | FVEY | Outside controlled airspace |

---

## Operator Profiles

### Alice — Full Access

| Attribute | Value |
|---|---|
| Identity | `alice` (Keycloak subject) |
| Clearance | SECRET |
| Releasability | CAN, FVEY, NATO, AUS, NZL, GBR, USA |
| Caveats | none |
| Client | Element (existing iron-veil demo) / COP-UI |

**What Alice sees:**
- Full drone track for the entire mission
- Video feed for all zones (CAN base, exercise corridor, UK base, target, transit)
- All telemetry: position, heading, sensor data, timestamps

### Bob — Canada Only

| Attribute | Value |
|---|---|
| Identity | `bob` (Keycloak subject) |
| Clearance | SECRET |
| Releasability | CAN |
| Caveats | none |
| Client | Nheko (existing iron-veil demo) / COP-UI |

**What Bob sees:**
- Drone track over `CAN_BASE` — **visible** (CAN satisfies `{CAN, FVEY}`)
- Drone track over `EXERCISE_CORRIDOR` — **cut off** (CAN does not satisfy `{FVEY}`)
- Drone track over `UK_BASE` — **cut off** (CAN does not satisfy `{GBR, FVEY}`)
- Drone track over `TARGET_AREA` — **cut off**
- Transit / return frames — **cut off** (SECRET clearance, but PROTECTED frames: Bob can't see PROTECTED either — wait, Bob IS SECRET, so he CAN see PROTECTED frames)

> Note: PROTECTED frames use releasability FVEY. Bob has `rels: [CAN]`. The PROTECTED frame requires at least one of `{FVEY}`. CAN is not in `{FVEY}`. Bob is cut off from transit frames too. He effectively loses the drone entirely once it leaves the Canadian base perimeter.

**Demo moment:** The map shows the drone track going dark — last position dot visible is at the CAN_BASE geofence boundary.

### Dave — Minimal Access

| Attribute | Value |
|---|---|
| Identity | `dave` (Keycloak subject) |
| Clearance | PROTECTED |
| Releasability | CAN, FVEY |
| Caveats | none |
| Client | Cinny (existing iron-veil demo) / COP-UI |

**What Dave sees:**
- Drone track over `CAN_BASE` — **cut off** (PROTECTED clearance, zone is SECRET)
- Drone track over exercise areas — **cut off** (SECRET)
- Drone track over `UK_BASE` — **cut off** (SECRET)
- Drone track over `TARGET_AREA` — **cut off** (SECRET)
- Drone track in `TRANSIT` on return leg — **visible** (PROTECTED, FVEY — Dave qualifies)

**Demo moment:** Dave's map is empty for most of the mission. The drone suddenly appears as it exits the target area and enters transit airspace on the return leg. Dave sees only the final portion of the track.

---

## Mission Timeline (approximate)

| Time (T+s) | Position | Zone | Alice | Bob | Dave |
|---|---|---|---|---|---|
| T+0 | Takeoff, Canadian Base | CAN_BASE | Track + Video | Track + Video | Nothing |
| T+30 | Departing CAN base | CAN_BASE → EXERCISE | Track + Video | **Last frame** | Nothing |
| T+60 | Over exercise corridor | EXERCISE_CORRIDOR | Track + Video | Nothing | Nothing |
| T+90 | Approaching UK base | EXERCISE_CORRIDOR | Track + Video | Nothing | Nothing |
| T+120 | Over UK base | UK_BASE | Track + Video | Nothing | Nothing |
| T+150 | En route to target | EXERCISE_CORRIDOR | Track + Video | Nothing | Nothing |
| T+180 | Over target | TARGET_AREA | Track + Video | Nothing | Nothing |
| T+210 | Target loiter | TARGET_AREA | Track + Video | Nothing | Nothing |
| T+240 | Exiting target area | TARGET_AREA → TRANSIT | Track + Video | Nothing | **First frame** |
| T+270 | Return transit | TRANSIT | Track + Video | Nothing | Track + Video |
| T+300 | Approaching CAN base | TRANSIT → CAN_BASE | Track + Video | Track + Video | Nothing |
| T+330 | Landing | CAN_BASE | Track + Video | Track + Video | Nothing |

---

## Demonstration Narrative

> "The same drone. The same server. Three operators. Each sees a different picture — not because we configured three different rooms, but because every telemetry frame carries its own cryptographic policy. The server cannot cheat."

**Step 1:** Show all three COP views side by side. Drone takes off from Canadian base. Alice and Bob both see the track. Dave sees nothing.

**Step 2:** Drone crosses into the exercise corridor. Bob's track freezes. Alice continues. Dave still nothing. Point to the moment on Bob's map — the track stops at the geofence boundary.

**Step 3:** Drone flies over UK base and to target. Only Alice sees this leg. Emphasize: Bob and Dave can't even see the track dots — not blurred, not hidden by UI — the data never reaches them because Signet denied the key.

**Step 4:** Drone exits target area into transit airspace. Dave's map suddenly shows the drone. Point to the policy label change from SECRET to PROTECTED at the geofence boundary.

**Step 5:** Drone approaches Canadian base. Bob's track reappears. Alice, Bob, and Dave each see different portions of the same mission.

**Step 6:** Open Signet Admin audit log. Show the stream of ALLOW and DENY decisions with reasons: `releasability_mismatch`, `clearance_insufficient`, `policy_allow`.
