#!/usr/bin/env python3
"""Multi-camera snapshot uploader (RTSP -> FTPS)

Usage: python scripts/upload_snapshot.py --config config/cameras.json

Reads camera list from a JSON file and uploads each camera's snapshot
to the configured remote path over FTPS (explicit TLS). Uses ffmpeg for RTSP captures.
"""
import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from io import BytesIO
import ssl
from ftplib import FTP_TLS
import shutil

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

try:
    from PIL import Image
except Exception:
    Image = None

DEFAULT_MIN_SIZE = 1024

LOG = logging.getLogger("uploader")


def run_ffmpeg_snapshot(rtsp_url, username=None, password=None, timeout=15, ffmpeg_cmd=None):
    """Capture a single frame from RTSP using ffmpeg and return JPEG bytes (in-memory).

    The ffmpeg executable will be taken from `ffmpeg_cmd` param, `FFMPEG_CMD`
    environment variable, or located via PATH (`shutil.which`). If not found,
    a clear error is logged and None is returned.
    """
    cmd_exe = ffmpeg_cmd or os.environ.get("FFMPEG_CMD") or shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if not cmd_exe:
        LOG.error("ffmpeg not found. Install ffmpeg and add it to PATH, or set FFMPEG_CMD to the ffmpeg executable path.")
        return None

    cmd = [
        cmd_exe,
        "-y",
        "-rtsp_transport", "tcp",
        "-probesize", "524288",
        "-analyzeduration", "1000000",
        "-i", rtsp_url,
        "-frames:v", "1",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "-",
    ]
    try:
        LOG.debug("Running ffmpeg: %s", ' '.join(shlex.quote(c) for c in cmd))
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout)
        if p.returncode == 0 and p.stdout and len(p.stdout) > 0:
            return p.stdout
    except subprocess.TimeoutExpired:
        LOG.warning("ffmpeg timed out for %s", rtsp_url)
    except FileNotFoundError:
        LOG.error("ffmpeg executable not found at %s", cmd_exe)
    except Exception as e:
        LOG.exception("ffmpeg capture failed: %s", e)
    return None



def validate_image_bytes(data, min_size=DEFAULT_MIN_SIZE):
    if not data or len(data) < min_size:
        if data:
            LOG.warning("Image too small (%d bytes)", len(data))
        return False
    return True


class _FTP_TLS_CustomHostname(FTP_TLS):
    """FTP_TLS subclass that uses a custom hostname for TLS SNI/verification."""
    tls_hostname = None

    def _with_tls_hostname(self, fn):
        """Call fn() with self.host temporarily set to tls_hostname for SNI/verification."""
        if self.tls_hostname:
            real_host = self.host
            self.host = self.tls_hostname
            try:
                return fn()
            finally:
                self.host = real_host
        return fn()

    def auth(self):
        """Upgrade control connection to TLS using the certificate hostname for SNI."""
        return self._with_tls_hostname(super().auth)

    def ntransfercmd(self, cmd, rest=None):
        """Open data connection using the certificate hostname for SNI."""
        return self._with_tls_hostname(lambda: super(_FTP_TLS_CustomHostname, self).ntransfercmd(cmd, rest))


class FTPSUploader:
    def __init__(self, host, port, user, password=None, cafile=None, timeout=15, tls_hostname=None):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.cafile = cafile
        self.timeout = timeout
        self.tls_hostname = tls_hostname
        self._ftp = None

    def _connect(self):
        ctx = None
        try:
            ctx = ssl.create_default_context()
            if self.cafile:
                try:
                    ctx.load_verify_locations(self.cafile)
                except Exception:
                    # fallback to default context if cafile invalid
                    pass
        except Exception:
            ctx = None

        if self.tls_hostname:
            ftp = _FTP_TLS_CustomHostname(context=ctx) if ctx is not None else _FTP_TLS_CustomHostname()
            ftp.tls_hostname = self.tls_hostname
        else:
            ftp = FTP_TLS(context=ctx) if ctx is not None else FTP_TLS()
        ftp.connect(self.host, self.port, timeout=self.timeout)
        ftp.login(self.user, self.password)
        try:
            # secure the data connection
            ftp.prot_p()
        except Exception:
            pass
        ftp.set_pasv(True)
        return ftp

    def _ensure_remote_dir(self, ftp, rdir):
        if not rdir:
            return
        try:
            ftp.cwd("/")
        except Exception:
            pass
        parts = [p for p in rdir.split("/") if p]
        for part in parts:
            try:
                ftp.cwd(part)
            except Exception:
                try:
                    ftp.mkd(part)
                    ftp.cwd(part)
                except Exception:
                    try:
                        ftp.cwd(part)
                    except Exception:
                        pass

    def connect(self):
        """Establish (or re-establish) the FTP connection."""
        self.close()
        self._ftp = self._connect()

    def close(self):
        if self._ftp is not None:
            try:
                self._ftp.quit()
            except Exception:
                pass
            self._ftp = None

    def _get_ftp(self):
        """Return the current connection, or create one lazily."""
        if self._ftp is None:
            self._ftp = self._connect()
        return self._ftp

    def upload_bytes(self, data_bytes, remote_path):
        ftp = self._get_ftp()
        rdir = os.path.dirname(remote_path)
        fname = os.path.basename(remote_path)
        self._ensure_remote_dir(ftp, rdir)
        temp_name = fname + ".tmp"
        bio = BytesIO(data_bytes)
        ftp.storbinary(f"STOR {temp_name}", bio)
        try:
            ftp.rename(temp_name, fname)
        except Exception:
            try:
                ftp.delete(fname)
            except Exception:
                pass
            ftp.rename(temp_name, fname)

