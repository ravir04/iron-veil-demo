# Iron-Veil Demo — Testing Guide

## Quick Reference

| What | Where |
|---|---|
| COP-UI (Alice) | http://localhost:8090?user=alice |
| COP-UI (Bob) | http://localhost:8090?user=bob |
| COP-UI (Dave) | http://localhost:8090?user=dave |
| Signet API | http://localhost:4774 |
| Signet Admin / Audit | http://localhost:4775/audit |
| MinIO console (data lake) | http://localhost:9001 (admin / minioadmin) |

---

## 1. Starting the Stack

Start services in order. Each depends on the previous.

```bash
# Step 1 — Signet (identity + policy + KAS)
cd ../signet/deploy
docker compose up -d --build

# Step 2 — Iron-Veil (proxy + catalog)
cd ../../iron-veil/deploy
docker compose up -d --build

# Step 3 — Iron-Veil Demo (drone-sim + cop-ui)
cd ../../iron-veil-demo/deploy
docker compose up -d --build

# Optional: data lake (MinIO)
docker compose --profile datalake up -d
```

Verify all services are up:

```bash
docker compose ps
curl -s http://localhost:4774/health    # Signet
curl -s http://localhost:8090           # COP-UI (nginx 200)
```

---

## 2. Operator Logins

The COP-UI does not have a login screen. Operator identity is set via the `user` query parameter. Signet authenticates with password `password` for all dev users.

| Operator | URL | Clearance | Releasability | What they see |
|---|---|---|---|---|
| **Alice** | `?user=alice` | SECRET | CAN, FVEY, GBR, USA, NATO, AUS, NZL | Everything — 100% of frames |
| **Bob** | `?user=bob` | SECRET | CAN only | CAN_BASE zone only — ~1.6% of frames |
| **Dave** | `?user=dave` | PROTECTED | CAN, FVEY | TRANSIT airspace on return leg — ~80.5% of frames |

Open three browser tabs simultaneously for the side-by-side demo:

- http://localhost:8090?user=alice
- http://localhost:8090?user=bob
- http://localhost:8090?user=dave

---

## 3. Running the Mission

The drone-sim container starts automatically and begins flying the mission on startup. To manually restart or replay:

```bash
# Restart the live mission (runs inside the container)
docker compose restart drone-sim

# Watch the ingest log in real time
docker compose logs -f drone-sim

# Replay a pre-recorded mission (no FFmpeg, no Signet ingest — offline)
docker compose exec drone-sim python simulator.py --replay /footage/../tests/fixtures/mission_recording.jsonl
```

The mission takes approximately **44 minutes** at 1 frame/second (2,625 frames). For faster demo cycles, increase the frame rate:

```bash
# Edit docker-compose.yml: set FRAME_INTERVAL: "0.1" to run 10x speed
docker compose up -d --build drone-sim
```

---

## 4. Testing Scenarios

### Scenario A — Basic Policy Check (Alice sees everything)

1. Open http://localhost:8090?user=alice
2. Watch the map — drone track should appear and grow continuously
3. Watch the video feed — aerial footage plays with HUD overlay showing lat/lon/alt/zone
4. **Expected:** AUTHORIZED count increases every frame. REDACTED stays at 0. Zone changes from `CAN_BASE` → `EXERCISE_CORRIDOR` → `UK_BASE` → `TARGET_AREA` → `TRANSIT` → `CAN_BASE`
5. **Failure indicator:** If REDACTED > 0 for Alice, policy engine is misconfigured

### Scenario B — Releasability Cut-off (Bob loses the drone at the perimeter)

1. Open http://localhost:8090?user=bob
2. Bob sees the drone track while it is over `CAN_BASE` (first ~30 seconds)
3. **Watch moment:** As the drone crosses from `CAN_BASE` into `EXERCISE_CORRIDOR`, Bob's track stops updating and the video feed goes dark (🔒 redacted overlay appears)
4. **Expected:** Last authorized track dot is at approximately lat=51.25, lon=-0.49 (CAN_BASE boundary). All subsequent frames show as REDACTED in the telemetry log
5. **Expected reason in log:** `releasability_mismatch` — Bob holds CAN, zone requires FVEY
6. Bob's video resumes briefly only if/when the drone re-enters `CAN_BASE` on landing (~T+2600s)

