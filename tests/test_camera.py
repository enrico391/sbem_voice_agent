"""ROS image conversion without a camera, ROS runtime, or provider calls."""

import base64
import io
import unittest

from PIL import Image

from sbem_voice.robot.camera import encode_camera_image


def raw_image(*, width=2, height=1, encoding="rgb8", step=6, data=None):
    return {
        "width": width,
        "height": height,
        "encoding": encoding,
        "step": step,
        "is_bigendian": 0,
        "data": base64.b64encode(data if data is not None else b"\xff\x00\x00" * 2).decode(),
    }


class CameraEncodingTests(unittest.TestCase):
    def test_rgb_and_bgr_with_row_padding_produce_correct_jpeg_colors(self):
        for encoding, pixel in (("rgb8", b"\xff\x00\x00"), ("bgr8", b"\x00\x00\xff")):
            message = raw_image(
                width=2,
                height=2,
                encoding=encoding,
                step=8,
                data=(pixel * 2 + b"\x00\x00") * 2,
            )
            jpeg, size = encode_camera_image(message, 1024)
            self.assertEqual(size, (2, 2))
            with Image.open(io.BytesIO(jpeg)) as image:
                self.assertEqual(image.format, "JPEG")
                red, green, blue = image.getpixel((0, 0))
                self.assertGreater(red, 240)
                self.assertLess(green, 15)
                self.assertLess(blue, 15)

    def test_alpha_grayscale_and_byte_array_inputs(self):
        for encoding, data in (
            ("rgba8", [255, 0, 0, 255] * 2),
            ("bgra8", [0, 0, 255, 255] * 2),
            ("mono8", [128, 128]),
        ):
            message = raw_image(encoding=encoding, step=len(data))
            message["data"] = data
            jpeg, size = encode_camera_image(message, 1024)
            self.assertEqual(size, (2, 1))
            with Image.open(io.BytesIO(jpeg)) as image:
                self.assertEqual(image.mode, "RGB")

    def test_snapshot_is_resized_preserving_aspect_ratio(self):
        jpeg, size = encode_camera_image(
            raw_image(width=8, height=4, step=24, data=b"\x80" * 96), 4
        )
        self.assertEqual(size, (4, 2))
        with Image.open(io.BytesIO(jpeg)) as image:
            self.assertEqual(image.size, size)

    def test_rejects_malformed_unsupported_and_oversized_images(self):
        for changes in (
            {"width": True},
            {"height": 0},
            {"width": 100000},
            {"step": 1},
            {"encoding": "16UC1"},
            {"data": "not base64!"},
            {"data": []},
            {"data": [True] * 6},
            {"data": [256] * 6},
            {"step": 20_000_000},
            {"data": "A" * 15_000_000},
        ):
            with self.subTest(changes=str(changes)[:100]), self.assertRaises(ValueError):
                encode_camera_image({**raw_image(), **changes}, 1024)
