"""
STANAG 4609-conformant MPEG-TS video segment generator.

Produces real H.264 video + MISB ST0601 KLV muxed into a single MPEG-TS
segment using FFmpeg.

Two video source modes:
  1. Real footage (VIDEO_SOURCE_PATH set): loops aerial.mp4 (or any MP4) and
     overlays the MISB HUD (classification, lat/lon/alt, zone, timestamp).
     The clip is looped seamlessly regardless of its length — a 9-second
     clip works just as well as a 90-minute one.

  2. Synthetic lavfi (VIDEO_SOURCE_PATH unset): FFmpeg-generated EO/IR frames
     with zone-specific colour palette. No footage file required.

The "feed going dark" effect is handled in the browser (app.js): when Signet
denies the unwrap (SIG-006 → 403), the video element is paused and blanked
and the 🔒 REDACTED overlay is shown. No dark segment is ever sent — the
absence of data IS the darkness.

Video PID:  0x0041 (65)   — H.264 video
KLV PID:    0x0065 (101)  — MISB ST0601 metadata (STANAG 4609 §8.2)
PMT PID:    0x0020 (32)

Segment duration is configurable via SEGMENT_DURATION_S (default 2s).
  SEGMENT_DURATION_S=0.5  — low-latency production
  SEGMENT_DURATION_S=4.0  — high-efficiency production
"""

from __future__ import annotations
import os
import subprocess
import tempfile
import threading

SEGMENT_DURATION_S = float(os.environ.get("SEGMENT_DURATION_S", "2.0"))

# Path to a real aerial footage file (MP4/MKV/etc).
# If set and the file exists, footage is looped and HUD overlaid.
# If unset, synthetic lavfi frames are generated instead.
VIDEO_SOURCE_PATH = os.environ.get("VIDEO_SOURCE_PATH", "")

# Target output resolution. Real footage is scaled to fit; lavfi renders at this size.
# 1280x720 for demo (decent quality, manageable segment sizes ~150KB/2s).
# Use 640x360 for lower bandwidth, 1920x1080 for higher fidelity.
OUTPUT_WIDTH  = int(os.environ.get("VIDEO_WIDTH",  "1280"))
OUTPUT_HEIGHT = int(os.environ.get("VIDEO_HEIGHT", "720"))

# Zone-specific lavfi colour palettes — EO/IR synthetic camera look (fallback only)
_ZONE_PALETTE = {
    "CAN_BASE":          {"sky": "0x1a2a4a", "ground": "0x2a4a1a"},
    "UK_BASE":           {"sky": "0x3a1a3a", "ground": "0x3a1a1a"},
    "EXERCISE_CORRIDOR": {"sky": "0x2a1a0a", "ground": "0x3a2a0a"},
    "TARGET_AREA":       {"sky": "0x3a0a0a", "ground": "0x2a1a0a"},
    "TRANSIT":           {"sky": "0x0a1a2a", "ground": "0x1a2a1a"},
    "UNKNOWN":           {"sky": "0x111111", "ground": "0x111111"},
}

# Thread-local temp dir so parallel calls don't collide
_tl = threading.local()


def _tmpdir() -> str:
    if not hasattr(_tl, "tmpdir"):
        _tl.tmpdir = tempfile.mkdtemp(prefix="ivdemo_vid_")
    return _tl.tmpdir


def _safe(s: str) -> str:
    """Escape a string for use in an FFmpeg drawtext filter value."""
    return str(s).replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:").replace("%", "\\%")


