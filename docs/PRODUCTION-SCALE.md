# Production FMV Streaming — Scale Architecture

**Document:** Iron-Veil Demo — Production Scale Reference
**Date:** 2026-03-08
**Status:** REFERENCE (not yet implemented in demo)

---

## 1. Purpose

This document bridges the Iron-Veil Demo (1 drone, 1 frame/s, SSE-driven telemetry) to the production FMV streaming architecture (multiple drones, 25 fps video, GOP-gated push stream, KLV telemetry extraction). It provides throughput numbers, explains the per-GOP wrapping model, and specifies what changes are needed in drone-sim for higher-throughput testing.

---

## 2. The Core Shift: Per-Frame → Per-GOP Wrapping

### 2.1 Why not wrap every video frame

At 25 fps, one ACP-240 ZTDF envelope per video frame means:

- **25 RSA operations/s per drone.** Each `make_envelope()` call performs: RSA-OAEP-256 DEK wrap (KAS public key), RSA-PSS signature (issuer private key), AES-256-GCM encrypt, HMAC-SHA256 policy binding. The RSA operations dominate at ~5–15 ms each on a modern CPU core. At 25/s that's 250–750 ms/s of RSA on a single core — saturates one core per drone.

- **25 SSE events/s per drone per connected operator.** At 3 operators × 5 drones = 375 events/s on the SIG-003 notification channel. Each event triggers a map update, a telemetry log entry, and policy evaluation in every browser. The browser event loop saturates before the network does.

- **90,000 Signet objects/hour per drone.** A 6-hour mission with 10 drones = 5.4 million rows in Signet's objects table. Time-range queries (`/signet/objects?since=...`) become expensive without careful indexing, and the backfill on tab open fetches an impractical number of records.

### 2.2 Why the GOP is the right boundary

A GOP (Group of Pictures) is the smallest self-contained decode unit in H.264 — a closed GOP can be decrypted and rendered independently. In the demo, `SEGMENT_DURATION_S=2.0` with `keyint=25` at 25 fps = 1 GOP/second (each segment contains 2 GOPs). The production configuration aligns `FRAME_INTERVAL = SEGMENT_DURATION_S = GOP duration`.

**Policy changes are zone-event-driven, not frame-driven.** At a real ISR drone speed of 50–150 m/s, a 1 km zone has a minimum dwell time of ~7–20 seconds. A GOP interval of 1–2 seconds provides 4–20 policy evaluations per zone — more than sufficient. There is no operational benefit to evaluating policy 25× per second when zones change at most once per 7 seconds.

**One envelope per GOP therefore gives:**
- 1 RSA wrap + 1 RSA sign per GOP (same rate as the demo at 1 frame/s if GOP = 1 s)
- 1 Signet object per GOP (3,600/hour per drone — same as current demo)
- 0–1 zone_transition SSE events per zone crossing regardless of frame rate

### 2.3 Multi-drone zone transitions

Each `zone_transition` SSE event (STR-005) carries a `drone_id` field. Signet's SIG-003 SSE is a single connection that delivers events for **all** objects the operator's JWT authorises. Multiple drones therefore share one SSE connection, with each event explicitly tagged to its drone. The COP-UI dispatches by `drone_id` to per-drone state (track points, clearance cards, video elements).

This architecture is independent of frame rate — adding drones does not require additional SSE connections, only additional event handlers keyed by `drone_id`.

---

