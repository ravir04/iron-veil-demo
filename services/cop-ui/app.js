/**
 * Iron-Veil Demo — COP-UI
 *
 * Layout: map (left) | video feed + telemetry log (right)
 *
 * Data sources:
 *   GET /signet/stream/objects                       — SSE, JWT-authed (SIG-003)
 *   GET /signet/unwrap/{id}                          — per-frame metadata + labels (SIG-002)
 *   GET /signet/stream/fmv/{mission_id}/{drone_id}   — continuous MPEG-TS, policy-gated (SIG-010)
 *
 * Video playback (SIG-010):
 *   A single long-lived chunked HTTP connection to /signet/stream/fmv streams
 *   continuous MPEG-TS (H.264+KLV) directly to an MSE SourceBuffer. Signet
 *   evaluates policy per-GOP and drops denied chunks. The SSE channel (SIG-003)
 *   is kept for redaction overlay signalling — when Signet sends a "denied"
 *   notification the UI shows the lock overlay even though the video stream
 *   itself simply has a gap. mpegts.js handles MSE + MIME feature detection.
 */

// ---------------------------------------------------------------------------
// Demo controls — restart mission / change speed via control sidecar
// ---------------------------------------------------------------------------
function _setSpeedActive(interval) {
  const map = { 2.0: 'slow', 1.0: 'normal', 0.5: 'fast', 0.25: 'faster' };
  ['slow','normal','fast','faster'].forEach(k => {
    document.getElementById(`demo-btn-${k}`)?.classList.remove('active');
  });
  const key = map[interval];
  if (key) document.getElementById(`demo-btn-${key}`)?.classList.add('active');
}

function _resetMissionState() {
  trackPoints.length = 0;
  seenIds.clear();
  cntTotal = cntAllow = cntDeny = 0;
  _lastZone = null;
  _lastAllowed = null;
  updateTimeline(0);
  document.getElementById('cc-allow').textContent = '0';
  document.getElementById('cc-deny').textContent  = '0';
  const statusEl = document.getElementById('cc-status');
  if (statusEl) { statusEl.textContent = 'AWAITING DATA'; statusEl.style.color = ''; }
  renderMap();
}

async function demoSpeed(interval) {
  _setSpeedActive(interval);
  try {
    await fetch(`/demo/speed/${interval}`, { method: 'POST' });
    _resetMissionState();
  } catch {}
}

async function demoRestart() {
  try {
    await fetch('/demo/restart', { method: 'POST' });
    _resetMissionState();
  } catch {}
}

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
const params     = new URLSearchParams(location.search);
const USER       = params.get('user')   || 'alice';
// Signet is proxied through nginx at /signet/ — same origin, no CORS.
// Override with ?signet=http://... only when running outside docker.
const SIGNET_URL = params.get('signet') || '';

// ---------------------------------------------------------------------------
// Operator display profiles (policy enforced server-side by Signet OPA)
// ---------------------------------------------------------------------------
const OPERATORS = {
  alice: { name: 'Alice', cls: 'SECRET',    rels: 'CAN · FVEY · GBR · USA · NATO · AUS · NZL' },
  bob:   { name: 'Bob',   cls: 'SECRET',    rels: 'CAN' },
  dave:  { name: 'Dave',  cls: 'PROTECTED', rels: 'CAN · FVEY' },
};
const op = OPERATORS[USER] || OPERATORS['alice'];

// ---------------------------------------------------------------------------
// Zone config — must mirror mission.py
// ---------------------------------------------------------------------------
const ZONE_CFG = {
  CAN_BASE:          { colour: '#4488ff', label: 'CAN BASE',  shape: 'circle', radiusKm: 1.0,  lat: 51.2500, lon: -0.5000 },
  UK_BASE:           { colour: '#ff4488', label: 'UK BASE',   shape: 'circle', radiusKm: 1.0,  lat: 51.2700, lon: -0.2000 },
  TARGET_AREA:       { colour: '#ff2222', label: 'TARGET',    shape: 'circle', radiusKm: 2.0,  lat: 51.3000, lon:  0.2500 },
  EXERCISE_CORRIDOR: { colour: '#ff8800', label: 'EXERCISE',  shape: 'rect' },
  TRANSIT:           { colour: '#22aa44', label: 'TRANSIT',   shape: null },
  UNKNOWN:           { colour: '#666',    label: '',           shape: null },
};

const CORRIDOR_BOX = { latMin: 51.24, latMax: 51.32, lonMin: -0.40, lonMax: 0.32 };
const MAP_BOUNDS   = { latMin: 51.18, latMax: 51.42, lonMin: -0.65, lonMax: 0.45 };

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------
let jwt          = null;
let trackPoints  = [];
let seenIds      = new Set();  // deduplicate backfill vs SSE
let cntTotal = 0, cntAllow = 0, cntDeny = 0;
let missionStart = null;
let focusPoint   = null;   // { lat, lon } — set by clicking a telemetry entry

function clsColour(cls) {
  if (cls === 'SECRET')    return '#f85149';   // red
  if (cls === 'PROTECTED') return '#d29922';   // amber
  return '#3fb950';                            // green (UNCLASS)
}

