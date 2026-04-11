#!/usr/bin/env python3
"""Multi-camera snapshot uploader (RTSP -> FTPS)

Usage: python scripts/upload_snapshot.py --config config/cameras.json

Reads camera list from a JSON file and uploads each camera's snapshot
to the configured remote path over FTPS (explicit TLS). Uses ffmpeg for RTSP captures.
"""
import argparse
import base64
import json
import logging
import os
import shlex
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from ftplib import FTP_TLS
from io import BytesIO

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MIN_SIZE = 1024

LOG = logging.getLogger("uploader")

# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _find_executable(name, env_var=None, explicit=None):
    """Locate an executable by explicit path, environment variable, or PATH."""
    return (
        explicit
        or (os.environ.get(env_var) if env_var else None)
        or shutil.which(name)
        or shutil.which(f"{name}.exe")
    )


def _get_ffmpeg_timeout(default=30):
    """Return ffmpeg timeout from FFMPEG_TIMEOUT env var or *default*."""
    try:
        val = os.environ.get("FFMPEG_TIMEOUT")
        if val:
            return int(val)
    except (ValueError, TypeError):
        pass
    return default


def _run_cmd(cmd, label, input_bytes=None, timeout=30):
    """Run a subprocess and return stdout bytes on success, or None.

    Handles logging, stderr capture, timeout, and common exceptions.
    When *input_bytes* is provided it is fed to stdin via a pipe;
    otherwise stdin is connected to devnull.
    """
    try:
        LOG.debug("Running %s: %s", label, " ".join(shlex.quote(c) for c in cmd))
        kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if input_bytes is not None:
            kwargs["input"] = input_bytes
        else:
            kwargs["stdin"] = subprocess.DEVNULL
        p = subprocess.run(cmd, **kwargs)
        stderr_text = p.stderr.decode("utf-8", errors="replace").strip() if p.stderr else ""
        if p.returncode == 0 and p.stdout:
            if stderr_text:
                LOG.debug("%s stderr: %s", label, stderr_text)
            return p.stdout
        if p.returncode == 0:
            LOG.warning("%s succeeded but produced no output", label)
        elif stderr_text:
            LOG.debug("%s failed (rc=%s): %s", label, p.returncode, stderr_text)
        else:
            LOG.debug("%s failed (rc=%s) with no stderr", label, p.returncode)
    except subprocess.TimeoutExpired:
        LOG.warning("%s timed out (timeout=%s)", label, timeout)
    except FileNotFoundError:
        LOG.error("%s executable not found: %s", label, cmd[0])
    except Exception as exc:
        LOG.exception("%s failed: %s", label, exc)
    return None


# ---------------------------------------------------------------------------
# Image capture / conversion
# ---------------------------------------------------------------------------


def _ffmpeg_rtsp_cmd(ffmpeg, rtsp_url, output_args):
    """Build the common ffmpeg RTSP single-frame capture command."""
    return [
        ffmpeg, "-y", "-nostdin",
        "-rtsp_transport", "tcp",
        "-probesize", "1048576",
        "-analyzeduration", "2000000",
        "-i", rtsp_url,
        "-frames:v", "1",
        *output_args,
        "pipe:1",
    ]


def run_ffmpeg_snapshot(rtsp_url, timeout=30, ffmpeg_cmd=None):
    """Capture a single RTSP frame as WebP bytes via ffmpeg."""
    ffmpeg = _find_executable("ffmpeg", "FFMPEG_CMD", ffmpeg_cmd)
    if not ffmpeg:
        LOG.error("ffmpeg not found. Install it or set FFMPEG_CMD.")
        return None
    quality = os.environ.get("FFMPEG_WEBP_QUALITY", "85")
    cmd = _ffmpeg_rtsp_cmd(ffmpeg, rtsp_url, [
        "-vcodec", "libwebp", "-lossless", "0",
        "-q:v", quality, "-preset", "picture", "-f", "webp",
    ])
    return _run_cmd(cmd, "ffmpeg-webp", timeout=_get_ffmpeg_timeout(timeout))


