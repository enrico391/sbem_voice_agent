"""Bounded conversion of ROS color images to JPEG snapshots for the LLM."""

import base64
import binascii
import io

from PIL import Image

MAX_IMAGE_BYTES = 10 * 1024 * 1024
_ENCODINGS = {
    "rgb8": ("RGB", "RGB", 3),
    "bgr8": ("RGB", "BGR", 3),
    "rgba8": ("RGBA", "RGBA", 4),
    "bgra8": ("RGBA", "BGRA", 4),
    "mono8": ("L", "L", 1),
}


def encode_camera_image(message, max_dimension):
    """Validate a sensor_msgs/Image, honoring row stride, and return JPEG plus size."""
    if not isinstance(message, dict):
        raise ValueError("Invalid camera image message")
    width, height, step = (message.get(key) for key in ("width", "height", "step"))
    if any(type(value) is not int or value <= 0 for value in (width, height, step)):
        raise ValueError("Camera image dimensions and row stride must be positive integers")
    if width > 8192 or height > 8192 or step * height > MAX_IMAGE_BYTES:
        raise ValueError("Camera image exceeds the supported input size")
    encoding = message.get("encoding")
    if not isinstance(encoding, str) or encoding not in _ENCODINGS:
        raise ValueError("Camera encoding must be rgb8, bgr8, rgba8, bgra8, or mono8")
    mode, raw_mode, channels = _ENCODINGS[encoding]
    if step < width * channels:
        raise ValueError("Camera row stride is smaller than its pixel data")
    data = message.get("data")
    if isinstance(data, str):
        if len(data) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise ValueError("Camera image exceeds the supported input size")
        try:
            pixels = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Camera image data is not valid base64") from exc
    elif isinstance(data, list):
        if len(data) > MAX_IMAGE_BYTES or any(
            type(v) is not int or not 0 <= v <= 255 for v in data
        ):
            raise ValueError("Camera image data must contain bytes")
        pixels = bytes(data)
    else:
        raise ValueError("Camera image data must be base64 or a byte array")
    if len(pixels) != step * height:
        raise ValueError("Camera image data length does not match its dimensions and row stride")
    with Image.frombytes(mode, (width, height), pixels, "raw", raw_mode, step, 1) as raw:
        image = raw.convert("RGB")
    with image:
        image.thumbnail((max_dimension, max_dimension))
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=85)
        return output.getvalue(), image.size