**What to verify:**
- Video goes dark and shows 🔒 overlay (not just stops — it actively blanks)
- Telemetry log shows `[REDACTED]` entries in red
- Map track dot freezes at the geofence boundary — the last authorized position

### Scenario C — Clearance Gate (Dave waits, then sees the return leg)

1. Open http://localhost:8090?user=dave
2. Dave's map is empty for the first ~240 seconds — drone is in SECRET zones
3. **Watch moment:** When the drone exits `TARGET_AREA` into `TRANSIT` (approximately T+240s), a track dot suddenly appears on Dave's map and the video feed starts playing
4. **Expected:** Dave's AUTHORIZED count stays at 0 for ~240 frames, then starts climbing. REDACTED count accumulates during the outbound leg
5. **Expected reason for initial denials:** `clearance_insufficient` — Dave is PROTECTED, zone is SECRET
6. Dave's video goes dark again when the drone re-enters `CAN_BASE` on landing

**What to verify:**
- "First frame" moment is dramatic — Dave's map is empty, then a dot appears
- Video feed starts playing from blank/paused state (no buffered footage leaks through)
- Dave sees both the map track and video feed during the return transit leg

### Scenario D — Feed Going Dark (video blackout on policy change)

Test the transition behavior specifically:

1. Open http://localhost:8090?user=bob in a tab that is already showing video (drone over CAN_BASE)
2. Watch the video panel as the drone crosses the CAN_BASE boundary
3. **Expected sequence:**
   - Last authorized segment plays out
   - Video element pauses (black screen, not last frame frozen)
   - 🔒 VIDEO FEED REDACTED overlay fades in
   - Reason shown: `releasability_mismatch`
   - Classification badge clears
4. When the drone returns to CAN_BASE: overlay disappears, video resumes from the new segment

### Scenario E — Side-by-Side Comparison (the full demo)

Open all three operator tabs simultaneously (use a window manager or browser split-screen):

| Tab | URL |
|---|---|
| Alice | http://localhost:8090?user=alice |
| Bob | http://localhost:8090?user=bob |
| Dave | http://localhost:8090?user=dave |

**Narrative checkpoints:**

| Mission moment | Alice | Bob | Dave |
|---|---|---|---|
| T+0: Takeoff | Track + Video | Track + Video | Nothing |
| T+30: Enters Exercise Corridor | Track + Video | **Feed goes dark** | Nothing |
| T+120: Over UK Base | Track + Video | Nothing | Nothing |
| T+180: Over Target | Track + Video | Nothing | Nothing |
| T+240: Enters Transit (return) | Track + Video | Nothing | **Feed appears** |
| T+300: Approaching CAN Base | Track + Video | Nothing | Track + Video |
| T+330: Landing | Track + Video | **Feed resumes** | Nothing (SECRET zone again) |

### Scenario F — Audit Log Verification

1. Open http://localhost:4775/audit during the mission
2. **Expected entries:**
   - Alice: all `ALLOW`, reason `policy_allow`
   - Bob: `ALLOW` for early CAN_BASE frames, then `DENY` with `releasability_mismatch`
   - Dave: `DENY` with `clearance_insufficient` for outbound leg, `ALLOW` for transit return
3. Each entry should include: `object_id`, `subject`, `decision`, `reason`, `timestamp`, `zone` (from SIG-008 metadata)

### Scenario G — Unit Tests (no Signet required)

```bash
cd iron-veil-demo
bash tests/run_tests.sh
```

Tests KLV encoder and mission path logic offline. Expected output:

```
=== Unit tests (no Signet required) ===
tests/test_klv_encoder.py    35 passed
tests/test_mission.py        31 passed
```

### Scenario H — Integration Tests (requires live Signet)

```bash
cd iron-veil-demo
bash tests/run_tests.sh --all
```

Tests the full pipeline: ingest → SSE → unwrap → policy check for Alice, Bob, and Dave. Signet must be running at `http://localhost:4774`.

### Scenario I — Video Pipeline Test (FFmpeg)

Test the video segment generator independently:

