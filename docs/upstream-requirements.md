# Upstream Requirements

This file tracks changes required to **Signet** and **Iron-Veil** that are needed to support the Drone COP demo and to **harden both platforms for production-grade FMV streaming workloads**.

The Drone COP project is being used as a live integration harness to drive these hardening requirements. Changes discovered during demo development are captured here and handed to the Signet and Iron-Veil teams.

No changes have been made to those repos from this project.

---

## Background: Why FMV Streaming Hardening?

The Drone COP demo is not just a standalone showcase. It is the first concrete exercise of using Signet and Iron-Veil as the DCS enforcement layer for **continuous streaming sensor data** — specifically STANAG 4609 Full Motion Video with MISB ST0601 metadata.

The end-state architecture the teams are working toward is:

```
Drone / UAS sensor
  → KLV telemetry + video frames
  → ACP-240 ZTDF envelope per frame (drone-sim today; real sensor adapter in future)
  → Signet ingest (per-frame, high-throughput)
  → Policy-filtered fan-out to:
       a) Live COP viewers  (cop-ui — low-latency, real-time)
       b) Data lake / archive  (S3, object store, classified data lake)
       c) Downstream analysis  (video analytics, AI/ML pipelines — future)
```

Signet and Iron-Veil were designed and tested for **chat-sized** objects (text messages, small files). FMV streaming introduces:
- **High object volume** — 1 frame/second = 3,600 objects/hour per drone
- **Streaming semantics** — consumers need ordered, low-latency delivery, not just random-access fetch
- **Fan-out** — one producer, many consumers, each with different policy → different plaintext views
- **Data lake integration** — objects must be queryable by time range, zone, classification, mission
- **Large payloads** — video segments can be 100KB–10MB per object; current ZTDF wrapping is optimized for KB-scale

---

## Priority Levels

- **P0** — Blocking: demo cannot run without this
- **P1** — Important: demo degrades significantly without this
- **P2** — Nice to have: improves demo quality
- **P3** — FMV hardening: required for production streaming / data lake use cases

---

## Signet Requirements

### [P0] SIG-001 — Object List / Catalog API

**Context:** The COP-UI needs to poll for newly ingested objects to know when new telemetry frames are available. Today there is no API to enumerate ingested objects.

**Requirement:** Expose a paginated object list endpoint:
```
GET /signet/objects?since=<unix_ms>&limit=100&cls=SECRET
→ [
    {
      "object_id": "...",
      "ingest_ts": 1234567890.123,
      "labels": {"cls": "SECRET", "releasability": ["CAN","FVEY"]},
      "manifest_hash": "sha256:...",
      "issuer_id": "drone-sim-dev"
    },
    ...
  ]
```

**Workaround in use:** drone-sim exposes a sidecar SSE feed at `:8091/stream` that pushes object IDs directly. This avoids the Signet change for the demo but is not production-ready.

---

### [P0] SIG-002 — Unwrap Response Includes OCL Labels

**Context:** COP-UI needs the zone classification label alongside the plaintext to colour-code track dots.

**Requirement:** `GET /signet/unwrap/{id}` response includes labels:
```json
{
  "plaintext_b64": "...",
  "object_id": "...",
  "labels": { "cls": "SECRET", "releasability": ["CAN", "FVEY"] }
}
```

**Workaround in use:** COP-UI reads labels from the sidecar SSE payload (which carries zone metadata from the drone-sim).

---

### [P1] SIG-003 — Server-Sent Events / Push Notification for New Objects

**Context:** Polling creates 0–1 second delay. For a live COP, sub-second track updates matter.

**Requirement:**
```
GET /signet/stream/objects   (SSE, text/event-stream)
  Authorization: Bearer <jwt>
→ data: {"object_id": "...", "ingest_ts": ..., "labels": {...}}
```

**Workaround in use:** drone-sim sidecar SSE at `:8091/stream`.

---

### [P1] SIG-004 — Node Clearance Configuration Verification

**Context:** Signet's ingest OPA policy checks the node's own clearance before storing. The demo needs SECRET objects to be accepted.

**Requirement:** Confirm and document that the demo Signet node is configured with:
```
NODE_CLEARANCE=SECRET
NODE_RELS=["CAN","FVEY","GBR","USA","AUS","NZL"]
NODE_CAVEATS=[]
```

