"""Tests for the apply_pixelation function."""
import io
import sys
import os
import pytest

# Make the scripts directory importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from upload_snapshot import apply_pixelation  # noqa: E402

try:
    from PIL import Image
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False

pytestmark = pytest.mark.skipif(not PILLOW_AVAILABLE, reason="Pillow not installed")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_image_bytes(width=320, height=240, fmt="JPEG", color=(255, 0, 0)):
    """Return bytes of a solid-colour image in the requested format."""
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _open_bytes(data):
    return Image.open(io.BytesIO(data))


def _pixel(data, x, y):
    return _open_bytes(data).convert("RGB").getpixel((x, y))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestApplyPixelationNoOp:
    def test_no_pixelate_key_returns_original(self):
        camera = {"id": "cam1", "remote_path": "/cams/cam1.jpg"}
        data = _make_image_bytes()
        result = apply_pixelation(data, camera)
        assert result is data  # exact same object — no processing

    def test_empty_pixelate_list_returns_original(self):
        camera = {"id": "cam1", "remote_path": "/cams/cam1.jpg", "pixelate": []}
        data = _make_image_bytes()
        result = apply_pixelation(data, camera)
        assert result is data


class TestApplyPixelationJPEG:
    def test_region_is_modified(self):
        """Pixels inside the pixelated region must differ from the sharp original."""
        # A gradient image so the region would otherwise have distinct pixel colours
        img = Image.new("RGB", (320, 240))
        for x in range(320):
            for y in range(240):
                img.putpixel((x, y), (x % 256, y % 256, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        original_bytes = buf.getvalue()

        camera = {
            "id": "cam1",
            "remote_path": "/cams/cam1.jpg",
            "pixelate_factor": 16,
            "pixelate": [{"x": 0, "y": 0, "width": 64, "height": 64}],
        }
        result = apply_pixelation(original_bytes, camera)
        assert result != original_bytes

    def test_pixels_outside_region_are_unchanged(self):
        """A solid-colour image: pixels outside the pixelated region stay the same colour."""
        color = (100, 150, 200)
        data = _make_image_bytes(color=color)
        camera = {
            "id": "cam1",
            "remote_path": "/cams/cam1.jpg",
            "pixelate_factor": 10,
            "pixelate": [{"x": 0, "y": 0, "width": 32, "height": 32}],
        }
        result = apply_pixelation(data, camera)
        # Far corner should still be the original colour (JPEG tolerance ±5)
        px = _pixel(result, 319, 239)
        assert all(abs(px[i] - color[i]) <= 5 for i in range(3))

    def test_returns_bytes(self):
        data = _make_image_bytes()
        camera = {
            "id": "cam1",
            "remote_path": "/cams/cam1.jpg",
            "pixelate": [{"x": 10, "y": 10, "width": 50, "height": 50}],
        }
        result = apply_pixelation(data, camera)
        assert isinstance(result, bytes)
        assert len(result) > 0


class TestApplyPixelationWebP:
    def test_webp_output_for_webp_remote_path(self):
        data = _make_image_bytes(fmt="WEBP")
        camera = {
            "id": "cam1",
            "remote_path": "/cams/cam1.webp",
            "pixelate": [{"x": 0, "y": 0, "width": 64, "height": 64}],
        }
        result = apply_pixelation(data, camera)
        # RIFF….WEBP signature
        assert result[:4] == b"RIFF"
        assert result[8:12] == b"WEBP"


class TestApplyPixelationMultipleRegions:
    def test_multiple_regions_applied(self):
        img = Image.new("RGB", (320, 240))
        for x in range(320):
            for y in range(240):
                img.putpixel((x, y), (x % 256, y % 256, 0))
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        original_bytes = buf.getvalue()

        camera = {
            "id": "cam1",
            "remote_path": "/cams/cam1.jpg",
            "pixelate": [
                {"x": 0,   "y": 0,   "width": 32, "height": 32},
                {"x": 200, "y": 100, "width": 50, "height": 50},
            ],
        }
        result = apply_pixelation(original_bytes, camera)
        assert result != original_bytes


class TestApplyPixelationPerRegionFactor:
    def test_per_region_factor_overrides_default(self):
        """Two identical images pixelated with different factors must differ."""
        img = Image.new("RGB", (320, 240))
        for x in range(320):
            for y in range(240):
                img.putpixel((x, y), (x % 256, y % 256, 128))
        buf = io.BytesIO()
        img.save(buf, format="WEBP")
        original_bytes = buf.getvalue()

        region_base = {"x": 0, "y": 0, "width": 64, "height": 64}

        cam_small_factor = {
            "id": "cam1",
            "remote_path": "/cams/cam1.webp",
            "pixelate": [{**region_base, "factor": 2}],
        }
        cam_large_factor = {
            "id": "cam1",
            "remote_path": "/cams/cam1.webp",
            "pixelate": [{**region_base, "factor": 32}],
        }
        result_small = apply_pixelation(original_bytes, cam_small_factor)
        result_large = apply_pixelation(original_bytes, cam_large_factor)
        assert result_small != result_large


class TestApplyPixelationEdgeCases:
    def test_region_clamped_to_image_bounds(self):
        """A region extending beyond image edges should not raise."""
        data = _make_image_bytes(width=100, height=100)
        camera = {
            "id": "cam1",
            "remote_path": "/cams/cam1.jpg",
            "pixelate": [{"x": 80, "y": 80, "width": 200, "height": 200}],
        }
        result = apply_pixelation(data, camera)
        assert isinstance(result, bytes)

    def test_zero_size_region_skipped(self):
        """Regions with zero width or height must be silently skipped."""
        data = _make_image_bytes()
        camera = {
            "id": "cam1",
            "remote_path": "/cams/cam1.jpg",
            "pixelate": [{"x": 10, "y": 10, "width": 0, "height": 50}],
        }
        result = apply_pixelation(data, camera)
        assert isinstance(result, bytes)

    def test_factor_1_does_not_raise(self):
        data = _make_image_bytes()
        camera = {
            "id": "cam1",
            "remote_path": "/cams/cam1.jpg",
            "pixelate_factor": 1,
            "pixelate": [{"x": 0, "y": 0, "width": 64, "height": 64}],
        }
        result = apply_pixelation(data, camera)
        assert isinstance(result, bytes)

    def test_rgba_jpeg_converted_without_error(self):
        """RGBA source image should be converted to RGB before JPEG save."""
        img = Image.new("RGBA", (100, 100), (255, 0, 0, 128))
        buf = io.BytesIO()
        img.save(buf, format="PNG")  # PNG supports RGBA; we pass it as JPEG context
        png_bytes = buf.getvalue()

        camera = {
            "id": "cam1",
            "remote_path": "/cams/cam1.jpg",
            "pixelate": [{"x": 0, "y": 0, "width": 50, "height": 50}],
        }
        result = apply_pixelation(png_bytes, camera)
        assert isinstance(result, bytes)


# ---------------------------------------------------------------------------
# Manual / visual test with a real image file
#
# Drop any image into tests/fixtures/ and point TEST_IMAGE at it, or just
# place a file named "test_image.jpg" (or .webp / .png) there.
#
# Run with:
#   pytest tests/test_pixelation.py::test_real_image_pixelation -v
#
# The pixelated result is written next to the source file as
#   <name>_pixelated<ext>   (e.g. test_image_pixelated.jpg)
#
# Customise the regions and factor via environment variables:
#   TEST_IMAGE         – path to the source image   (default: tests/fixtures/test_image.jpg)
#   TEST_PIX_FACTOR    – pixel block size            (default: 15)
#   TEST_PIX_REGIONS   – JSON array of region dicts  (default: top-left quarter of the image)
# ---------------------------------------------------------------------------

def test_real_image_pixelation():
    """Pixelate a real image and write the result to disk for visual inspection."""
    import json

    default_path = os.path.join(os.path.dirname(__file__), "fixtures", "test_image.jpg")
    image_path = os.environ.get("TEST_IMAGE", default_path)

    if not os.path.isfile(image_path):
        pytest.skip(
            f"No test image found at {image_path}. "
            "Place an image there or set TEST_IMAGE=<path> to run this test."
        )

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    # Determine output format from extension
    ext = os.path.splitext(image_path)[1].lower()
    remote_path = f"/cams/cam1{ext}"

    # Build pixelation regions: default to top-left quarter of the image
    regions_env = os.environ.get("TEST_PIX_REGIONS")
    if regions_env:
        regions = json.loads(regions_env)
    else:
        img_size = Image.open(io.BytesIO(image_bytes)).size
        regions = [{"x": 0, "y": 300, "width": img_size[0] // 4, "height": img_size[1] // 4}]

    factor = int(os.environ.get("TEST_PIX_FACTOR", "15"))

    camera = {
        "id": "test",
        "remote_path": remote_path,
        "pixelate_factor": factor,
        "pixelate": regions,
    }

    result = apply_pixelation(image_bytes, camera)
    assert isinstance(result, bytes) and len(result) > 0

    stem, suffix = os.path.splitext(image_path)
    output_path = f"{stem}_pixelated{suffix}"
    with open(output_path, "wb") as f:
        f.write(result)

    print(f"\nPixelated image saved to: {output_path}")
    print(f"Regions: {regions}  factor: {factor}")
