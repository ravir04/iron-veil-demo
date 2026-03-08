"""
Demo control sidecar — Iron-Veil Demo

Tiny HTTP server (port 8092) that lets the COP-UI restart the mission
or change the frame interval without touching the terminal.

Endpoints:
  POST /demo/restart          — kill the current simulator.py process and
                                start a fresh one (same env vars)
  POST /demo/speed/{interval} — restart with a different FRAME_INTERVAL
                                (e.g. /demo/speed/0.5 for 2×, /demo/speed/2.0 for 0.5×)
  GET  /demo/status           — current pid, interval, mission elapsed seconds

The sidecar is started by the container entrypoint alongside simulator.py.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread, Lock

CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "8092"))
FRAME_INTERVAL = float(os.environ.get("FRAME_INTERVAL", "1.0"))

_lock = Lock()
_sim_proc: subprocess.Popen | None = None
_current_interval: float = FRAME_INTERVAL
_mission_start: float = time.time()


def _start_sim(interval: float) -> subprocess.Popen:
    global _mission_start
    _mission_start = time.time()
    env = {**os.environ, "FRAME_INTERVAL": str(interval), "PYTHONUNBUFFERED": "1"}
    return subprocess.Popen(
        [sys.executable, "-u", "simulator.py", "--interval", str(interval)],
        env=env,
    )


def _restart(interval: float):
    global _sim_proc, _current_interval
    with _lock:
        if _sim_proc and _sim_proc.poll() is None:
            _sim_proc.send_signal(signal.SIGTERM)
            try:
                _sim_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _sim_proc.kill()
        _current_interval = interval
        _sim_proc = _start_sim(interval)


def _watch_sim():
    """Restart sim automatically if it exits (mission complete)."""
    global _sim_proc
    while True:
        time.sleep(2)
        with _lock:
            if _sim_proc and _sim_proc.poll() is not None:
                _sim_proc = _start_sim(_current_interval)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silence access log

    def _respond(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        if self.path == "/demo/status":
            elapsed = int(time.time() - _mission_start)
            running = _sim_proc is not None and _sim_proc.poll() is None
            self._respond(200, {
                "running": running,
                "pid": _sim_proc.pid if _sim_proc else None,
                "interval": _current_interval,
                "mission_elapsed_s": elapsed,
            })
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.rstrip("/")

        if path == "/demo/restart":
            _restart(_current_interval)
            self._respond(200, {"ok": True, "interval": _current_interval})

        elif path.startswith("/demo/speed/"):
            try:
                interval = float(path.split("/demo/speed/", 1)[1])
                if not (0.1 <= interval <= 10.0):
                    raise ValueError("out of range")
            except (ValueError, IndexError):
                self._respond(400, {"error": "interval must be 0.1–10.0"})
                return
            _restart(interval)
            self._respond(200, {"ok": True, "interval": interval})

        else:
            self._respond(404, {"error": "not found"})


def main():
    global _sim_proc
    _sim_proc = _start_sim(_current_interval)

    watcher = Thread(target=_watch_sim, daemon=True)
    watcher.start()

    print(f"[control] Demo control sidecar on :{CONTROL_PORT}", flush=True)
    server = HTTPServer(("0.0.0.0", CONTROL_PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