## 3. Three-Channel Production Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  drone-sim  (per drone)                                                     │
│  • Advance mission path by SEGMENT_DURATION_S seconds                      │
│  • Determine zone → ACP-240 labels                                          │
│  • Encode KLV + H.264 GOP into MPEG-TS segment                             │
│  • Wrap in ZTDF envelope → POST /signet/bulk-ingest                         │
└───────────────────────────────┬─────────────────────────────────────────────┘
                                │  1 ZTDF envelope per GOP (1–2 s)
                                ▼
                        ┌───────────────┐
                        │    Signet     │
                        │               │
                        │  • Validate   │
                        │  • OPA ingest │
                        │  • Store GOP  │
                        │  • Notify SSE │
                        └───┬───────────┘
                            │
          ┌─────────────────┼──────────────────────┐
          │                 │                       │
          ▼                 ▼                       ▼
   ┌─────────────┐  ┌──────────────────┐  ┌────────────────────┐
   │  SIG-010    │  │  STR-003         │  │  SIG-003           │
   │  Push video │  │  klv_telemetry   │  │  zone_transition   │
   │  MPEG-TS    │  │  SSE events      │  │  SSE events        │
   │  per-GOP    │  │  (lat/lon/alt)   │  │  (policy changes)  │
   │  gated by   │  │  at GOP rate     │  │  only on zone cross│
   │  OPA        │  │                  │  │                    │
   └──────┬──────┘  └────────┬─────────┘  └─────────┬──────────┘
          │                  │                       │
          └──────────────────▼───────────────────────┘
                             │
                      ┌──────────────┐
                      │   COP-UI     │
                      │  (browser)   │
                      │              │
                      │  Video → MSE │  ← SIG-010
                      │  Map icon    │  ← STR-003
                      │  Clearance   │  ← SIG-003
                      └──────────────┘
