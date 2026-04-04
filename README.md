# Multi-camera SFTP snapshot uploader

This repository contains a small Python script to capture a single JPEG frame from multiple Ubiquiti (or RTSP/ONVIF) cameras and upload each camera's `latest.jpg` to a static website via SFTP.

Quick start
1. Install system dependency `ffmpeg` (apt, brew, or Windows installer).
2. Create a Python 3.9+ virtualenv and install requirements:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

3. Configure `config/cameras.json` with your cameras (see sample file).
4. Copy `config/.env.example` to `config/.env` and update `SFTP_*` values. Prefer SSH key auth:

```bash
ssh-keygen -f ~/.ssh/uploader_key -N ''
ssh-copy-id -i ~/.ssh/uploader_key.pub uploader@example.com
```

5. Run once for testing:

```bash
python scripts/upload_snapshot.py --config config/cameras.json --env config/.env
```

Continuous mode
- To run continuously and capture+upload every N seconds, set `INTERVAL_SECONDS` in `config/.env` (for example `INTERVAL_SECONDS=60`). The script will loop and process all cameras every interval.
 - Captured images are converted to WebP before upload (always enabled). The script will upload `latest.webp` for each camera — ensure your static site polls the `.webp` filename.

Scheduling with cron (every 5 minutes)

```cron
*/5 * * * * /usr/bin/python3 /path/to/scripts/upload_snapshot.py --config /path/to/config/cameras.json --env /path/to/config/.env >> /var/log/camera_uploader.log 2>&1
```

Windows Task Scheduler
- Create a basic task that runs the same Python command on your chosen schedule.

Notes
- The script uses `ffmpeg` to capture one frame from RTSP streams. If RTSP fails and the optional ONVIF library is installed, it will try ONVIF `GetSnapshotUri` as a fallback.
- The script uploads via SFTP using `paramiko`. It uploads to a temporary filename and renames to `latest.jpg` to avoid partial reads by the site.
- To avoid caching on the site, configure your web server to set `Cache-Control: no-cache` for the `latest.jpg` paths, or have the site append a cache-busting query, e.g. `latest.jpg?t=TIMESTAMP`.

Security
- Use SSH key auth where possible and store private key files with restrictive permissions (600).
- Do not commit `config/.env` with real credentials.

Troubleshooting
- If captures fail, test the RTSP URL in VLC and run the ffmpeg command manually:

```bash
ffmpeg -rtsp_transport tcp -i "rtsp://user:pass@camera-ip:554/stream" -frames:v 1 /tmp/snap.jpg
```

If SFTP upload fails, test with `sftp` or `scp` from the uploader host to confirm credentials/permissions.
