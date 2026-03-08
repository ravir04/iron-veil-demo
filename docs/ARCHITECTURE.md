# Iron-Veil Demo — Architecture

---

## Overview

Iron-Veil Demo adds a **Drone COP (Common Operating Picture)** layer on top of the existing Iron-Veil + Signet stack. It does not modify Iron-Veil or Signet. All DCS enforcement (wrap, ingest, unwrap, OPA policy evaluation) continues to flow through Signet at port 4774.

The demo introduces two new services:

| Service | Role |
|---|---|
| **drone-sim** | Synthetic STANAG 4609-conformant drone telemetry + video generator. Produces ACP-240 ZTDF envelopes and POSTs them to `POST /signet/ingest`. |
| **cop-ui** | Browser-based map + video feed. Calls `GET /signet/unwrap/{id}` with the operator's JWT. Renders only what Signet authorizes. |

---

## Full System Architecture

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  IRON-VEIL DEMO  —  Drone COP Layer                                         ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  PRODUCER                                                                    ║
║  ────────                                                                    ║
║  ┌────────────────────────────────────────────────────────────────┐         ║
║  │  drone-sim  (Python)                                           │         ║
║  │                                                                │         ║
║  │  1. Generates synthetic mission path (lat/lon over time)       │         ║
║  │  2. Determines active geofence zone → classification labels    │         ║
║  │  3. Encodes MISB ST0601 KLV telemetry frame                    │         ║
║  │  4. Wraps KLV frame + video segment in ACP-240 ZTDF envelope   │         ║
║  │     (using same make_envelope() pattern as iron-veil simulator)│         ║
║  │  5. POST /signet/ingest  (one envelope per ~1-second interval) │         ║
║  └────────────────────────────────────────────────────────────────┘         ║
║           │                                                                  ║
║           │  ACP-240 ZTDF envelopes (one per telemetry frame)               ║
║           ▼                                                                  ║
║  ┌─────────────────────────────────────────────────────────────────────┐    ║
║  │  Signet  (port 4774)  — unchanged, no modifications                 │    ║
║  │                                                                     │    ║
║  │  • Validates issuer trust + RSA-PSS signature                       │    ║
║  │  • Evaluates node ingest OPA policy                                 │    ║
║  │  • Stores envelope (ciphertext + metadata)                          │    ║
║  │  • Returns object_id                                                │    ║
║  │                                                                     │    ║
║  │  On unwrap request:                                                 │    ║
║  │  • Validates operator JWT                                           │    ║
║  │  • Evaluates subject OPA policy (clearance + releasability)         │    ║
║  │  • Returns plaintext KLV frame OR structured denial                 │    ║
║  └─────────────────────────────────────────────────────────────────────┘    ║
║           │                                                                  ║
║           │  GET /signet/unwrap/{id}  (per operator JWT)                    ║
║           ▼                                                                  ║
║  ┌────────────────────────────────────────────────────────────────┐         ║
║  │  cop-ui  (Browser SPA)                                         │         ║
║  │                                                                │         ║
║  │  • Operator logs in → gets JWT from Signet/Keycloak            │         ║
║  │  • Polls /signet/catalog for new object_ids                    │         ║
║  │  • For each object_id: GET /signet/unwrap/{id}                 │         ║
║  │    ├─ 200 plaintext KLV → decode → render on map               │         ║
║  │    └─ 403 deny → display [REDACTED] or hide track point        │         ║
║  │  • Video segments: rendered only if unwrap succeeds            │         ║
║  │  • Map shows drone track dots, coloured by zone                 │         ║
║  └────────────────────────────────────────────────────────────────┘         ║
║                                                                              ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  EXISTING IRON-VEIL STACK  (unchanged)                                      ║
║  Matrix-Proxy :8009 │ Catalog-API :8082 │ OPA :8181 │ Synapse (internal)   ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  EXISTING SIGNET STACK  (unchanged)                                         ║
║  Signet :4774 │ Signet-Admin :4775 │ Keycloak (identity) │ OPA │ KAS       ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

