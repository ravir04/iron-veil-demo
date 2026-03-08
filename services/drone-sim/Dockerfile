FROM python:3.11-slim

# FFmpeg — required for STANAG 4609 MPEG-TS video segment generation.
# Set VIDEO_ENABLED=false to skip FFmpeg and use KLV-only TS packets instead.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# control.py is the entrypoint — it spawns and manages simulator.py,
# and exposes POST /demo/restart and /demo/speed/{n} on port 8092.
CMD ["python", "control.py"]
