# Multi-camera FTPS snapshot uploader

This repository contains a small Python script to capture a single frame from multiple RTSP cameras and upload each camera's snapshot to a static website via FTPS (explicit TLS).

Quick start
1. Install system dependency `ffmpeg` (apt, brew, or Windows installer).
2. Create a Python virtualenv and install requirements:

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

3. Configure `config/cameras.json` with your cameras (see Camera config below).
4. Copy `config/.env.example` to `config/.env` and fill in `FTPS_*` values.
5. Run once for testing:

```bash
python scripts/upload_snapshot.py --config config/cameras.json --env config/.env
```

Camera config
Each entry in `cameras.json` supports the following fields:

| Field | Required | Description |
|---|---|---|
| `id` | yes | Unique identifier used in log messages and local output paths |
| `remote_path` | yes | Destination path on the FTPS server, e.g. `/webcams/cam1.jpg`. **The file extension determines the output format** — `.jpg`/`.jpeg` produces JPEG, anything else (e.g. `.webp`) produces WebP |
| `rtsp_url` | no | RTSP stream URL; used as primary or fallback capture source. Credentials can be embedded in the URL (`rtsp://user:pass@host:554/stream`) or provided via separate `user`/`pass` fields |
| `snapshot_url` | no | HTTP/HTTPS URL to fetch a still image; tried first, falls back to `rtsp_url` |
| `user` | no | Username for RTSP and HTTP snapshot authentication |
| `pass` | no | Password for RTSP and HTTP snapshot authentication |

Example:

```json
[
  {
    "id": "cam1",
    "rtsp_url": "rtsp://192.168.1.10:554/stream",
    "remote_path": "/webcams/cam1.jpg"
  },
  {
    "id": "cam2",
    "snapshot_url": "http://192.168.1.11/snapshot.jpg",
    "rtsp_url": "rtsp://admin:secret@192.168.1.11:554/stream",
    "remote_path": "/webcams/cam2.webp",
    "user": "admin",
    "pass": "secret"
  }
]
```

Output format
The script selects the encoding pipeline based on the `remote_path` extension in `cameras.json`:

- **`.jpg` / `.jpeg`** — ffmpeg captures RTSP directly as JPEG (lightweight, recommended for resource-constrained devices like Raspberry Pi)
- **any other extension** (e.g. `.webp`) — ffmpeg captures RTSP and encodes to WebP

No environment variable or flag is needed — just set the correct extension in `remote_path`.

Environment variables (`.env`)

| Variable | Default | Description |
|---|---|---|
| `FTPS_HOST` | *(required)* | FTPS server hostname |
| `FTPS_PORT` | `21` | FTPS server port |
| `FTPS_USER` | *(required)* | FTPS username |
| `FTPS_PASSWORD` | | FTPS password |
| `FTPS_CAFILE` | | Path to a custom CA certificate for TLS verification |
| `FTPS_TLS_HOSTNAME` | | Override hostname used for TLS SNI/verification (when cert CN differs from connection host) |
| `FFMPEG_CMD` | auto | Path to ffmpeg executable |
| `FFMPEG_WEBP_QUALITY` | `85` | WebP quality (0–100) |
| `FFMPEG_JPEG_QUALITY` | `2` | JPEG quality scale (2=best, 31=worst, ffmpeg scale) |
| `FFMPEG_TIMEOUT` | `30` | Seconds before ffmpeg is killed |
| `INTERVAL_SECONDS` | | Run continuously, capturing every N seconds |
| `OUTPUT_DIR` | | If set, saves a timestamped local copy of each image under `OUTPUT_DIR/<camera-id>/` |
| `LOG_LEVEL` | `INFO` | Python logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

Continuous mode
Set `INTERVAL_SECONDS` in `config/.env` to run in a loop:

```
INTERVAL_SECONDS=60
```

The script will capture and upload all cameras every N seconds, sleeping between runs.

Scheduling with cron (every 5 minutes)

```cron
*/5 * * * * /path/to/.venv/bin/python /path/to/scripts/upload_snapshot.py --config /path/to/config/cameras.json --env /path/to/config/.env >> /var/log/camera_uploader.log 2>&1
```

Windows Task Scheduler
Create a basic task that runs the same Python command on your chosen schedule.

Security
- Do not commit `config/.env` — it contains credentials.
- FTPS uses explicit TLS; the data connection is also secured (`PROT P`). Uploads are written atomically via a `.tmp` rename to prevent partial reads.

Troubleshooting

If captures fail, test the RTSP URL in VLC, then run ffmpeg manually:

```bash
# WebP
ffmpeg -rtsp_transport tcp -i "rtsp://user:pass@camera-ip:554/stream" -frames:v 1 -vcodec libwebp -f webp out.webp

# JPEG
ffmpeg -rtsp_transport tcp -i "rtsp://user:pass@camera-ip:554/stream" -frames:v 1 -q:v 2 -f image2pipe -vcodec mjpeg out.jpg
```

If the FTPS upload fails, test connectivity manually:

```bash
ftp -p <host>
```

Enable `LOG_LEVEL=DEBUG` in `.env` for detailed ffmpeg and upload diagnostics.