**Action:** Signet team to confirm current demo node config covers all releasability groups used in the mission (CAN, GBR, FVEY).

---

### [P3] SIG-005 — High-Throughput Ingest: Batch Endpoint

**Context:** At 1 frame/second per drone, ingestion is manageable. At 30 fps video segments, or with multiple drones, the per-frame HTTP round-trip overhead becomes significant.

**Requirement:** `POST /signet/ingest/batch` — accepts an array of ZTDF envelopes, processes them as a transaction, returns per-envelope `{object_id, status, error?}`.

**FMV Hardening Rationale:** Required for production sensor-to-archive pipelines where throughput must exceed 10 objects/second.

---

### [P3] SIG-006 — Streaming Payload Support (Large Objects)

**Context:** Current ZTDF implementation wraps the entire payload in one AES-GCM encryption block. For video segments (1–10 MB), this works but is memory-intensive for the KAS at unwrap time.

**Requirement:** Support ZTDF `isStreamable: true` with chunked segments — the `encryptionInformation.segments` array already exists in the schema but the KAS currently processes the full ciphertext. Implement streaming unwrap that decrypts and streams chunks without buffering the full plaintext.

**FMV Hardening Rationale:** Required for video archive use cases where consumers (data lake writers, video analytics) need to process large video segments without OOM on the KAS.

---

### [P3] SIG-007 — Time-Range Query on Ingested Objects

**Context:** A downstream data lake or analysis system needs to retrieve all SECRET/FVEY frames from a specific mission time window.

**Requirement:** Extend the object list API (SIG-001) with time-range and label filtering:
```
GET /signet/objects?from=<iso8601>&to=<iso8601>&cls=SECRET&rel=FVEY&issuer=drone-sim-dev
```

**FMV Hardening Rationale:** Required for offline/DDIL data lake queries and post-mission analysis. Without this, consumers must maintain their own index.

---

### [P3] SIG-008 — Object Metadata Index (Mission Correlation)

**Context:** FMV frames need to be correlated by mission, drone ID, and time sequence. The current ZTDF envelope has no standard field for application-level correlation metadata.

**Requirement:** Support an optional `metadata` block in the ingest envelope that is stored alongside the object (not encrypted, not policy-enforced) and returned in SIG-001 queries:
```json
{
  "metadata": {
    "mission_id": "FVEX-26",
    "drone_id": "UAS-001",
    "frame_seq": 42,
    "sensor_ts": 1234567890.123
  }
}
```

**FMV Hardening Rationale:** Required for data lake cataloguing, where a query like "all frames from drone UAS-001 between T+100s and T+200s of mission FVEX-26" must be answerable without decrypting every object.

---

### [P3] SIG-010 — Live FMV Streaming Endpoint (Continuous MPEG-TS over HTTP)

**Context:** The current SIG-006 endpoint (`GET /signet/unwrap/{id}/stream`) is a **pull** model — the client fetches one ZTDF object at a time after receiving an SSE notification. For production FMV delivery this introduces ~1–3s of round-trip overhead per segment (SSE event → fetch → decrypt → play), which is acceptable for the demo but unacceptable for live ISR (Intelligence, Surveillance, Reconnaissance) where glass-to-glass latency must be under 1 second.

A production FMV streaming endpoint is a **push** model: the client opens a single long-lived HTTP connection to Signet and receives a policy-filtered, decrypted MPEG-TS bitstream in real time, with the KAS applying the OPA policy gate at the GOP (Group of Pictures) boundary — not the segment boundary.

**Architecture:**

```
Drone sensor
  → continuous MPEG-TS bitstream (video + KLV on PID 0x0065)
  → Signet ingest (per-GOP ACP-240 wrap)
  → Signet FMV streaming endpoint
      GET /signet/stream/fmv/{mission_id}/{drone_id}
        Authorization: Bearer <jwt>
      ← Transfer-Encoding: chunked
      ← Content-Type: video/MP2T
      ← [decrypted MPEG-TS chunks, policy-gated per GOP]
      ← [HTTP 200 continues until mission ends or session revoked]
```