def _hud_filters(meta: dict, W: int, H: int) -> str:
    """
    Build the chain of FFmpeg drawtext/drawbox filters that burn a MISB-style
    HUD into the video.  These are appended after the video source filter,
    whether that source is real footage or synthetic lavfi.

    HUD elements:
      - Classification badge (top-left, colour-coded)
      - Zone label (top-right)
      - LAT / LON / ALT coordinates (bottom-left)
      - Mission time (bottom-left, second line)
      - Recording indicator dot (top-right, blinks at ~1 Hz)
      - Thin black semi-transparent bar top and bottom for readability
    """
    cls   = meta.get("classification", "?")
    zone  = meta.get("zone", "?")
    lat   = meta.get("lat", 0.0)
    lon   = meta.get("lon", 0.0)
    alt   = int(meta.get("alt_m", 0))
    t_s   = int(meta.get("mission_time_s", 0))

    cls_colour = {"SECRET": "red", "PROTECTED": "yellow", "UNCLASS": "lime"}.get(cls, "white")
    fs = max(14, W // 80)   # font size scales with resolution

    # Build as a series of chained filters (each takes [prev] and outputs [next])
    filters = []

    # Semi-transparent bars top and bottom for HUD legibility
    filters.append(
        f"drawbox=x=0:y=0:w=iw:h={fs+10}:color=black@0.5:t=fill"
    )
    filters.append(
        f"drawbox=x=0:y=ih-{fs*2+14}:w=iw:h={fs*2+14}:color=black@0.5:t=fill"
    )

    # Classification (top-left)
    filters.append(
        f"drawtext=text='{_safe(cls)}':fontsize={fs}:fontcolor={cls_colour}"
        f":x=8:y=6:box=0"
    )

    # Zone label (top-right, after cls)
    filters.append(
        f"drawtext=text='EO\\/IR · {_safe(zone)}':fontsize={fs}:fontcolor=white@0.8"
        f":x=w-tw-8:y=6"
    )

    # Coordinates (bottom-left, first line)
    filters.append(
        f"drawtext=text='LAT {lat:.5f}  LON {lon:.5f}  ALT {alt}m'"
        f":fontsize={fs}:fontcolor=white@0.9:x=8:y=h-{fs*2+10}"
    )

    # Mission time (bottom-left, second line)
    filters.append(
        f"drawtext=text='IRON-VEIL FVEX-26 · T\\+{t_s:05d}s'"
        f":fontsize={fs}:fontcolor=yellow@0.85:x=8:y=h-{fs+6}"
    )

    # Blinking red dot top-right (recording indicator, blinks at ~1 Hz)
    filters.append(
        f"drawtext=text='⬤':fontsize={fs}:fontcolor=red@0.95"
        f":x=w-tw-{fs+10}:y=6:enable='mod(t\\,2)'[hud]"
    )

    # Join: first N-1 filters have no label on output, last has [hud]
    result = ""
    for i, f in enumerate(filters):
        if i < len(filters) - 1:
            result += f + ","
        else:
            result += f
    return result


def generate_ts_segment(
    meta: dict,
    klv_bytes: bytes,
    duration_s: float | None = None,
    width: int | None = None,
    height: int | None = None,
) -> bytes:
    """
    Generate a STANAG 4609-conformant MPEG-TS segment.

    Parameters
    ----------
    meta       : Frame metadata dict (zone, lat, lon, alt_m, classification, mission_time_s)
    klv_bytes  : Raw MISB ST0601 KLV payload (already encoded by klv_encoder.py)
    duration_s : Segment duration in seconds (default: SEGMENT_DURATION_S env var)
    width, height : Output resolution (default: OUTPUT_WIDTH / OUTPUT_HEIGHT env vars)

    Returns
    -------
    bytes : Complete MPEG-TS segment, H.264 video + ST0601 KLV, STANAG 4609 conformant.
    """
    if duration_s is None:
        duration_s = SEGMENT_DURATION_S
    if width is None:
        width = OUTPUT_WIDTH
    if height is None:
        height = OUTPUT_HEIGHT

    tmp      = _tmpdir()
    klv_path = os.path.join(tmp, f"klv_{meta.get('frame_seq', 0):06d}.bin")
    out_path = os.path.join(tmp, f"seg_{meta.get('frame_seq', 0):06d}.ts")

    with open(klv_path, "wb") as f:
        f.write(klv_bytes)

    src_path = VIDEO_SOURCE_PATH if VIDEO_SOURCE_PATH and os.path.exists(VIDEO_SOURCE_PATH) else None

    hud = _hud_filters(meta, width, height)

    if src_path:
        # ── Real footage mode ─────────────────────────────────────────────
        # Input 0: aerial.mp4 looped indefinitely (-stream_loop -1)
        # Input 1: KLV binary data (also looped)
        # Filtergraph: scale → HUD overlay → output
        vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,{hud}"
        cmd = [
            "ffmpeg", "-y",
            "-loglevel", "error",

            # Looped real footage
            "-stream_loop", "-1",
            "-i", src_path,

            # Looped KLV binary
            "-stream_loop", "-1",
            "-f", "data",
            "-i", klv_path,

            "-map", "0:v:0",
            "-map", "1:d:0",

            "-vf", vf,
            "-c:v", "libx264",
            "-profile:v", "baseline",
            "-level", "3.1",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-x264opts", "keyint=25:min-keyint=25:no-scenecut",
            "-r", "25",
            "-pix_fmt", "yuv420p",

            "-c:d", "copy",

            "-f", "mpegts",
            "-mpegts_pmt_start_pid", "0x0020",
            "-mpegts_start_pid", "0x0041",
            "-mpegts_flags", "+pat_pmt_at_frames",

            "-t", str(duration_s),
            out_path,
        ]
    else:
        # ── Synthetic lavfi mode ──────────────────────────────────────────
        zone = meta.get("zone", "UNKNOWN")
        pal  = _ZONE_PALETTE.get(zone, _ZONE_PALETTE["UNKNOWN"])
        hy   = height // 2

        # Build sky/ground background then apply HUD
        lavfi_bg = (
            f"color=c={pal['sky']}:size={width}x{hy}:rate=25[sky];"
            f"color=c={pal['ground']}:size={width}x{height-hy}:rate=25[gnd];"
            f"[sky][gnd]vstack=inputs=2[base];"
            f"[base]{hud}"
        )

        cmd = [
            "ffmpeg", "-y",
            "-loglevel", "error",

            "-f", "lavfi",
            "-i", f"[{lavfi_bg}]null",

            "-stream_loop", "-1",
            "-f", "data",
            "-i", klv_path,

            "-map", "0:v",
            "-map", "1:d",

            "-c:v", "libx264",
            "-profile:v", "baseline",
            "-level", "3.1",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-x264opts", "keyint=25:min-keyint=25:no-scenecut",
            "-r", "25",
            "-pix_fmt", "yuv420p",

            "-c:d", "copy",

            "-f", "mpegts",
            "-mpegts_pmt_start_pid", "0x0020",
            "-mpegts_start_pid", "0x0041",
            "-mpegts_flags", "+pat_pmt_at_frames",

            "-t", str(duration_s),
            out_path,
        ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=max(30, int(duration_s) * 10),
        )
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed: {result.stderr.decode()[:600]}")

        with open(out_path, "rb") as f:
            return f.read()

    finally:
        for p in (klv_path, out_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def generate_ts_segment_simple(
    meta: dict,
    klv_bytes: bytes,
    duration_s: float | None = None,
) -> bytes:
    """
    Fallback: video-only MPEG-TS (no KLV data track) using lavfi or real footage.
    Used when the full mux path fails. KLV still travels as SIG-008 metadata.
    """
    if duration_s is None:
        duration_s = SEGMENT_DURATION_S

    W, H = OUTPUT_WIDTH, OUTPUT_HEIGHT
    tmp      = _tmpdir()
    out_path = os.path.join(tmp, f"simple_{meta.get('frame_seq', 0):06d}.ts")

    hud     = _hud_filters(meta, W, H)
    src_path = VIDEO_SOURCE_PATH if VIDEO_SOURCE_PATH and os.path.exists(VIDEO_SOURCE_PATH) else None

    if src_path:
        vf = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,{hud}"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-stream_loop", "-1", "-i", src_path,
            "-vf", vf,
            "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1",
            "-preset", "ultrafast", "-tune", "zerolatency",
            "-r", "25", "-pix_fmt", "yuv420p",
            "-f", "mpegts", "-t", str(duration_s), out_path,
        ]
    else:
        zone = meta.get("zone", "UNKNOWN")
        pal  = _ZONE_PALETTE.get(zone, _ZONE_PALETTE["UNKNOWN"])
        hy   = H // 2
        lavfi_bg = (
            f"color=c={pal['sky']}:size={W}x{hy}:rate=25[sky];"
            f"color=c={pal['ground']}:size={W}x{H-hy}:rate=25[gnd];"
            f"[sky][gnd]vstack=inputs=2[base];"
            f"[base]{hud}"
        )
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"[{lavfi_bg}]null",
            "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.1",
            "-preset", "ultrafast", "-tune", "zerolatency",
            "-r", "25", "-pix_fmt", "yuv420p",
            "-f", "mpegts", "-t", str(duration_s), out_path,
        ]

    result = subprocess.run(cmd, capture_output=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed: {result.stderr.decode()[:600]}")

    with open(out_path, "rb") as f:
        ts_bytes = f.read()

    try:
        os.unlink(out_path)
    except OSError:
        pass

    return ts_bytes