```

### Channel 1 — Video (SIG-010 push stream)

`GET /signet/stream/fmv/{mission_id}/{drone_id}`

Continuous MPEG-TS chunked HTTP response. Signet evaluates OPA policy at each GOP boundary; denied GOPs are absent from the stream (no bytes delivered). mpegts.js in the browser demuxes MPEG-TS → fMP4 → MSE SourceBuffer. One long-lived connection per (operator, drone) pair. At 10 drones × 3 operators = 30 concurrent connections.

### Channel 2 — Map telemetry (STR-003, currently OPEN)

`event: klv_telemetry` on the SIG-003 SSE connection.

Signet extracts MISB ST0601 KLV from the video stream GOP as it passes through the SIG-010 handler and emits a lightweight SSE event carrying `{drone_id, lat, lon, alt_m, sensor_ts, frame_seq}`. The COP-UI uses this to drive the drone icon position at GOP rate (1–2/s), with client-side interpolation filling the gaps at 60 fps. One shared SSE connection covers all drones.

### Channel 3 — Zone/policy events (SIG-003 + STR-005)

`event: zone_transition` on the SIG-003 SSE connection.

Signet detects zone changes between successive ingested objects for the same drone and emits an explicit zone_transition event for every crossing, even if multiple crossings occur within one bulk-ingest batch. The COP-UI uses this to update the clearance card and flash callout. This fires at most once per ~7–20 s per drone in normal flight — negligible SSE load.

---

## 4. Throughput Analysis

### 4.1 Assumptions

| Constant | Value | Source |
|---|---|---|
| H.264 video bitrate (1080p CBR) | 4 Mbps | Typical ISR EO/IR |
| H.264 video bitrate (720p demo) | 600 kbps | FFmpeg ultrafast preset |
| GOP duration | 1 s (`keyint=25`, 25 fps) | `video_generator.py` |
| Segment duration | 2 s (2 GOPs/envelope) | `SEGMENT_DURATION_S=2.0` |
| RSA-OAEP-256 + PSS per envelope | ~10 ms | Measured on i7 |
| AES-256-GCM throughput | ~2 GB/s | Hardware AES-NI |
| Operators per deployment | 3–10 | Demo: 3 |
| Drones per mission | 1–10 | Demo: 1 |

### 4.2 Single drone, production

| Metric | Value |
|---|---|
| Envelopes/s into Signet | 0.5 (GOP=2s) – 1 (GOP=1s) |
| Ingest payload size | ~1 MB/envelope (1080p) |
| Ingest bandwidth to Signet | ~0.5–1 MB/s |
| Crypto cost (drone-sim) | ~10 ms/envelope → <1% of 1 core |
| KAS decrypt ops/s (streaming) | 0.5–1 per active operator stream |
| SIG-003 SSE events/s | ~0 (only on zone transitions) |
| klv_telemetry SSE events/s (STR-003) | 0.5–1 per drone |
| Signet objects/hour | 1,800–3,600 |

### 4.3 Multi-drone fan-out

| Scenario | Envelopes/s | Ingest MB/s | KAS ops/s | SSE events/s |
|---|---|---|---|---|
| 1 drone, demo (1 fps, 720p) | 1 | 0.2 | 3 (3 ops) | 1 telemetry |
| 1 drone, prod (GOP=2s, 1080p) | 0.5 | 0.5 | 1.5 | 0.5 telemetry |
| 5 drones, prod (GOP=2s, 1080p) | 2.5 | 2.5 | 7.5 | 2.5 telemetry |
| 10 drones, prod (GOP=2s, 1080p) | 5 | 5 | 15 | 5 telemetry |
| 10 drones, 10 ops (GOP=2s) | 5 | 5 | 50 | 5 telemetry |

**KAS is the scaling constraint.** At 50 KAS decrypt ops/s (10 drones × 10 operators × 0.5/s) and ~20 ms per RSA-OAEP-256 decrypt, a single-threaded KAS would need ~1,000 ms/s — fully saturated. The KAS worker pool size (`KAS_WORKER_THREADS`) must be set to at least `ceil(drone_count × operator_count × ops_per_s × rsa_ms / 1000)`.

### 4.4 Crypto cost summary

All RSA operations are at 2048-bit. Upgrading to 3072-bit (CNSA 2.0 recommendation) approximately doubles RSA time.

| Operation | Where | Time | Rate at demo | Rate at 10 drones |
|---|---|---|---|---|
| RSA-OAEP-256 encrypt (DEK wrap) | drone-sim | ~5 ms | 1/s | 5/s |
| RSA-PSS sign | drone-sim | ~5 ms | 1/s | 5/s |
| AES-256-GCM encrypt (~1 MB) | drone-sim | <1 ms | 1/s | 5/s |
| RSA-OAEP-256 decrypt (KAS) | Signet/KAS | ~15 ms | 3/s (3 ops) | 50/s (10 ops) |

---

## 5. Demo vs Production Comparison

| Dimension | Demo (current) | Production target |
|---|---|---|
| Drone count | 1 | 5–20 |
| Frame rate | 1 fps (telemetry only) | 25 fps video |
| Wrap granularity | 1 envelope per KLV frame | 1 envelope per GOP (1–2 s) |
| Video delivery | mpegts.js → SIG-010 push stream | Same (SIG-010 already implemented) |
| Telemetry to map | SIG-003 SSE `data:` at 1 event/s | STR-003 `klv_telemetry` at GOP rate |
| Zone/policy events | Inferred from successive SSE frames | Explicit `zone_transition` SSE (STR-005) |
| Video latency | ~1–2 s (GOP boundary) | ~0.5–1 s (shorter GOP) |
| Map update latency | 1 s | 1–2 s (GOP), smoothed by interpolation |
| SSE load per drone | 1 event/s | ~0 zone events + 0.5–1 telemetry/s |
| Signet objects/hr/drone | 3,600 | 1,800–3,600 |
| Multi-drone COP-UI | Not implemented | 1 SSE conn + N video elements |
| `BULK_SIZE` | 1 (demo) / 5 (configured) | N_DRONES (one batch per second) |
| `SEGMENT_DURATION_S` | 2.0 | 1.0–2.0 |
| `FRAME_INTERVAL` | 1.0 | = SEGMENT_DURATION_S |

---

## 6. STR-003 Integration Design

STR-003 (status: OPEN — filed 2026-03-08) is the key upstream change that enables scalable multi-drone map updates without SSE-per-frame overhead.

### Current state without STR-003

The COP-UI drives map position from `data:` SSE events (one per frame). At 1 fps this works. At GOP rate (0.5–1/s per drone) it still works — but only if `frame_interval_ms` is included (STR-002, implemented) so the interpolation duration is correct. STR-003 is strictly required only at ≥5 fps FMV rates.

### How STR-003 fits with SIG-010

Signet's SIG-010 handler already processes each GOP as it streams to operators. STR-003 asks Signet to additionally:
1. Decode the KLV PES packet from PID 0x0065 in the GOP
2. Extract lat/lon/alt/sensor_ts from MISB ST0601 tags 13, 14, 15, 2
3. Emit `event: klv_telemetry` on the existing SIG-003 SSE connection

This is additive — SIG-003 SSE clients gain a new event type; existing `data:` object notification clients are unaffected.

### COP-UI changes required when STR-003 is implemented

The SSE parser already handles `event:` lines (added in this demo). When Signet ships STR-003:

1. Add `klv_telemetry` handler alongside the existing `zone_transition` handler in `_processSseFrame`
2. Update map position from `klv_telemetry` instead of (or in addition to) the `data:` object notification
3. Remove `_demoInterval` polling fallback — `frame_interval_ms` from STR-002 covers 1-fps demo; `klv_telemetry` covers production FMV

---

## 7. Drone-Sim Changes for Throughput Testing

### 7.1 Environment variable reference

| Variable | Demo default | Production recommendation |
|---|---|---|
| `FRAME_INTERVAL` | `1.0` | `= SEGMENT_DURATION_S` |
| `SEGMENT_DURATION_S` | `2.0` | `1.0` (lower latency) |
| `BULK_SIZE` | `1` | `N_DRONES` (batch all drones per cycle) |
| `DRONE_ID` | `UAS-001` | per-instance (see multi-drone below) |
| `VIDEO_ENABLED` | `true` | `true` for FMV; `false` for KLV-only stress test |
| `VIDEO_SOURCE_PATH` | unset (synthetic) | `/footage/aerial.mp4` |

### 7.2 GOP-aligned configuration

For production-representative behaviour, set:

```yaml
SEGMENT_DURATION_S: "1.0"
FRAME_INTERVAL: "1.0"
BULK_SIZE: "1"
```

This produces one ZTDF envelope per 1-second GOP, one ingest call per second per drone — matching the production per-GOP model.

### 7.3 Multi-drone simulation options

**Option A — Docker Compose scale (simplest, no code change):**

```bash
# In deploy/docker-compose.yml, override DRONE_ID per replica:
docker compose up --scale drone-sim=5
# (Each replica needs a unique DRONE_ID — use an entrypoint script or
#  separate service definitions with DRONE_ID: UAS-001..UAS-005)
```

Each instance runs an independent mission path. No coordination, but sufficient for throughput testing.

**Option B — Multi-drone mode in simulator.py (future work):**

`python simulator.py --drones 5 --phase-offset 30` spawns 5 threads, each running a `MissionPath()` with a 30-second stagger. All threads share a flusher thread that batches envelopes from all drones into a single `POST /signet/bulk-ingest` call per second (`BULK_SIZE=N_DRONES`). This produces realistic coordinated multi-drone batches.

**Option C — Replay N missions in parallel (deterministic testing):**

```bash
python simulator.py --replay mission.jsonl &
python simulator.py --replay mission.jsonl --drone-id UAS-002 &
# ... N times
```

### 7.4 Async crypto for stress testing

At 10 drones × 1 envelope/s = 10 `make_envelope()` calls/s, synchronous crypto is fine (~100 ms/s on a single core, well within budget). If simulating unrealistic per-frame wrapping at 25 fps:

- 250 envelopes/s × 10 ms = 2,500 ms/s — requires a `ThreadPoolExecutor`
- Replace the synchronous `make_envelope(...)` call in the main loop with `executor.submit(make_envelope, ...)`
- The flusher thread consumes completed futures and posts batches

This is only needed for synthetic stress testing. In production, per-GOP wrapping keeps the load manageable.

---

## 8. Reference

- [docs/ARCHITECTURE.md](ARCHITECTURE.md) — demo system architecture
- [docs/SMOOTH-TELEMETRY-REQUIREMENTS.md](SMOOTH-TELEMETRY-REQUIREMENTS.md) — STR-001 through STR-005
- [docs/UPSTREAM-REQUIREMENTS.md](UPSTREAM-REQUIREMENTS.md) — SIG-001 through SIG-012
- [services/drone-sim/video_generator.py](../services/drone-sim/video_generator.py) — GOP/segment constants
- [services/drone-sim/simulator.py](../services/drone-sim/simulator.py) — `make_envelope()`, `BULK_SIZE`, `FRAME_INTERVAL`
- MISB ST0601 — UAS Datalink Local Metadata Set
- STANAG 4609 — NATO Digital Motion Imagery Standard
- STANAG 5636 — ACP-240 Object Handling Labels