// ---------------------------------------------------------------------------
// JWT
// ---------------------------------------------------------------------------
async function getJWT() {
  try {
    const r = await fetch(`${SIGNET_URL}/signet/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ grant_type: 'password', client_id: 'signet-cli', username: USER, password: 'password' }),
    });
    if (r.ok) return (await r.json()).access_token || null;
  } catch {}
  return null;
}

// ---------------------------------------------------------------------------
// Signet: metadata unwrap (SIG-002) — policy check (labels only)
// ---------------------------------------------------------------------------
async function unwrap(objectId) {
  const headers = jwt ? { Authorization: `Bearer ${jwt}` } : {};
  try {
    const r = await fetch(`${SIGNET_URL}/signet/unwrap/${objectId}`, { headers });
    if (r.ok)             return { allowed: true,  data: await r.json() };
    if (r.status === 403) return { allowed: false, reason: (await r.json().catch(() => ({}))).reason || 'denied' };
    return { allowed: false, reason: `http_${r.status}` };
  } catch {
    return { allowed: false, reason: 'network_error' };
  }
}

// ---------------------------------------------------------------------------
// Fetch mission metadata (zone, lat, lon, alt_m, etc.) for an object.
// The SSE event doesn't include metadata — fetch it from the objects listing
// using the ingest_ts as an anchor (returns the object at that timestamp).
// ---------------------------------------------------------------------------
async function fetchObjectMeta(objectId, ingestTs) {
  try {
    // Use since=(ingest_ts - 2000ms) to capture the frame in a small window.
    const since = Math.floor(ingestTs) - 2000;
    const r = await fetch(`${SIGNET_URL}/signet/objects?since=${since}&limit=20`);
    if (!r.ok) return null;
    const body = await r.json();
    const item = (body.items || []).find(i => i.object_id === objectId);
    return item?.metadata || null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Signet: continuous FMV stream (SIG-010) — long-lived MPEG-TS over HTTP
//
// Opens a single chunked connection to /signet/stream/fmv/{mission}/{drone}.
// Reads the response body as a ReadableStream and pushes chunks directly into
// the MSE SourceBuffer as they arrive. Signet gates each GOP by policy; denied
// GOPs are simply absent from the stream (the SSE channel signals the gap).
//
// Returns a controller object with .abort() to close the stream.
// ---------------------------------------------------------------------------
function connectFMVStream(missionId, droneId) {
  const headers = jwt ? { Authorization: `Bearer ${jwt}` } : {};
  const url = `${SIGNET_URL}/signet/stream/fmv/${encodeURIComponent(missionId)}/${encodeURIComponent(droneId)}`;
  const controller = new AbortController();

  (async () => {
    let backoff = 1000;
    while (!controller.signal.aborted) {
      try {
        const r = await fetch(url, { headers, signal: controller.signal });
        if (!r.ok || !r.body) {
          await new Promise(res => setTimeout(res, backoff));
          backoff = Math.min(backoff * 2, 30000);
          continue;
        }
        backoff = 1000;
        const reader = r.body.getReader();
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          if (value && value.byteLength > 0) feedSegment(value.buffer);
        }
      } catch (e) {
        if (controller.signal.aborted) break;
        await new Promise(res => setTimeout(res, backoff));
        backoff = Math.min(backoff * 2, 30000);
      }
    }
  })();

  return controller;
}

// ---------------------------------------------------------------------------
// MPEG-TS player — MSE SourceBuffer fed with segments from Signet SIG-006
//
// We push ArrayBuffer segments directly into the MSE SourceBuffer as they
// arrive from Signet. mpegts.js is used for MIME type negotiation and
// browser compatibility detection; actual demuxing is handled by the
// native browser MSE pipeline (H.264 in video/mp2t).
//
// Segment queue ensures we never call appendBuffer while the SourceBuffer
// is still processing a previous segment (updating === true).
// ---------------------------------------------------------------------------
const videoEl   = document.getElementById('video-el');
const _segQueue = [];
let   _sourceBuffer = null;
let   _mseReady     = false;

function initPlayer() {
  if (!('MediaSource' in window)) {
    console.warn('[cop-ui] MediaSource API not available');
    return;
  }

  // Check mpegts.js loaded (optional — used for feature detection)
  if (window.mpegts && !mpegts.isSupported()) {
    console.warn('[cop-ui] mpegts.js reports MSE not supported');
  }

  const ms = new MediaSource();
  videoEl.src = URL.createObjectURL(ms);

  ms.addEventListener('sourceopen', () => {
    try {
      // IANA MIME for MPEG-TS; H.264 baseline (avc1.42E01E = profile 66, level 30)
      _sourceBuffer = ms.addSourceBuffer('video/mp2t; codecs="avc1.42E01E"');
    } catch {
      // Fallback: try without explicit codec
      try { _sourceBuffer = ms.addSourceBuffer('video/mp2t'); } catch {}
    }
    if (!_sourceBuffer) return;

    // Set mode once — never touch it again after this point.
    try { _sourceBuffer.mode = 'sequence'; } catch {}
    _sourceBuffer.addEventListener('updateend', _drainQueue);
    _mseReady = true;
    _drainQueue();
  });

  ms.addEventListener('sourceclose', () => { _mseReady = false; });
  ms.addEventListener('sourceended', () => { _mseReady = false; });
}

function _drainQueue() {
  if (!_mseReady || !_sourceBuffer || _sourceBuffer.updating || _segQueue.length === 0) return;
  const seg = _segQueue.shift();
  try {
    _sourceBuffer.appendBuffer(seg);
  } catch (e) {
    // QuotaExceededError: evict old buffered data and retry
    if (e.name === 'QuotaExceededError' && _sourceBuffer.buffered.length > 0) {
      const end   = _sourceBuffer.buffered.end(0);
      const start = _sourceBuffer.buffered.start(0);
      if (end - start > 30) {
        _sourceBuffer.remove(start, end - 30);
        _segQueue.unshift(seg);
        return;
      }
    }
    // Any other error (InvalidStateError etc): discard segment and log once
    if (e.name !== 'InvalidStateError') console.warn('[cop-ui] appendBuffer error:', e.name);
  }
}

function feedSegment(arrayBuffer) {
  if (!_sourceBuffer) return;
  _segQueue.push(arrayBuffer);
  _drainQueue();
  if (videoEl.paused && videoEl.readyState >= 2) {
    videoEl.play().catch(() => {});
  }
}

// ---------------------------------------------------------------------------
// Video overlay HUD
// ---------------------------------------------------------------------------
function updateVideoOverlay(meta) {
  document.getElementById('ov-lat').textContent = meta.lat ?? '–';
  document.getElementById('ov-lon').textContent = meta.lon ?? '–';
  document.getElementById('ov-alt').textContent = Math.round(meta.alt_m ?? meta.metadata?.alt_m ?? 0);
  document.getElementById('ov-hdg').textContent = '–';
  document.getElementById('ov-spd').textContent = '–';

  const cls   = meta.classification || meta.labels?.cls || '?';
  const badge = document.getElementById('video-cls-badge');
  badge.textContent = cls;
  badge.style.background = cls === 'SECRET' ? '#3a0a0a' : cls === 'PROTECTED' ? '#3a2a00' : '#0a1a0a';
  badge.style.color      = cls === 'SECRET' ? '#f85149' : cls === 'PROTECTED' ? '#d29922' : '#3fb950';
  badge.style.border     = `1px solid ${badge.style.color}`;
}

function showVideoRedacted(reason) {
  // Stop feeding new segments — the existing buffer drains and the video
  // naturally freezes on the last frame, then stalls. We accelerate that
  // by pausing immediately and clearing the source so the screen goes black.
  videoEl.pause();
  // Drain the segment queue so buffered authorized frames don't leak through
  _segQueue.length = 0;

  document.getElementById('video-redacted').classList.add('visible');
  document.getElementById('redact-reason').textContent = reason || 'ACCESS DENIED';
  const badge = document.getElementById('video-cls-badge');
  badge.textContent = '';
  badge.style.background = '';
  badge.style.border = '';
}

function hideVideoRedacted() {
  document.getElementById('video-redacted').classList.remove('visible');
  // Resume playback — next feedSegment() call will restart it
  if (videoEl.paused && videoEl.readyState >= 2) {
    videoEl.play().catch(() => {});
  }
}

// ---------------------------------------------------------------------------
// Map canvas
// ---------------------------------------------------------------------------
const mapCanvas = document.getElementById('map');
const mctx      = mapCanvas.getContext('2d');

function resizeMap() {
  const wrap = document.getElementById('map-wrap');
  mapCanvas.width  = wrap.clientWidth;
  mapCanvas.height = wrap.clientHeight;
  renderMap();
}

function project(lat, lon) {
  const x = (lon - MAP_BOUNDS.lonMin) / (MAP_BOUNDS.lonMax - MAP_BOUNDS.lonMin) * mapCanvas.width;
  const y = (1 - (lat - MAP_BOUNDS.latMin) / (MAP_BOUNDS.latMax - MAP_BOUNDS.latMin)) * mapCanvas.height;
  return { x, y };
}

function kmToPixels(km) {
  const midLat   = (MAP_BOUNDS.latMin + MAP_BOUNDS.latMax) / 2;
  const degPerKm = 1 / (111.0 * Math.cos(midLat * Math.PI / 180));
  const p1 = project(midLat, MAP_BOUNDS.lonMin);
  const p2 = project(midLat, MAP_BOUNDS.lonMin + degPerKm * km);
  return Math.abs(p2.x - p1.x);
}

function renderMap() {
  const W = mapCanvas.width, H = mapCanvas.height;
  mctx.clearRect(0, 0, W, H);

  const grad = mctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, '#080f08');
  grad.addColorStop(1, '#0d150d');
  mctx.fillStyle = grad;
  mctx.fillRect(0, 0, W, H);

  mctx.strokeStyle = '#141e14';
  mctx.lineWidth = 1;
  for (let lat = 51.18; lat <= 51.42; lat += 0.02) {
    const { y } = project(lat, 0);
    mctx.beginPath(); mctx.moveTo(0, y); mctx.lineTo(W, y); mctx.stroke();
    mctx.fillStyle = '#1e2a1e'; mctx.font = '9px Courier New';
    mctx.fillText(lat.toFixed(2) + '°N', 4, y - 2);
  }
  for (let lon = -0.60; lon <= 0.40; lon += 0.20) {
    const { x } = project(0, lon);
    mctx.beginPath(); mctx.moveTo(x, 0); mctx.lineTo(x, H); mctx.stroke();
    mctx.fillStyle = '#1e2a1e'; mctx.font = '9px Courier New';
    mctx.fillText((lon >= 0 ? '+' : '') + lon.toFixed(2) + '°', x + 3, H - 4);
  }

  drawCorridorRect();
  drawBaseRect('CAN_BASE');
  drawBaseRect('UK_BASE');
  drawZoneCircle('CAN_BASE');
  drawZoneCircle('UK_BASE');
  drawZoneCircle('TARGET_AREA');
  drawMissionGhost();

  // Draw track lines — classification-colored for allowed, dashed red for denied
  for (let i = 1; i < trackPoints.length; i++) {
    const a = trackPoints[i - 1], b = trackPoints[i];
    const pa = project(a.lat, a.lon), pb = project(b.lat, b.lon);
    if (!b.allowed) {
      // Denied segment — dashed red to show "drone was here but access denied"
      mctx.strokeStyle = '#f85149';
      mctx.lineWidth   = 1.5;
      mctx.setLineDash([4, 4]);
      mctx.globalAlpha = 0.5;
      mctx.beginPath(); mctx.moveTo(pa.x, pa.y); mctx.lineTo(pb.x, pb.y); mctx.stroke();
      mctx.setLineDash([]);
      mctx.globalAlpha = 1;
    } else {
      mctx.strokeStyle = clsColour(b.cls);
      mctx.lineWidth   = 2;
      mctx.beginPath(); mctx.moveTo(pa.x, pa.y); mctx.lineTo(pb.x, pb.y); mctx.stroke();
    }
  }

  // Draw track dots — classification-colored for allowed, dim red X for denied
  for (const pt of trackPoints) {
    const { x, y } = project(pt.lat, pt.lon);
    if (!pt.allowed) {
      mctx.strokeStyle = '#f85149';
      mctx.lineWidth   = 1.5;
      mctx.globalAlpha = 0.4;
      mctx.beginPath();
      mctx.moveTo(x - 3, y - 3); mctx.lineTo(x + 3, y + 3);
      mctx.moveTo(x + 3, y - 3); mctx.lineTo(x - 3, y + 3);
      mctx.stroke();
      mctx.globalAlpha = 1;
    } else {
      mctx.fillStyle = clsColour(pt.cls);
      mctx.beginPath(); mctx.arc(x, y, 3, 0, Math.PI * 2); mctx.fill();
    }
  }

  // Zone transition labels — annotate where zone changes along the track
  let lastZone = null;
  for (const pt of trackPoints) {
    if (!pt.allowed || pt.zone === lastZone) { lastZone = pt.zone; continue; }
    if (lastZone !== null) {
      const { x, y } = project(pt.lat, pt.lon);
      const cfg = ZONE_CFG[pt.zone];
      mctx.fillStyle = cfg?.colour || '#888';
      mctx.font = '9px Courier New';
      mctx.textAlign = 'left';
      mctx.fillText('→ ' + (cfg?.label || pt.zone), x + 7, y - 4);
    }
    lastZone = pt.zone;
  }

  const latest = [...trackPoints].reverse().find(p => p.allowed);
  if (latest) {
    const { x, y } = project(latest.lat, latest.lon);
    const glow = mctx.createRadialGradient(x, y, 2, x, y, 14);
    glow.addColorStop(0, 'rgba(255,255,255,0.3)');
    glow.addColorStop(1, 'rgba(255,255,255,0)');
    mctx.fillStyle = glow;
    mctx.beginPath(); mctx.arc(x, y, 14, 0, Math.PI * 2); mctx.fill();
    mctx.fillStyle = '#fff';
    mctx.beginPath(); mctx.arc(x, y, 5, 0, Math.PI * 2); mctx.fill();
    mctx.strokeStyle = '#fff'; mctx.lineWidth = 1.5;
    mctx.beginPath(); mctx.arc(x, y, 9, 0, Math.PI * 2); mctx.stroke();
  }

  // Focus highlight — yellow ring when user clicks a telemetry entry
  if (focusPoint) {
    const { x, y } = project(focusPoint.lat, focusPoint.lon);
    const t = (Date.now() % 1200) / 1200;
    const pulse = 14 + 6 * Math.sin(t * Math.PI * 2);
    mctx.strokeStyle = '#ffd700';
    mctx.lineWidth   = 2;
    mctx.globalAlpha = 0.85;
    mctx.beginPath(); mctx.arc(x, y, pulse, 0, Math.PI * 2); mctx.stroke();
    mctx.globalAlpha = 1;
    mctx.fillStyle   = '#ffd700';
    mctx.beginPath(); mctx.arc(x, y, 5, 0, Math.PI * 2); mctx.fill();
  }
}

// Animate focus pulse — only runs while focusPoint is set
let _focusAnimId = null;
function _startFocusAnim() {
  if (_focusAnimId) return;
  function frame() {
    if (!focusPoint) { _focusAnimId = null; return; }
    renderMap();
    _focusAnimId = requestAnimationFrame(frame);
  }
  _focusAnimId = requestAnimationFrame(frame);
}

function focusMapAt(lat, lon) {
  focusPoint = { lat, lon };
  _startFocusAnim();
  // Clear focus after 3 seconds
  clearTimeout(focusMapAt._timer);
  focusMapAt._timer = setTimeout(() => {
    focusPoint = null;
    _focusAnimId = null;
    renderMap();
  }, 3000);
}

// Rectangular airfield footprint — drawn behind the geofence circle.
// Width/height in km; the circle radius is used as a rough guide.
function drawBaseRect(zoneName) {
  const cfg = ZONE_CFG[zoneName];
  if (!cfg || cfg.shape !== 'circle') return;
  // Airfield rectangle is 0.8× the geofence radius in each direction
  const halfKm = cfg.radiusKm * 0.7;
  const midLat = (MAP_BOUNDS.latMin + MAP_BOUNDS.latMax) / 2;
  const kmPerDegLat = 111.0;
  const kmPerDegLon = 111.0 * Math.cos(midLat * Math.PI / 180);
  const dLat = halfKm / kmPerDegLat;
  const dLon = halfKm / kmPerDegLon;
  const tl = project(cfg.lat + dLat, cfg.lon - dLon);
  const br = project(cfg.lat - dLat, cfg.lon + dLon);
  const w = br.x - tl.x, h = br.y - tl.y;
  mctx.fillStyle = cfg.colour + '22';
  mctx.fillRect(tl.x, tl.y, w, h);
  mctx.strokeStyle = cfg.colour;
  mctx.lineWidth = 1.5;
  mctx.setLineDash([]);
  mctx.strokeRect(tl.x, tl.y, w, h);
  // Runway centerline
  const cx = (tl.x + br.x) / 2, cy = (tl.y + br.y) / 2;
  mctx.strokeStyle = cfg.colour + '88';
  mctx.lineWidth = 1;
  mctx.beginPath();
  mctx.moveTo(cx, tl.y + 4); mctx.lineTo(cx, br.y - 4);
  mctx.stroke();
}

function drawZoneCircle(zoneName) {
  const cfg = ZONE_CFG[zoneName];
  if (!cfg || cfg.shape !== 'circle') return;
  const { x, y } = project(cfg.lat, cfg.lon);
  const r = kmToPixels(cfg.radiusKm);
  mctx.fillStyle = cfg.colour + '18';
  mctx.beginPath(); mctx.arc(x, y, r, 0, Math.PI * 2); mctx.fill();
  mctx.strokeStyle = cfg.colour;
  mctx.lineWidth   = 1.5;
  mctx.setLineDash([5, 4]);
  mctx.beginPath(); mctx.arc(x, y, r, 0, Math.PI * 2); mctx.stroke();
  mctx.setLineDash([]);
  mctx.fillStyle = cfg.colour;
  mctx.font = 'bold 10px Courier New';
  mctx.textAlign = 'center';
  mctx.fillText(cfg.label, x, y - r - 5);
  mctx.textAlign = 'left';
}

function drawCorridorRect() {
  const tl  = project(CORRIDOR_BOX.latMax, CORRIDOR_BOX.lonMin);
  const br  = project(CORRIDOR_BOX.latMin, CORRIDOR_BOX.lonMax);
  const w   = br.x - tl.x, h = br.y - tl.y;
  const col = ZONE_CFG.EXERCISE_CORRIDOR.colour;
  mctx.fillStyle = col + '12';
  mctx.fillRect(tl.x, tl.y, w, h);
  mctx.strokeStyle = col;
  mctx.lineWidth   = 1.5;
  mctx.setLineDash([6, 5]);
  mctx.strokeRect(tl.x, tl.y, w, h);
  mctx.setLineDash([]);
  mctx.fillStyle = col;
  mctx.font = '10px Courier New';
  mctx.fillText('EXERCISE CORRIDOR', tl.x + 6, tl.y + 14);
}

function drawMissionGhost() {
  const waypoints = [
    { lat: 51.2500, lon: -0.5000, label: 'TAKEOFF' },
    { lat: 51.2700, lon: -0.2000, label: 'UK BASE' },
    { lat: 51.3000, lon:  0.2500, label: 'TARGET' },
    { lat: 51.3800, lon: -0.1500, label: 'RTB TRANSIT' },
    { lat: 51.2500, lon: -0.5000, label: null },  // return to CAN_BASE — label already shown
  ];
  mctx.strokeStyle = 'rgba(255,255,255,0.06)';
  mctx.lineWidth   = 1;
  mctx.setLineDash([3, 6]);
  mctx.beginPath();
  waypoints.forEach((wp, i) => {
    const { x, y } = project(wp.lat, wp.lon);
    i === 0 ? mctx.moveTo(x, y) : mctx.lineTo(x, y);
  });
  mctx.stroke();
  mctx.setLineDash([]);
  waypoints.forEach(wp => {
    const { x, y } = project(wp.lat, wp.lon);
    mctx.strokeStyle = 'rgba(255,255,255,0.15)';
    mctx.lineWidth = 1;
    mctx.beginPath();
    mctx.moveTo(x - 5, y); mctx.lineTo(x + 5, y);
    mctx.moveTo(x, y - 5); mctx.lineTo(x, y + 5);
    mctx.stroke();
    if (wp.label) {
      mctx.fillStyle = 'rgba(255,255,255,0.25)';
      mctx.font = '9px Courier New';
      mctx.textAlign = 'left';
      mctx.fillText(wp.label, x + 8, y + 3);
    }
  });
}

window.addEventListener('resize', resizeMap);

// ---------------------------------------------------------------------------
// Mission backfill — sync late-joining tabs to current mission state
//
// On tab open, fetches all objects ingested since this mission started
// (using mission_elapsed_s from /demo/status to compute the since timestamp).
// Runs each object through unwrap() and pushes track points silently —
// no telemetry log entries, no flash callouts — then renders the map.
// This makes all tabs show the same track regardless of when they were opened.
// ---------------------------------------------------------------------------
let _backfilling = false;

async function backfillMission() {
  let statusRes;
  try {
    const r = await fetch('/demo/status');
    if (!r.ok) return;
    statusRes = await r.json();
  } catch { return; }

  const elapsedS = statusRes.mission_elapsed_s ?? 0;
  if (elapsedS < 2) return; // mission just started, nothing to backfill

  // since = now - elapsed (ms), with a small buffer
  const sinceMs = Math.floor(Date.now() - elapsedS * 1000 - 2000);

  // Fetch all objects from this mission window (up to 500 frames = well above mission length)
  let items;
  try {
    const r = await fetch(`${SIGNET_URL}/signet/objects?since=${sinceMs}&limit=200`);
    if (!r.ok) return;
    const body = await r.json();
    items = body.items || [];
  } catch { return; }

  if (items.length === 0) return;

  // Sort oldest-first by ingest_ts (API returns newest-first)
  items.sort((a, b) => a.ingest_ts - b.ingest_ts);

  _backfilling = true;
  for (const item of items) {
    const meta = item.metadata;
    if (!meta?.lat || !meta?.lon) continue;
    seenIds.add(item.object_id);

    const result = await unwrap(item.object_id);
    const cls    = item.labels?.classification || item.labels?.cls || '?';
    const zone   = meta.zone || 'UNKNOWN';

    if (result.allowed) {
      trackPoints.push({ lat: meta.lat, lon: meta.lon, zone, cls, allowed: true, ts: item.ingest_ts });
    } else if (trackPoints.length > 0) {
      const last = trackPoints[trackPoints.length - 1];
      trackPoints.push({ lat: last.lat, lon: last.lon, zone, cls, allowed: false, ts: item.ingest_ts });
    }
  }
  _backfilling = false;

  // If the most recent frame was denied, show the redaction overlay immediately
  // so Dave doesn't see an unredacted video panel while waiting for the next SSE event.
  if (trackPoints.length > 0 && !trackPoints[trackPoints.length - 1].allowed) {
    showVideoRedacted('ACCESS DENIED — CLEARANCE INSUFFICIENT');
  }

  renderMap();
}

// ---------------------------------------------------------------------------
// Telemetry log
// ---------------------------------------------------------------------------
function addTelemEntry(meta, allowed, reason) {
  const log = document.getElementById('telem-log');
  const el  = document.createElement('div');
  el.className = `telem-entry ${allowed ? 'allow' : 'deny'}`;
  const zone = meta.zone || meta.metadata?.zone || '?';
  const cls  = meta.classification || meta.labels?.cls || '?';
  const ts   = new Date((meta.ingest_ts || meta.ts || Date.now())).toLocaleTimeString();
  const t    = meta.mission_time_s ?? meta.metadata?.mission_time_s ?? '?';
  const rels = (meta.releasability || meta.labels?.releasability || []).join(', ');
  const lat  = meta.lat ?? meta.metadata?.lat ?? null;
  const lon  = meta.lon ?? meta.metadata?.lon ?? null;

  if (allowed) {
    el.innerHTML = `
      <div class="t-zone">
        <span>${zone}</span>
        <span class="cls-${cls}">${cls}</span>
      </div>
      <div class="t-meta">${ts} &nbsp;T+${t}s</div>
      <div class="t-meta">${lat ?? '?'}, ${lon ?? '?'} &nbsp;|&nbsp; ${rels}</div>`;
  } else {
    el.innerHTML = `
      <div class="t-zone">
        <span class="redacted-text">[REDACTED]</span>
        <span class="cls-${cls}">${cls}</span>
      </div>
      <div class="t-meta">${ts} &nbsp;|&nbsp; ${reason}</div>`;
  }

  // Click to focus map on this frame's position
  if (lat != null && lon != null) {
    el.title = 'Click to focus map';
    el.style.cursor = 'pointer';
    el.addEventListener('click', () => focusMapAt(lat, lon));
  }

  log.insertBefore(el, log.firstChild);
  while (log.children.length > 60) log.removeChild(log.lastChild);
}

// ---------------------------------------------------------------------------
// Event flash — large map overlay for key demo moments
// ---------------------------------------------------------------------------
let _flashTimer = null;

function showEventFlash(title, sub, colour) {
  const el    = document.getElementById('event-flash');
  const tEl   = document.getElementById('ef-title');
  const subEl = document.getElementById('ef-sub');
  tEl.textContent  = title;
  tEl.style.color  = colour || '#fff';
  subEl.textContent = sub || '';
  // Restart animation by removing and re-adding .visible
  el.classList.remove('visible');
  void el.offsetWidth; // force reflow
  el.classList.add('visible');
  clearTimeout(_flashTimer);
  _flashTimer = setTimeout(() => el.classList.remove('visible'), 3000);
}

// ---------------------------------------------------------------------------
// Clearance card — commander-facing status panel
// ---------------------------------------------------------------------------
let _lastZone    = null;
let _lastAllowed = null;

function initClearanceCard() {
  const clsBg   = op.cls === 'SECRET'    ? '#3a0a0a' : op.cls === 'PROTECTED' ? '#3a2a00' : '#0a1a0a';
  const clsCol  = op.cls === 'SECRET'    ? '#f85149' : op.cls === 'PROTECTED' ? '#d29922' : '#3fb950';
  const badge   = document.getElementById('cc-cls-badge');
  badge.textContent       = op.cls;
  badge.style.background  = clsBg;
  badge.style.color       = clsCol;
  badge.style.border      = `1px solid ${clsCol}`;
  document.getElementById('cc-name').textContent = op.name.toUpperCase();
  document.getElementById('cc-rels').textContent = op.rels;
}

function updateClearanceCard(zone, allowed, reason) {
  document.getElementById('cc-allow').textContent = cntAllow;
  document.getElementById('cc-deny').textContent  = cntDeny;

  const statusEl = document.getElementById('cc-status');
  const zoneCfg  = ZONE_CFG[zone] || ZONE_CFG.UNKNOWN;

  if (allowed) {
    statusEl.textContent  = `AUTHORIZED — ${zoneCfg.label || zone}`;
    statusEl.style.color  = '#3fb950';
  } else {
    const why = reason === 'releasability_mismatch' ? 'RELEASABILITY MISMATCH'
              : reason === 'clearance_insufficient' ? 'CLEARANCE INSUFFICIENT'
              : reason ? reason.toUpperCase().replace(/_/g, ' ')
              : 'ACCESS DENIED';
    statusEl.textContent  = `DENIED — ${why}`;
    statusEl.style.color  = '#f85149';
  }

  // Trigger flash on first denial or zone change
  const zoneChanged    = zone !== _lastZone && _lastZone !== null;
  const accessChanged  = allowed !== _lastAllowed && _lastAllowed !== null;

  if (!_backfilling) {
    if (accessChanged && !allowed) {
      showEventFlash('ACCESS REVOKED', reason?.toUpperCase().replace(/_/g, ' ') || 'POLICY DENIED', '#f85149');
    } else if (accessChanged && allowed) {
      showEventFlash('ACCESS GRANTED', zoneCfg.label || zone, '#3fb950');
    } else if (zoneChanged && allowed) {
      showEventFlash(`→ ${(zoneCfg.label || zone).toUpperCase()}`, null, zoneCfg.colour || '#58a6ff');
    }
  }

  _lastZone    = zone;
  _lastAllowed = allowed;
}

// ---------------------------------------------------------------------------
// Mission timeline bar
// ---------------------------------------------------------------------------
// Milestones from measured mission path at 300 m/s (total ~370s).
// Fractions = mission_time_s / MISSION_DURATION_S.
//   T+5   exit CAN_BASE → TRANSIT
//   T+68  reach UK_BASE
//   T+75  EXERCISE_CORRIDOR
//   T+106 exit corridor → TRANSIT
//   T+170 TARGET_AREA
//   T+185 exit TARGET → TRANSIT (Dave's first frame)
//   T+368 return to CAN_BASE (Bob reappears)
const TIMELINE_MILESTONES = [
  { label: 'TAKEOFF',  frac: 0.00 },
  { label: 'UK BASE',  frac: 0.18 },   // T+68s
  { label: 'TARGET',   frac: 0.46 },   // T+170s
  { label: 'TRANSIT',  frac: 0.50 },   // T+185s  — Dave appears
  { label: 'RTB',      frac: 1.00 },   // T+373s
];
const MISSION_DURATION_S = 373;

function updateTimeline(missionTimeS) {
  const frac = Math.min(missionTimeS / MISSION_DURATION_S, 1.0);
  const pct  = (frac * 100).toFixed(1) + '%';
  document.getElementById('timeline-fill').style.width = pct;
  document.getElementById('timeline-head').style.left  = pct;
}

// Poll /demo/status every second to:
//   1. Drive the timeline bar from server wall-clock time (all tabs stay in sync)
//   2. Detect restarts initiated by any tab and auto-reset local state
//   3. Pick up speed changes set by any tab
//
// mission_elapsed_s (wall seconds since sim start) / interval = mission_time_s.
// At 4× (interval=0.25): 1 wall-second = 4 mission-seconds.
let _demoInterval  = 1.0;
let _lastElapsed   = 0;

function _startTimelinePoller() {
  setInterval(async () => {
    try {
      const r = await fetch('/demo/status');
      if (!r.ok) return;
      const s = await r.json();

      const newInterval = s.interval ?? 1.0;
      const elapsed     = s.mission_elapsed_s ?? 0;

      // Detect restart: elapsed went backwards (or dropped significantly).
      // Any tab's restart/speed-change will cause the server to reset mission_elapsed_s to 0.
      if (elapsed < _lastElapsed - 3) {
        // Mission was restarted — reset this tab's local state too
        _resetMissionState();
      }

      // Detect speed change from another tab — update speed button highlight
      if (newInterval !== _demoInterval) {
        _setSpeedActive(newInterval);
        _demoInterval = newInterval;
      }

      _lastElapsed = elapsed;

      const missionTimeS = elapsed / _demoInterval;
      updateTimeline(missionTimeS);
      document.getElementById('mission-timer').textContent = `${Math.round(missionTimeS)}s`;
    } catch {}
  }, 1000);
}

// ---------------------------------------------------------------------------
// Stats + footer
// ---------------------------------------------------------------------------
function updateStats(zone, allowed, reason) {
  updateClearanceCard(zone, allowed, reason);
}

function updateFooter(enriched) {
  if (!missionStart) missionStart = Date.now();
  const m = enriched.metadata;
  // Timeline and timer are driven by _startTimelinePoller() for cross-tab sync.
  // Only update mission/drone IDs here.
  if (m?.mission_id) document.getElementById('f-mission').textContent = m.mission_id;
  if (m?.drone_id)   document.getElementById('f-drone').textContent   = m.drone_id;
}

// ---------------------------------------------------------------------------
// SSE — implemented with fetch() so we can send Authorization header.
// EventSource does not support custom headers; fetch+ReadableStream does.
// Parses the `data: ...` lines from the text/event-stream format manually.
// ---------------------------------------------------------------------------
function connectSSE(onMessage) {
  const pill = document.getElementById('conn-pill');
  const url  = `${SIGNET_URL}/signet/stream/objects`;
  const controller = new AbortController();

  (async () => {
    let backoff = 1000;
    while (!controller.signal.aborted) {
      try {
        const headers = { Accept: 'text/event-stream' };
        if (jwt) headers['Authorization'] = `Bearer ${jwt}`;
        const r = await fetch(url, { headers, signal: controller.signal });
        if (!r.ok || !r.body) {
          pill.textContent = 'Reconnecting…'; pill.className = 'disconnected';
          await new Promise(res => setTimeout(res, backoff));
          backoff = Math.min(backoff * 2, 30000);
          continue;
        }
        pill.textContent = 'Connected'; pill.className = 'connected';
        backoff = 1000;

        const reader  = r.body.getReader();
        const decoder = new TextDecoder();
        let   buf     = '';
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop(); // keep incomplete last line
          for (const line of lines) {
            if (line.startsWith('data: ')) {
              try { onMessage({ data: line.slice(6) }); } catch {}
            }
          }
        }
      } catch (e) {
        if (controller.signal.aborted) break;
        pill.textContent = 'Reconnecting…'; pill.className = 'disconnected';
        await new Promise(res => setTimeout(res, backoff));
        backoff = Math.min(backoff * 2, 30000);
      }
    }
  })();

  return controller;
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

// Mission / drone IDs — must match drone-sim environment
const MISSION_ID = params.get('mission') || 'FVEX-26';
const DRONE_ID   = params.get('drone')   || 'UAS-001';

async function start() {
  document.getElementById('op-name').textContent = op.name;
  const clsEl = document.getElementById('op-cls');
  clsEl.textContent = op.cls;
  clsEl.className   = `cls cls-${op.cls}`;
  document.getElementById('op-rels').textContent = op.rels;

  initClearanceCard();
  _startTimelinePoller();

  resizeMap();
  window.addEventListener('resize', resizeMap);

  jwt = await getJWT();

  // Show redacted overlay by default — hideVideoRedacted() clears it only when
  // the operator receives their first authorized frame. This prevents a window
  // where a restricted user (e.g. Dave) sees unredacted video before policy is evaluated.
  showVideoRedacted('AWAITING AUTHORIZATION');

  // Backfill track history so late-joining tabs sync immediately to current mission state
  await backfillMission();

  // Initialise MSE pipeline for MPEG-TS playback
  initPlayer();

  // SIG-010: open a single long-lived FMV stream for continuous H.264+KLV video.
  // Signet gates each GOP by policy; the SSE channel below handles the overlay.
  connectFMVStream(MISSION_ID, DRONE_ID);

  // SIG-003: SSE for per-frame notifications — drives map, telemetry log, and
  // redaction overlay. Video bytes come from SIG-010, not from here.
  connectSSE(async (event) => {
    const notification = JSON.parse(event.data);

    // Skip secondary FMV redaction events (fmv_redacted, fmv_allowed) — these
    // have no ingest_ts/labels and are not new objects to process.
    if (notification.event) return;

    const { object_id, labels, ingest_ts } = notification;

    // Skip frames already processed during backfill
    if (seenIds.has(object_id)) return;
    seenIds.add(object_id);

    cntTotal++;

    const cls  = labels?.classification || labels?.cls || '?';
    const rels = labels?.releasability || [];

    // SSE event includes metadata inline — use it directly; no extra round-trip needed.
    const meta = notification.metadata || await fetchObjectMeta(object_id, ingest_ts);
    const zone = meta?.zone || 'UNKNOWN';
    const lat  = meta?.lat;
    const lon  = meta?.lon;

    // SIG-002: metadata unwrap — policy check (determines overlay state)
    const result = await unwrap(object_id);

    const enriched = { ...notification, zone, lat, lon, classification: cls, releasability: rels, metadata: meta };

    if (result.allowed) {
      cntAllow++;
      if (lat != null && lon != null) {
        trackPoints.push({ lat, lon, zone, cls, allowed: true, ts: notification.ingest_ts });
      }
      hideVideoRedacted();
      updateVideoOverlay(enriched);
      addTelemEntry(enriched, true, null);
    } else {
      cntDeny++;
      if (trackPoints.length > 0) {
        const last = trackPoints[trackPoints.length - 1];
        // Carry last known position so denied segment is still drawn
        if (last.lat != null && last.lon != null) {
          trackPoints.push({ lat: last.lat, lon: last.lon, zone, cls, allowed: false, ts: notification.ingest_ts });
        }
      }
      showVideoRedacted(result.reason);
      addTelemEntry(enriched, false, result.reason);
    }

    updateStats(zone, result.allowed, result.reason);
    updateFooter(enriched);
    renderMap();
  });
}

start();