def run_ffmpeg_snapshot_jpeg(rtsp_url, timeout=30, ffmpeg_cmd=None):
    """Capture a single RTSP frame as JPEG bytes via ffmpeg."""
    ffmpeg = _find_executable("ffmpeg", "FFMPEG_CMD", ffmpeg_cmd)
    if not ffmpeg:
        LOG.error("ffmpeg not found. Install it or set FFMPEG_CMD.")
        return None
    quality = str(os.environ.get("FFMPEG_JPEG_QUALITY", "2"))
    cmd = _ffmpeg_rtsp_cmd(ffmpeg, rtsp_url, [
        "-q:v", quality, "-f", "image2pipe", "-vcodec", "mjpeg",
    ])
    return _run_cmd(cmd, "ffmpeg-jpeg", timeout=_get_ffmpeg_timeout(timeout))


def ffmpeg_image_to_webp_bytes(img_bytes, quality=85, timeout=30, ffmpeg_cmd=None):
    """Convert image bytes to WebP using ffmpeg (pipe-to-pipe)."""
    ffmpeg = _find_executable("ffmpeg", "FFMPEG_CMD", ffmpeg_cmd)
    if not ffmpeg:
        LOG.error("ffmpeg not found for WebP conversion")
        return None
    q = str(os.environ.get("FFMPEG_WEBP_QUALITY", str(quality)))
    cmd = [
        ffmpeg, "-y", "-nostdin",
        "-f", "image2pipe", "-i", "pipe:0",
        "-vcodec", "libwebp", "-lossless", "0", "-q:v", q,
        "-preset", "picture", "-f", "webp", "pipe:1",
    ]
    return _run_cmd(cmd, "ffmpeg-to-webp", input_bytes=img_bytes, timeout=timeout)


# ---------------------------------------------------------------------------
# Image utilities
# ---------------------------------------------------------------------------


