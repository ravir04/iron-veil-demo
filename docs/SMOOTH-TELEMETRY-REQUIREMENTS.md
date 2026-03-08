# Smooth Telemetry Streaming — Upstream Requirements for Signet Team

**Document:** Iron-Veil Demo — Upstream Requirements
**Date:** 2026-03-08
**Status:** OPEN
**Raised by:** Iron-Veil Demo team

---

## Background

In production, drones will traverse multiple policy zones in rapid succession
(e.g. TRANSIT → UK_BASE → EXERCISE_CORRIDOR within 2–5 seconds at operational
speeds). The current demo exposes two classes of problem when this happens:

1. **Jerky drone icon** — the map icon snaps frame-by-frame rather than moving
   smoothly. At 1 frame/sec the drone teleports ~300 m per tick.

2. **Burst delivery** — when Signet notifies multiple frames simultaneously
   (bulk-ingest batch), the COP-UI receives them all at once and renders them
   in a burst, causing visible stutter and rapid-fire zone-transition flash
   callouts that are unreadable.

The demo has implemented client-side mitigations (position interpolation, SSE
queue, zone-transition debouncing). The items below are upstream changes
needed in Signet to fully resolve the issues at scale.

---

## Requirements

### STR-001 — Per-frame SSE notification with ingest timestamp

**Priority:** HIGH
**Current behaviour:** Signet emits one SSE event per object ingested, but
when frames arrive via `/signet/bulk-ingest` in a batch, all notifications
are emitted together after the batch is committed. The COP-UI receives N
frames simultaneously.
**Required behaviour:** SSE notifications should be emitted individually as
each frame is committed to the store, with a minimum inter-event gap of
`floor(frame_interval / batch_size)` ms. This spreads burst notifications
evenly over the expected frame interval.
**Acceptance:** At 4× demo speed (250 ms frame interval, batch size 1), the
UI should receive one SSE event every ~250 ms ± 50 ms jitter. No burst of
>2 events within a 100 ms window.

---

### STR-002 — Include `frame_interval_ms` in SSE event payload

**Priority:** HIGH
**Current behaviour:** The SSE event payload does not include the frame
interval. The COP-UI has to infer it from `/demo/status` polling (1 s
latency).
**Required behaviour:** Each SSE object-notification event should include
`frame_interval_ms` (integer, milliseconds) so the client can set the correct
interpolation duration immediately without a separate poll.

**Proposed payload addition:**
```json
{
  "object_id": "...",
  "ingest_ts": 1772934855380.07,
  "frame_interval_ms": 250,
  "labels": { ... },
  "metadata": { ... }
}
```

**Acceptance:** `frame_interval_ms` present in every object SSE event.
Value matches the current `FRAME_INTERVAL` env var on drone-sim within ±10 ms.

---

### STR-003 — KLV-embedded telemetry extraction for FMV streams (SIG-010 extension)

**Priority:** MEDIUM
**Current behaviour:** Map telemetry is driven entirely by the SSE channel
(SIG-003). For high-frame-rate FMV (≥25 fps), using SSE-per-frame for
telemetry is unscalable — it would generate 25+ SSE events/second per drone.
**Required behaviour:** Signet's FMV stream endpoint (`/signet/stream/fmv`)
should extract MISB ST 0601 KLV metadata from each MPEG-TS GOP and emit a
lightweight telemetry-only SSE event (`event: klv_telemetry`) containing
lat/lon/alt/sensor_ts. The main object-notification SSE (SIG-003) would
then only fire on **zone transitions** and **policy changes**, not on every
frame.

**Proposed SSE event:**
```
event: klv_telemetry
data: {"mission_id":"FVEX-26","drone_id":"UAS-001","lat":51.272,"lon":-0.158,"alt_m":300,"sensor_ts":1772934855.3,"frame_seq":320}
```

**Acceptance:**
- `klv_telemetry` events fire at the GOP rate (configurable, default 2 s)
- Object-notification events (existing format) only fire on zone/policy change
- Existing SIG-003 clients continue to work unchanged (additive change)

---

### STR-004 — Configurable bulk-ingest batch size via API

**Priority:** LOW
**Current behaviour:** `BULK_SIZE` is an environment variable set at
drone-sim startup. Changing it requires a restart.
**Required behaviour:** `/signet/bulk-ingest` should accept an optional
`batch_hint` field indicating the intended frame interval in ms. Signet uses
this to pace SSE emission (see STR-001) without requiring drone-sim changes.

**Proposed request field:**
```json
{
  "batch_hint_ms": 250,
  "envelopes": [ ... ]
}
```

**Acceptance:** When `batch_hint_ms` is present, Signet spaces SSE
notifications evenly across `batch_hint_ms` milliseconds regardless of
how many envelopes are in the batch.

---

### STR-005 — Zone-transition event in SSE payload

**Priority:** MEDIUM
**Current behaviour:** The COP-UI detects zone changes by comparing the
`zone` field in successive SSE events. At high drone speeds, several zone
transitions may happen within one SSE burst and all but the last are lost.
**Required behaviour:** When an object's zone changes from the previous
ingested object for the same drone, Signet should emit an additional SSE
event of type `zone_transition`:

```
event: zone_transition
data: {
  "drone_id": "UAS-001",
  "mission_id": "FVEX-26",
  "from_zone": "TRANSIT",
  "to_zone": "EXERCISE_CORRIDOR",
  "ts": 1772934875000,
  "lat": 51.271,
  "lon": -0.183
}
```

This allows the COP-UI to correctly record every zone crossing for audit
purposes even when multiple crossings occur between telemetry ticks.

**Acceptance:** A `zone_transition` event is emitted for every zone change,
even if multiple transitions occur within a single bulk-ingest batch.

---

## Client-side mitigations already implemented (Iron-Veil Demo)

The following have been implemented in `services/cop-ui/app.js` as interim
measures while the above Signet changes are pending:

| Mitigation | Description |
|---|---|
| Position interpolation | 60 fps ease-out lerp between telemetry ticks; duration = `_demoInterval * 1000` ms |
| SSE frame queue | Frames enqueued and processed one-at-a-time; prevents concurrent async handlers from interleaving |
| Zone transition debouncing | `updateClearanceCard` deferred 600 ms after last zone change; prevents rapid-fire flash callouts |
| Drone icon hide on deny | `_droneTo = null` when frame is denied; icon disappears cleanly rather than freezing at last position |

These mitigations work well at demo scale (1 drone, 1 frame/sec). They will
not scale to production FMV rates without STR-003.

---

## Reference

- MISB ST 0601 — UAS Datalink Local Metadata Set
- STANAG 4609 — NATO Digital Motion Imagery Standard
- [docs/ARCHITECTURE.md](ARCHITECTURE.md)
- [docs/UPSTREAM-REQUIREMENTS.md](upstream-requirements.md)