**Policy gating at GOP boundary:**
- Each GOP is one ACP-240 ZTDF object (1–2s of video)
- At each GOP boundary, Signet evaluates the OPA policy for the requesting JWT against the GOP's labels
- If ALLOW: decrypt and stream the GOP bytes into the response
- If DENY: substitute a "feed redacted" placeholder GOP (black frames with a KLV timestamp-only stream) OR simply stop writing to the response (client's `<video>` element stalls)
- Label changes (zone transitions) take effect at the next GOP boundary — no mid-GOP key switches

**Requirement:**
```
GET /signet/stream/fmv/{mission_id}/{drone_id}
    Authorization: Bearer <jwt>
    Accept: video/MP2T

Response:
    200 OK
    Content-Type: video/MP2T
    Transfer-Encoding: chunked
    X-Signet-Policy-Mode: gop-gated

    [chunked MPEG-TS stream, policy-filtered per GOP]
    [connection held open until: mission complete | drone offline | session expired]

On policy DENY at GOP boundary:
    Option A: substitute redacted GOP (black H.264 + KLV timestamp-only)
    Option B: stop writing — client stalls — HLS.js/mpegts.js buffers exhaust
    Option C: send HTTP trailer X-Signet-Redact: {reason} and close stream
```

**Client changes (cop-ui):**
- Replace the current SSE + per-segment fetch with a single `<video>` source pointing to the streaming endpoint
- mpegts.js or native MSE handles continuous playback
- Redaction events delivered via a parallel SSE channel (SIG-003) for UI overlay control (the video itself goes black, but the UI needs to know _why_ to show the correct message)

**FMV Hardening Rationale:**
- Eliminates the SSE → fetch → decrypt round-trip latency (from ~1–3s per segment to <100ms per GOP boundary)
- Enables true glass-to-glass latency under 1s — required for live ISR, SAR coordination, and weapons-system guidance support
- Single TCP connection per operator per drone — dramatically reduces connection overhead vs current N fetches/minute
- KAS load is lower: one streaming session vs one decrypt call per segment per operator
- Enables server-push revocation: if a user's JWT is revoked mid-mission, Signet can terminate the stream immediately at the next GOP without waiting for the client to poll

**Status:** **Closed** — implemented by Signet team (2026-03-06). `GET /signet/stream/fmv/{mission_id}/{drone_id}` now live.

**Blocks:** Requires SIG-003 (SSE notifications) to remain available as the redaction signalling channel alongside the new push stream.

---

### [P3] SIG-009 — Data Lake Sink Connector

**Context:** After policy-filtered ingest, authorized ciphertext objects need to be written to a downstream store (S3, MinIO, Azure Blob) for archival. This should be automatic, not a separate application.

**Requirement:** Optional Signet configuration to mirror ingested objects (ciphertext + metadata only, never plaintext) to a configured object store endpoint. Policy is preserved — only the ciphertext travels to the lake; keys remain in the KAS.

**FMV Hardening Rationale:** Core to the "DCS data lake" pattern. The lake holds ACP-240-protected objects that can only be decrypted by authorized consumers with valid JWTs, even after the live mission ends.

---

### [P0] SIG-011 — SSE Event Must Include Object Metadata

**Context:** Found during demo integration (2026-03-07).

The `GET /signet/stream/objects` SSE event currently only carries:
```json
{"object_id": "...", "ingest_ts": ..., "labels": {...}, "issuer_id": "..."}
```

It does **not** include the `metadata` block (zone, lat, lon, alt_m, mission_id, drone_id, frame_seq, etc.) that was stored with the object at ingest time. The COP-UI needs `zone`, `lat`, and `lon` from `metadata` to plot track dots on the map. Without them, the map shows nothing even when frames are ingesting correctly.

**Requirement:** Include the stored `metadata` JSONB in the SSE event:
```json
{
  "object_id": "...",
  "ingest_ts": 1772858147574.1,
  "labels": {"classification": "PROTECTED", "releasability": ["FVEY"], "caveats": []},
  "issuer_id": "node-issuer",
  "metadata": {
    "mission_id": "FVEX-26",
    "drone_id": "UAS-001",
    "frame_seq": 124,
    "zone": "TRANSIT",
    "lat": 51.2558,
    "lon": -0.4121,
    "alt_m": 500.0,
    "mission_time_s": 124
  }
}
```

**Workaround in use:** cop-ui calls `GET /signet/objects?since=<ingest_ts-2000>&limit=20` per frame and finds the matching object by `object_id` to retrieve metadata. This adds one extra HTTP round-trip per frame (60 extra requests/minute at 1 fps).

**Fix:** In `store.py` / `_sse_subscribers`, include `metadata` from the objects row when broadcasting the SSE event.

---

### [P0] SIG-012 — Bulk-Ingest Rate Limit Too Low for Streaming Telemetry

**Context:** Found during demo integration (2026-03-07).

The bulk-ingest endpoint has a `@limiter.limit("20/minute")` rate limit keyed by source IP. At 1 frame/second, the drone-sim POSTs 60 envelopes/minute even with `BULK_SIZE=5` (12 requests/minute). With multiple drones or higher frame rates this hits the limit immediately, causing `HTTP 429` and dropped frames.

**The demo workaround is `BULK_SIZE=5`** (batch 5 frames per request → 12 requests/minute < 20/minute limit). This works for 1 drone at 1 fps, but fails at higher rates.

**Root cause:** The rate limiter is designed for human-interactive API usage (60 req/min default, 20/min for bulk). Streaming telemetry is machine-to-machine with a predictable, sustained request rate — a different usage pattern.

**Requirement options (in order of preference):**

1. **Remove rate limit on bulk-ingest entirely** — it is an authenticated endpoint (issuer trust store), not a public API. Rate limiting should be a network/infrastructure concern (WAF, API gateway), not Signet's responsibility.
2. **Make rate limit configurable** via env var `BULK_INGEST_RATE_LIMIT` (default `"1000/minute"` or `"0"` for disabled).
3. **Key rate limit by issuer_id** rather than source IP — so each drone (issuer) gets its own bucket, and a burst from one doesn't affect others.

**Current impact:** At `BULK_SIZE=1` (the natural demo value): 429 errors start after ~20 seconds of mission. Demo requires `BULK_SIZE=5` workaround. Production multi-drone scenarios are blocked entirely.

---

## Iron-Veil Requirements

### [P2] IV-001 — Drone Telemetry Event Type in Matrix Room

**Context:** Tie the COP demo to Iron-Veil's chat demo — drone position references posted to the Matrix room alongside operator messages.

**Requirement:** Define Matrix custom event `m.iron_veil.drone_ref`:
```json
{
  "type": "m.iron_veil.drone_ref",
  "content": {
    "object_id": "...",
    "zone": "CAN_BASE",
    "classification": "SECRET",
    "mission_time_s": 30
  }
}
```
Matrix-proxy bot renders these as formatted position cards. OPA policy filters them per-user (same mechanism as message filtering).

---

### [P3] IV-002 — FMV Object Type in Catalog API

**Context:** The catalog-api currently lists all Signet objects as generic entries. FMV frames should be queryable as a distinct type with mission metadata.

**Requirement:** Extend catalog schema to include:
- `object_type`: `"fmv_frame"` | `"chat_message"` | `"attachment"`
- `mission_id`, `drone_id`, `frame_seq`, `zone`, `sensor_ts` (from SIG-008 metadata)

**FMV Hardening Rationale:** Provides the UI layer for the data lake — operators and analysts can browse the mission catalog filtered by type and mission.

---

### [P3] IV-003 — OPA Policy: Zone-Based Caveats (Design Only)

**Context:** For future use, zone access could be enforced as a caveat (`ZONE_CAN_BASE`, `ZONE_TARGET`) rather than relying solely on releasability.

**Requirement:** No code change. Design a caveat naming convention and document how zone caveats would be added to subject profiles in Signet's Keycloak and matched against object labels in the OPA policy.

**Rationale:** Deferred. Releasability-based zone control is sufficient for the demo. Caveat-based zone control enables finer-grained operator access (e.g., cleared for SECRET/FVEY but not permitted into the target area).

---

## Tracking

| ID | Team | Priority | Status | Notes |
|---|---|---|---|---|
| SIG-001 | Signet | P0 | **Closed** | `GET /signet/objects` now accepts `since=<unix_ms>`, `issuer=<id>` filters; response includes `ingest_ts` (Unix ms) and `issuer_id` per item — 2026-03-06 |
| SIG-002 | Signet | P0 | **Closed** | `/signet/unwrap/{id}` already returns `{object_id, plaintext, labels:{classification,releasability,caveats}}` — confirmed 2026-03-06 |
| SIG-003 | Signet | P1 | **Closed** | `GET /signet/stream/objects` SSE endpoint added (JWT-authed); pushes `{object_id, ingest_ts, labels, issuer_id}` on every wrap/ingest — 2026-03-06 |
| SIG-004 | Signet | P1 | **Closed** | Confirmed: NODE_CLEARANCE=SECRET, NODE_RELS=["CAN","FVEY","NATO","AUS","NZL","GBR","USA"], NODE_CAVEATS=[] — covers all demo releasability groups |
| SIG-005 | Signet | P3 | **Closed** | `POST /signet/bulk-ingest` already satisfies this requirement. Accepts up to 100 ZTDF envelopes per request, processes each independently, returns per-envelope `{object_id, ok, error}`. Rate limit: 20 req/min. URL differs from requirement (`/signet/bulk-ingest` not `/signet/ingest/batch`) — update drone-sim client accordingly — 2026-03-06 |
| SIG-006 | Signet | P3 | **Closed** | `GET /signet/unwrap/{id}/stream` added — same three-gate OPA check; returns `application/octet-stream` via `StreamingResponse`; decrypts per-segment if `encryptionInformation.segments` populated, falls back to single-block otherwise — 2026-03-06 |
| SIG-007 | Signet | P3 | **Closed** | `GET /signet/objects` extended with `from=<iso8601>`, `to=<iso8601>`, `rel=<community>` filters; time range uses PostgreSQL timestamp comparison on `created_at`; releasability uses JSONB `@>` containment check — 2026-03-06 |
| SIG-008 | Signet | P3 | **Closed** | Optional `metadata` JSONB field accepted in ingest envelope and stored in `objects` table (not encrypted, not policy-enforced); returned in `GET /signet/objects` items; filterable via `mission_id=` and `drone_id=` query params — 2026-03-06 |
| SIG-009 | Signet | P3 | **Closed** | Data lake S3 sink added — configure `DATA_LAKE_S3_ENDPOINT/ACCESS_KEY/SECRET_KEY/BUCKET` env vars; on each successful wrap/ingest, a daemon thread mirrors ciphertext, manifest JSON, and `lake_meta.json` to the lake bucket; plaintext never leaves the KAS — 2026-03-06 |
| SIG-011 | Signet | P0 | **Closed** | `metadata` JSONB included in all `_sse_notify` calls (wrap, ingest, bulk-ingest). SSE event now carries `zone`, `lat`, `lon`, `mission_id`, `drone_id`, etc. — no extra `GET /signet/objects` round-trip needed per frame — 2026-03-07 |
| SIG-012 | Signet | P0 | **Closed** | Hardcoded `20/minute` bulk-ingest rate limit replaced with `BULK_INGEST_RATE_LIMIT` env var (default `1000/minute`). Set to `""` to disable. No more 429s at production telemetry rates — 2026-03-07 |
| SIG-010 | Signet | P3 | **Closed** | `GET /signet/stream/fmv/{mission_id}/{drone_id}` implemented — continuous `video/MP2T` chunked stream, OPA policy-gated per GOP. On ALLOW: decrypt + stream GOP bytes. On DENY: substitute MPEG-TS null-packet redacted GOP placeholder, emit redaction event on SIG-003 SSE channel. Idle-closes after 30s without a GOP. Requires `metadata.mission_id` + `metadata.drone_id` on ingest envelopes (SIG-008) — 2026-03-06 |
| IV-001  | Iron-Veil | P2 | **Closed** | `m.iron_veil.drone_ref` event type implemented in matrix-proxy bot — OPA-filtered position cards rendered in room — 2026-03-06 |
| IV-002  | Iron-Veil | P3 | **Closed** | `GET /objects` list endpoint + `object_type`/`mission_id`/`drone_id`/`frame_seq`/`zone`/`sensor_ts` columns added to catalog-api — 2026-03-06 |
| IV-003  | Iron-Veil | P3 | **Closed** | Zone caveat design doc written at `docs/ZONE-CAVEAT-DESIGN.md` — no OPA change needed, `ZONE_*` strings work with existing caveats gate — 2026-03-06 |
