# Iron-Veil Demo — Drone COP (Common Operating Picture)

A greenfield showcase project demonstrating **Data-Centric Security (DCS) on streaming drone telemetry**, built on top of [Iron-Veil](../iron-veil) and [Signet](../signet).

## Scenario

A combined FVEY military exercise with a drone flying a mission path: **Canadian base → UK base → target (20km out) → back to Canadian base**.

Three operators with different clearances and national affiliations watch the same Drone COP. Because every telemetry frame and every video segment is wrapped in an ACP-240 ZTDF envelope, each operator sees only what their clearance and nationality permits — **enforced cryptographically, not by server-side room permissions**.

### Operators

| Operator | Clearance | Nation | What they see |
|---|---|---|---|
| **Alice** | SECRET — all nations (FVEY) | USA | Full drone tracks + video for the entire mission |
| **Bob** | SECRET — CAN only | CAN | Drone tracks + video over Canadian base; **cut off** as drone enters UK base area |
| **Dave** | PROTECTED | CAN/FVEY | No combined-exercise area tracks; sees drone **only** after it exits the target area heading back to Canadian base |

### Mission Geofence Zones

| Zone | Classification | Releasability | Notes |
|---|---|---|---|
| **Canadian Base** (geofence) | SECRET | CAN, FVEY | Home base, takeoff/landing |
| **UK Base** (geofence) | SECRET | GBR, FVEY | UK sovereign area; CAN-only excluded |
| **Combined Exercise Corridor** | SECRET | FVEY | Airspace between bases |
| **Target Area** (20 km from bases) | SECRET | FVEY | Military target |
| **Transit / Outside zones** | PROTECTED | FVEY | Visible to PROTECTED+ |

---

## Standards Compliance

This project simulates drone telemetry conforming to **STANAG 4609** — the NATO standard for Full Motion Video (FMV) from Unmanned Air Systems (UAS). Metadata is encoded per **MISB ST 0601** (UAS Datalink Local Set) as KLV (Key-Length-Value) streams embedded in MPEG-TS.

Security labels are applied per **STANAG 4774 / STANAG 5636** and wrapped in ACP-240 ZTDF envelopes before ingestion into Signet.

---

## Quick Start

```bash
# 1. Start Signet first
cd ../signet/deploy && docker compose up -d --build

# 2. Start Iron-Veil
cd ../iron-veil/deploy && docker compose up -d --build

# 3. Start Iron-Veil Demo
cd deploy && docker compose up -d --build
```

See [docs/DEMO-STEPS.md](docs/DEMO-STEPS.md) for the full presentation walkthrough.

---

## Project Structure

```
iron-veil-demo/
├── docs/
│   ├── ARCHITECTURE.md         # System design and data flow
│   ├── SCENARIO.md             # Mission scenario, geofences, operator profiles
│   ├── DEMO-STEPS.md           # Step-by-step presentation guide
│   ├── STANAG-4609-NOTES.md    # STANAG 4609 / MISB ST0601 implementation notes
│   └── UPSTREAM-REQUIREMENTS.md # Required changes to iron-veil / signet teams
├── services/
│   ├── drone-sim/              # Drone telemetry + video simulator
│   │   ├── simulator.py        # MISB ST0601 KLV generator + ACP-240 wrapper
│   │   ├── mission.py          # Mission path, geofence zones, policy labels
│   │   ├── klv_encoder.py      # STANAG 4609 KLV packet builder
│   │   └── requirements.txt
│   └── cop-ui/                 # Browser-based Common Operating Picture
│       ├── index.html          # Map + video feed display
│       ├── app.js              # Signet unwrap + policy-aware rendering
│       └── package.json
├── deploy/
│   ├── docker-compose.yml
│   └── config/
└── README.md
```