## Data Flow: Drone Telemetry Frame

```
drone-sim (every ~1 second)
  │
  ├─ Compute current lat/lon on mission path
  ├─ Determine geofence zone → labels {classification, releasability}
  ├─ Encode MISB ST0601 KLV packet:
  │    Tag 2:  Unix time stamp (microseconds)
  │    Tag 13: Sensor Lat
  │    Tag 14: Sensor Lon
  │    Tag 15: Sensor Alt
  │    Tag 5:  Platform Heading
  │    Tag 6:  Platform Pitch
  │    Tag 7:  Platform Roll
  │    Tag 17: Frame Center Lat
  │    Tag 18: Frame Center Lon
  │    Tag 56: Platform Ground Speed
  │    Tag 65: UAS Datalink LS Version Number
  │    Tag 1:  Checksum
  ├─ Pack KLV into MPEG-TS packet (PID 0x0065 per STANAG 4609)
  ├─ Build ACP-240 ZTDF envelope:
  │    plaintext  = KLV-in-MPEG-TS bytes
  │    labels     = zone labels
  │    issuer     = "drone-sim-dev"
  ├─ POST /signet/ingest  →  {object_id, manifest_hash}
  └─ Store object_id in local ring buffer (for COP-UI polling)
```

---

## Policy Labelling by Zone

The drone-sim applies STANAG 5636 OCL labels based on the current geofence zone:

| Zone | Classification | Releasability (catl type P) | Who can unwrap |
|---|---|---|---|
| Canadian Base | SECRET | CAN, FVEY | Alice (FVEY), Bob (CAN), NOT Dave |
| Combined Exercise Corridor | SECRET | FVEY | Alice (FVEY), NOT Bob (CAN-only), NOT Dave |
| UK Base | SECRET | GBR, FVEY | Alice (FVEY), NOT Bob (CAN-only), NOT Dave |
| Target Area | SECRET | FVEY | Alice (FVEY), NOT Bob, NOT Dave |
| Outside Zones (transit) | PROTECTED | FVEY | Alice, Dave (PROTECTED+FVEY), NOT Bob |

> **Bob** holds `rels: [CAN]` — he satisfies `{CAN, FVEY}` (OR logic) so he can see Canadian Base frames.
> He does NOT satisfy `{GBR, FVEY}` or `{FVEY}` alone (because his rels list only contains CAN), so he is cut off as soon as the drone enters the exercise corridor.

> **Dave** holds `clearance: PROTECTED` — he cannot decrypt any SECRET frame. He only sees PROTECTED frames (transit/outside zones on the return leg).

---

## Key Design Decisions

1. **No changes to Signet or Iron-Veil.** All labelling, wrapping, and policy enforcement uses existing APIs.
2. **One envelope per telemetry frame.** Each ~1-second KLV packet is a separate ZTDF object. This allows per-frame policy granularity matching the drone's position.
3. **Synthetic video.** No real drone hardware required. Video frames are generated as coloured JPEG tiles with telemetry overlay text, assembled into a simulated MPEG-TS stream. The video payload is embedded in the same envelope as the KLV telemetry.
4. **COP-UI is purely a display layer.** It holds no plaintext. Every frame is fetched from Signet with a policy check. The browser never receives data it shouldn't see.
5. **STANAG 4609 conformance is structural.** The KLV encoding follows MISB ST0601 tag layout and MPEG-TS PID conventions, making the output compatible with real FMV tools (e.g. QGIS FMV, VLC with KLV plugin) for inspection.

---

## Port Map

| Service | Port | Role |
|---|---|---|
| Signet | 4774 | ACP-240 DCS enforcement (external dependency) |
| Signet-Admin | 4775 | Admin console |
| drone-sim | — (internal) | Telemetry producer, no exposed port |
| cop-ui | **8090** | Browser-based COP display |
| Iron-Veil matrix-proxy | 8009 | Matrix chat (existing demo) |
| Catalog-API | 8082 | Object manifest (existing demo) |