```bash
cd iron-veil-demo/services/drone-sim
VIDEO_SOURCE_PATH="../../tests/fixtures/aerial.mp4" python - <<'EOF'
from video_generator import generate_ts_segment
from klv_encoder import encode_st0601_frame

klv = encode_st0601_frame(lat=51.25, lon=-0.50, alt_m=500, heading=90, speed_ms=50)
meta = {"zone": "CAN_BASE", "classification": "SECRET", "lat": 51.25, "lon": -0.50,
        "alt_m": 500, "mission_time_s": 42, "frame_seq": 0}
seg = generate_ts_segment(meta, klv, duration_s=2.0)
print(f"Segment: {len(seg):,} bytes")
open("/tmp/test_seg.ts", "wb").write(seg)
print("Written to /tmp/test_seg.ts — open in VLC to verify")
EOF
```

Open `/tmp/test_seg.ts` in VLC to verify: you should see 2 seconds of aerial footage with the HUD overlay (classification, lat/lon, zone, mission time).

### Scenario J — Sample Data Generation (offline, no services needed)

Generate pre-baked mission fixtures for verification or offline testing:

```bash
cd iron-veil-demo
python tests/generate_sample_data.py
```

Output in `tests/fixtures/`:
- `mission_recording.jsonl` — 2,625 frame records for replay
- `mission_klv.ts` — raw MPEG-TS with all KLV frames concatenated
- `mission_summary.json` — zone breakdown and per-operator visibility counts

Expected summary:
```
Operator    Visible   Redacted      %
--------------------------------------
alice          2625          0  100.0%
bob              42       2583    1.6%
dave           2112        513   80.5%
```

---

## 5. Checking the Video Feed

### Video plays but HUD text is missing
FFmpeg `drawtext` requires font files. Inside the container, `libfreetype` is installed with FFmpeg. If running locally (not in Docker), install `fontconfig`:

```bash
# Debian/Ubuntu
apt-get install -y fontconfig

# macOS
brew install fontconfig
```

Alternatively, strip drawtext filters from `video_generator.py` for quick testing.

### Video section is black / not playing
1. Open browser DevTools → Console — check for MSE errors
2. Check that `mpegts.js` loaded: `window.mpegts` should be defined in console
3. Check that `video/mp2t` is supported: `MediaSource.isTypeSupported('video/mp2t')` → should return `true` in Chrome/Edge
4. Firefox does not support `video/mp2t` natively — use Chrome or Edge

### Video plays but goes immediately dark
This is correct behavior for Bob and Dave when the drone is in a zone they cannot access. If Alice's video is also going dark, check:
- Signet JWT is being issued correctly: open DevTools → Network → look for `POST /signet/token`
- Signet unwrap is returning 200 for Alice: look for `GET /signet/unwrap/{id}` responses

### Looping footage shows a jump at the loop point
This is normal for the 9.6-second `aerial.mp4` clip. In production, use a longer clip or a seamlessly looping source. The FFmpeg `-stream_loop -1` flag loops the input indefinitely at the mux level, so the loop cut is clean but the scene may jump.

---

## 6. Configuration Reference

All settings are in `deploy/docker-compose.yml` under the `drone-sim` service.

| Variable | Default | Effect |
|---|---|---|
| `FRAME_INTERVAL` | `1.0` | Seconds between frames. Lower = faster mission, more load |
| `SEGMENT_DURATION_S` | `2.0` | MPEG-TS segment duration in seconds |
| `VIDEO_ENABLED` | `true` | Set `false` to skip FFmpeg (KLV-only, faster startup) |
| `VIDEO_SOURCE_PATH` | `/footage/aerial.mp4` | Path to footage file inside container. Unset for synthetic lavfi |
| `BULK_SIZE` | `1` | Envelopes per Signet bulk-ingest call |
| `MISSION_ID` | `FVEX-26` | Mission identifier embedded in all frames |
| `DRONE_ID` | `UAS-001` | Drone identifier embedded in all frames |

---

## 7. Known Limitations (Demo)

- **Firefox**: `video/mp2t` MSE is not supported. Use Chrome or Edge.
- **Segment discontinuity**: when the drone enters a redacted zone and returns, the video resumes on the next segment boundary — there may be a 1-2 second gap before the first authorized segment arrives.
- **Loop cut**: the 9.6-second aerial.mp4 clip loops visibly. Replace with a longer clip or use `VIDEO_SOURCE_PATH=` (empty) to fall back to synthetic lavfi, which has no loop.
- **Mission speed**: at `FRAME_INTERVAL=1.0` the full 2,625-frame mission takes 44 minutes. For demos, set `FRAME_INTERVAL=0.1` (10 fps ingest) to complete in ~4 minutes, or use `--replay` with a pre-recorded file.