def is_webp_bytes(data):
    """Best-effort detection of a WebP payload from RIFF/WEBP signature."""
    return bool(data and len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def validate_image_bytes(data, min_size=DEFAULT_MIN_SIZE):
    if not data or len(data) < min_size:
        if data:
            LOG.warning("Image too small (%d bytes)", len(data))
        return False
    return True


def output_format_for(remote_path):
    """Return 'webp' or 'jpeg' based on the remote_path file extension."""
    ext = os.path.splitext(remote_path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        return "jpeg"
    return "webp"


def _embed_rtsp_credentials(rtsp_url, username=None, password=None):
    """Embed username and password into an RTSP URL if provided.
    
    Converts 'rtsp://host:port/path' to 'rtsp://user:pass@host:port/path'
    """
    if not username or not password:
        return rtsp_url
    if "://" not in rtsp_url:
        return rtsp_url
    scheme, rest = rtsp_url.split("://", 1)
    # Only add credentials if not already present
    if "@" not in rest:
        return f"{scheme}://{username}:{password}@{rest}"
    return rtsp_url


# ---------------------------------------------------------------------------
# HTTP snapshot
# ---------------------------------------------------------------------------


def fetch_http_snapshot(url, username=None, password=None, timeout=30):
    """Fetch a snapshot image from an HTTP/HTTPS URL."""
    req = urllib.request.Request(url)
    if username or password:
        creds = base64.b64encode(f"{username or ''}:{password or ''}".encode()).decode()
        req.add_header("Authorization", f"Basic {creds}")
    try:
        LOG.debug("Fetching HTTP snapshot: %s", url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
        if data:
            LOG.debug("HTTP snapshot fetched: %d bytes", len(data))
        return data if data else None
    except Exception as e:
        LOG.exception("HTTP snapshot fetch failed for %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# FTPS upload
# ---------------------------------------------------------------------------


class _FTP_TLS_CustomHostname(FTP_TLS):
    """FTP_TLS subclass that uses a custom hostname for TLS SNI/verification."""
    tls_hostname = None

    def _with_tls_hostname(self, fn):
        if self.tls_hostname:
            real_host = self.host
            self.host = self.tls_hostname
            try:
                return fn()
            finally:
                self.host = real_host
        return fn()

    def auth(self):
        return self._with_tls_hostname(super().auth)

    def ntransfercmd(self, cmd, rest=None):
        return self._with_tls_hostname(
            lambda: super(_FTP_TLS_CustomHostname, self).ntransfercmd(cmd, rest)
        )


class FTPSUploader:
    def __init__(self, host, port, user, password=None, cafile=None, timeout=60, tls_hostname=None):
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
                    pass
        except Exception:
            ctx = None

        if self.tls_hostname:
            ftp = _FTP_TLS_CustomHostname(context=ctx) if ctx else _FTP_TLS_CustomHostname()
            ftp.tls_hostname = self.tls_hostname
        else:
            ftp = FTP_TLS(context=ctx) if ctx else FTP_TLS()
        LOG.debug("Connecting to FTPS %s:%s as %s", self.host, self.port, self.user)
        ftp.connect(self.host, self.port, timeout=self.timeout)
        ftp.login(self.user, self.password)
        try:
            ftp.prot_p()
        except Exception as exc:
            LOG.warning("Could not enable data channel TLS (prot_p): %s", exc)
        ftp.set_pasv(True)
        return ftp

    def _ensure_remote_dir(self, ftp, rdir):
        if not rdir:
            return
        try:
            ftp.cwd("/")
        except Exception:
            pass
        for part in (p for p in rdir.split("/") if p):
            try:
                ftp.cwd(part)
            except Exception:
                try:
                    LOG.debug("Creating remote directory: %s", part)
                    ftp.mkd(part)
                    ftp.cwd(part)
                except Exception:
                    try:
                        ftp.cwd(part)
                    except Exception:
                        pass

    def connect(self):
        """Establish (or re-establish) the FTP connection."""
        LOG.debug("Establishing FTPS connection to %s:%s", self.host, self.port)
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
        LOG.debug("Uploading %d bytes to %s", len(data_bytes), remote_path)
        ftp.storbinary(f"STOR {temp_name}", BytesIO(data_bytes))
        try:
            ftp.rename(temp_name, fname)
        except Exception:
            try:
                ftp.delete(fname)
            except Exception:
                pass
            ftp.rename(temp_name, fname)


# ---------------------------------------------------------------------------
# Pixelation
# ---------------------------------------------------------------------------


def _pixelate_region(img, x, y, w, h, factor):
    """Pixelate a rectangular region of a PIL Image in-place."""
    factor = max(1, int(factor))
    # Use Resampling.NEAREST in Pillow >= 10, fall back to NEAREST for older versions
    _nearest = getattr(
        getattr(img, "Resampling", None) or __import__("PIL.Image", fromlist=["Image"]).Image,
        "NEAREST",
        0,
    )
    region = img.crop((x, y, x + w, y + h))
    small = region.resize((max(1, w // factor), max(1, h // factor)), _nearest)
    img.paste(small.resize((w, h), _nearest), (x, y))


def apply_pixelation(image_bytes, camera):
    """Apply pixelation regions defined in the camera config.

    Each entry in the ``pixelate`` list must have ``x``, ``y``,
    ``width``, and ``height`` (in pixels).  An optional per-region
    ``factor`` (pixel block size) overrides the camera-level
    ``pixelate_factor`` (default: 10).

    Returns modified image bytes, or the original bytes on failure.
    """
    regions = camera.get("pixelate")
    if not regions:
        return image_bytes

    try:
        from PIL import Image as _PILImage
    except ImportError:
        LOG.error("Pillow is required for pixelation. Install it: pip install pillow")
        return image_bytes

    try:
        _nearest = getattr(
            getattr(_PILImage, "Resampling", _PILImage),
            "NEAREST",
            0,
        )
        remote_path = camera.get("remote_path", "")
        fmt = output_format_for(remote_path)
        default_factor = int(camera.get("pixelate_factor", 10))

        img = _PILImage.open(BytesIO(image_bytes))
        img_w, img_h = img.size

        for region in regions:
            x = int(region.get("x", 0))
            y = int(region.get("y", 0))
            w = int(region.get("width", 0))
            h = int(region.get("height", 0))
            factor = int(region.get("factor", default_factor))

            if w <= 0 or h <= 0:
                LOG.warning("Skipping pixelation region with non-positive size: %s", region)
                continue

            # Clamp to image bounds
            x = max(0, min(x, img_w - 1))
            y = max(0, min(y, img_h - 1))
            w = min(w, img_w - x)
            h = min(h, img_h - y)
            if w <= 0 or h <= 0:
                continue

            small = img.crop((x, y, x + w, y + h)).resize(
                (max(1, w // factor), max(1, h // factor)), _nearest
            )
            img.paste(small.resize((w, h), _nearest), (x, y))
            LOG.debug(
                "Pixelated region x=%d y=%d w=%d h=%d factor=%d for %s",
                x, y, w, h, factor, camera.get("id"),
            )

        buf = BytesIO()
        if fmt == "jpeg":
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=85)
        else:
            img.save(buf, format="WEBP", quality=int(os.environ.get("FFMPEG_WEBP_QUALITY", "85")))
        return buf.getvalue()
    except Exception:
        LOG.exception("Pixelation failed for %s; using original image", camera.get("id"))
        return image_bytes


# ---------------------------------------------------------------------------
# Camera processing
# ---------------------------------------------------------------------------


def _capture_image(camera):
    """Return (image_bytes, remote_path) for a camera, or (None, ...) on failure.

    The output format (WebP or JPEG) is determined by the file extension of
    *remote_path* in the camera config. Any extension other than .jpg/.jpeg
    produces WebP.
    """
    cam_id = camera.get("id")
    user = camera.get("user")
    password = camera.get("pass")
    remote_path = camera.get("remote_path")
    fmt = output_format_for(remote_path)
    LOG.debug("Output format for %s: %s", cam_id, fmt)
    image_bytes = None

    # 1. Try HTTP snapshot first
    snapshot_url = camera.get("snapshot_url")
    if snapshot_url:
        LOG.debug("Fetching HTTP snapshot for %s", cam_id)
        image_bytes = fetch_http_snapshot(snapshot_url, user, password)
        if validate_image_bytes(image_bytes):
            # Convert to WebP via ffmpeg if format requires it and source isn't already WebP
            if fmt == "webp" and not is_webp_bytes(image_bytes):
                LOG.debug("Converting HTTP snapshot to WebP for %s", cam_id)
                webp_bytes = ffmpeg_image_to_webp_bytes(image_bytes)
                if webp_bytes and validate_image_bytes(webp_bytes):
                    image_bytes = webp_bytes
                else:
                    LOG.warning("WebP conversion failed for %s; uploading original bytes", cam_id)
        else:
            LOG.warning("HTTP snapshot failed for %s, falling back to RTSP", cam_id)
            image_bytes = None

    # 2. Fall back to RTSP via ffmpeg
    rtsp = camera.get("rtsp_url")
    if image_bytes is None and rtsp:
        LOG.debug("Attempting RTSP capture for %s", cam_id)
        # Embed credentials in RTSP URL if provided
        rtsp_with_creds = _embed_rtsp_credentials(rtsp, user, password)
        if fmt == "jpeg":
            image_bytes = run_ffmpeg_snapshot_jpeg(rtsp_with_creds)
        else:
            image_bytes = run_ffmpeg_snapshot(rtsp_with_creds)

    if image_bytes is None:
        if not snapshot_url and not rtsp:
            LOG.error("No snapshot_url or rtsp_url configured for %s", cam_id)
    else:
        LOG.debug("Captured %d bytes for %s", len(image_bytes), cam_id)

    return image_bytes, remote_path


def _capture_with_retries(camera, attempts=3):
    """Capture and validate image bytes with retry logic; returns bytes or None."""
    cam_id = camera.get("id")
    for attempt in range(1, attempts + 1):
        image_bytes, remote_path = _capture_image(camera)
        if validate_image_bytes(image_bytes):
            return image_bytes, remote_path
        LOG.warning("Capture attempt %d/%d failed for %s", attempt, attempts, cam_id)
        if attempt < attempts:
            delay = 2 ** attempt
            LOG.debug("Retrying capture in %ds", delay)
            time.sleep(delay)
    LOG.error("All capture attempts failed for %s", cam_id)
    return None, camera.get("remote_path")


def _upload_with_retries(uploader, data, remote_path, cam_id, attempts=3):
    """Upload bytes with retry logic; returns True on success."""
    for attempt in range(1, attempts + 1):
        try:
            LOG.debug("Uploading to %s (attempt %d)", remote_path, attempt)
            uploader.upload_bytes(data, remote_path)
            LOG.info("Uploaded camera %s to %s", cam_id, remote_path)
            return True
        except Exception:
            LOG.exception("Upload attempt %d failed for %s", attempt, cam_id)
            uploader.close()
            if attempt < attempts:
                delay = 2 ** attempt
                LOG.debug("Retrying upload in %ds", delay)
                time.sleep(delay)
    LOG.error("All upload attempts failed for %s", cam_id)
    return False


def _save_local_copy(image_bytes, remote_path, cam_id, output_dir):
    """Save a timestamped local copy if *output_dir* is set."""
    if not output_dir:
        return
    try:
        dest_dir = os.path.join(output_dir, cam_id)
        os.makedirs(dest_dir, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        _, ext = os.path.splitext(remote_path)
        dest = os.path.join(dest_dir, f"{ts}{ext or '.webp'}")
        with open(dest, "wb") as f:
            f.write(image_bytes)
        LOG.debug("Saved local copy to %s", dest)
    except Exception:
        LOG.exception("Failed to write local copy for %s", cam_id)


def process_camera(camera, uploader, config):
    cam_id = camera.get("id")
    LOG.info("Processing camera %s", cam_id)

    if not camera.get("remote_path"):
        LOG.error("No remote_path configured for %s", cam_id)
        return False

    image_bytes, remote_path = _capture_with_retries(camera)

    if not validate_image_bytes(image_bytes):
        LOG.error("Failed to capture valid image for %s", cam_id)
        return False

    image_bytes = apply_pixelation(image_bytes, camera)

    if not _upload_with_retries(uploader, image_bytes, remote_path, cam_id):
        return False

    _save_local_copy(image_bytes, remote_path, cam_id, config.get("output_dir"))
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/cameras.json")
    parser.add_argument("--env", default="config/.env")
    args = parser.parse_args()

    if load_dotenv and os.path.exists(args.env):
        load_dotenv(args.env)

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

    with open(args.config, "r", encoding="utf-8") as f:
        cameras = json.load(f)
    LOG.info("Loaded %d camera(s) from %s", len(cameras), args.config)

    ftps_host = os.environ.get("FTPS_HOST")
    if not ftps_host:
        LOG.error("No upload destination configured. Set FTPS_HOST environment variable.")
        sys.exit(3)
    ftps_port = int(os.environ.get("FTPS_PORT", "21"))
    ftps_user = os.environ.get("FTPS_USER")
    if not ftps_user:
        LOG.error("No FTPS username configured. Set FTPS_USER environment variable.")
        sys.exit(3)

    uploader = FTPSUploader(
        ftps_host, ftps_port, ftps_user,
        password=os.environ.get("FTPS_PASSWORD"),
        cafile=os.environ.get("FTPS_CAFILE"),
        tls_hostname=os.environ.get("FTPS_TLS_HOSTNAME"),
    )
    LOG.info("FTPS target: %s:%s (user: %s)", ftps_host, ftps_port, ftps_user)

    interval = os.environ.get("INTERVAL_SECONDS")
    try:
        interval = int(interval) if interval else None
    except Exception:
        interval = None

    config = {"output_dir": os.environ.get("OUTPUT_DIR")}

    def run_once():
        results = [process_camera(cam, uploader, config) for cam in cameras]
        ok = sum(results)
        if ok < len(results):
            LOG.warning("%d/%d camera(s) failed", len(results) - ok, len(results))
        else:
            LOG.info("All %d camera(s) processed successfully", len(results))
        return all(results)

    if interval and interval > 0:
        LOG.info("Starting continuous mode: interval=%s seconds", interval)
        try:
            while True:
                start = time.time()
                run_once()
                remaining = interval - (time.time() - start)
                if remaining > 0:
                    time.sleep(remaining)
        except KeyboardInterrupt:
            LOG.info("Interrupted, exiting")
            sys.exit(0)
    else:
        sys.exit(0 if run_once() else 2)


if __name__ == "__main__":
    main()