def process_camera(camera, uploader, config):
    cam_id = camera.get("id")
    LOG.info("Processing camera %s", cam_id)
    rtsp = camera.get("rtsp_url")
    user = camera.get("user")
    password = camera.get("pass")
    remote_path = camera.get("remote_path")
    # capture image into memory (bytes)
    image_bytes = None
    if rtsp:
        LOG.debug("Attempting RTSP capture for %s", cam_id)
        image_bytes = run_ffmpeg_snapshot(rtsp, user, password)

    if not validate_image_bytes(image_bytes):
        LOG.error("Failed to capture valid image for %s", cam_id)
        return False

    # convert to WebP in-memory (single PIL open for validation + conversion)
    upload_bytes = image_bytes
    upload_remote = remote_path
    if Image is None:
        LOG.warning("Pillow not available; uploading original image bytes")
    else:
        try:
            im = Image.open(BytesIO(image_bytes))
            im.load()  # fully decode once — validates and prepares for conversion
            buf = BytesIO()
            im.save(buf, format="WEBP", quality=85)
            im.close()
            webp_bytes = buf.getvalue()
            if len(webp_bytes) >= 100:
                upload_bytes = webp_bytes
                base, _ = os.path.splitext(remote_path)
                upload_remote = base + ".webp"
                LOG.debug("Converted to WebP (in-memory), uploading %s", upload_remote)
            else:
                LOG.warning("Converted WebP too small; uploading original JPEG bytes")
        except Exception:
            LOG.exception("Failed to convert image to WebP for %s", cam_id)

    # upload bytes (atomic write via temp + rename)
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            LOG.debug("Uploading bytes to %s (attempt %d)", upload_remote, attempt)
            uploader.upload_bytes(upload_bytes, upload_remote)
            LOG.info("Uploaded camera %s to %s", cam_id, upload_remote)
            break
        except Exception:
            LOG.exception("Upload attempt %d failed for %s", attempt, cam_id)
            uploader.close()  # force reconnect on next attempt
            if attempt < attempts:
                time.sleep(2 ** attempt)
            else:
                LOG.error("All upload attempts failed for %s", cam_id)
                return False

    # optionally save a local timestamped JPEG copy if configured
    out_dir = config.get("output_dir")
    if out_dir:
        try:
            dest_dir = os.path.join(out_dir, cam_id)
            os.makedirs(dest_dir, exist_ok=True)
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
            dest = os.path.join(dest_dir, f"{ts}.jpg")
            with open(dest, "wb") as f:
                f.write(image_bytes)
        except Exception:
            LOG.exception("Failed to write local copy for %s", cam_id)

    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/cameras.json")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()

    if load_dotenv and os.path.exists(args.env):
        load_dotenv(args.env)

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

    with open(args.config, "r", encoding="utf-8") as f:
        cameras = json.load(f)

    ftps_host = os.environ.get("FTPS_HOST")
    if not ftps_host:
        LOG.error("No upload destination configured. Set FTPS_HOST environment variable.")
        sys.exit(3)
    ftps_port = int(os.environ.get("FTPS_PORT", "21"))
    ftps_user = os.environ.get("FTPS_USER")
    ftps_password = os.environ.get("FTPS_PASSWORD")
    ftps_cafile = os.environ.get("FTPS_CAFILE")
    ftps_tls_hostname = os.environ.get("FTPS_TLS_HOSTNAME")

    uploader = FTPSUploader(ftps_host, ftps_port, ftps_user, password=ftps_password, cafile=ftps_cafile, tls_hostname=ftps_tls_hostname)

    # runtime configuration
    interval = os.environ.get("INTERVAL_SECONDS")
    try:
        interval = int(interval) if interval else None
    except Exception:
        interval = None

    config = {
        "output_dir": os.environ.get("OUTPUT_DIR"),
    }

    def run_once():
        ok_all = True
        for cam in cameras:
            ok = process_camera(cam, uploader, config)
            ok_all = ok_all and ok
        return ok_all

    if interval and interval > 0:
        LOG.info("Starting continuous mode: interval=%s seconds", interval)
        try:
            while True:
                start = time.time()
                run_once()
                elapsed = time.time() - start
                to_sleep = interval - elapsed
                if to_sleep > 0:
                    time.sleep(to_sleep)
        except KeyboardInterrupt:
            LOG.info("Interrupted, exiting")
            sys.exit(0)
    else:
        overall_ok = run_once()
        sys.exit(0 if overall_ok else 2)


if __name__ == "__main__":
    main()
