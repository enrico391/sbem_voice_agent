"""Capture native microphone rates while preserving the bot's 16 kHz audio contract."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pyaudio
from pipecat.clocks.system_clock import SystemClock
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager

from sbem_voice.audio.transport import create_transport


class CaptureStream:
    def __init__(self, options):
        self.options = options
        self.closed = False

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


class Microphone:
    """Hardware boundary: model device 26's supported mono int16 capture rates."""

    def __init__(self, rates=(44100, 48000), error=None):
        self.rates = rates
        self.error = error
        self.stream = None

    def get_default_input_device_info(self):
        return self.get_device_info_by_index(26)

    def get_device_info_by_index(self, index):
        if index != 26:
            raise ValueError("Invalid device", pyaudio.paInvalidDevice)
        return {
            "index": 26,
            "structVersion": 2,
            "name": "USB Microphone: Audio (hw:4,0)",
            "hostApi": 0,
            "maxInputChannels": 1,
            "maxOutputChannels": 0,
            "defaultLowInputLatency": 0.01,
            "defaultLowOutputLatency": 0.0,
            "defaultHighInputLatency": 0.1,
            "defaultHighOutputLatency": 0.0,
            "defaultSampleRate": 44100.0,
        }

    def get_format_from_width(self, width):
        return pyaudio.get_format_from_width(width)

    def is_format_supported(self, rate, **options):
        self.get_device_info_by_index(options["input_device"])
        if self.error:
            raise self.error
        if rate not in self.rates:
            raise ValueError("Invalid sample rate", pyaudio.paInvalidSampleRate)
        return True

    def open(self, **options):
        try:
            device = options["input_device_index"]
            self.is_format_supported(options["rate"], input_device=26 if device is None else device)
        except ValueError as exc:
            raise OSError(exc.args[1], exc.args[0]) from exc
        self.stream = CaptureStream(options)
        return self.stream


class AudioTransportTests(unittest.IsolatedAsyncioTestCase):
    async def make_input(self, microphone, device=26):
        with patch("pipecat.transports.local.audio.pyaudio.PyAudio", return_value=microphone):
            transport = create_transport(SimpleNamespace(input_device=device))
        capture = transport.input()
        self.addAsyncCleanup(capture.cleanup)
        await capture.setup(
            FrameProcessorSetup(
                clock=SystemClock(), task_manager=TaskManager(), pipeline_worker=None
            )
        )
        # Exercise the callback-to-queue boundary without starting downstream services.
        capture._audio_in_queue = asyncio.Queue()
        return capture

    async def test_device_26_opens_at_native_rate_and_delivers_resampled_pcm(self):
        microphone = Microphone()
        capture = await self.make_input(microphone)
        self.assertEqual(microphone.stream.options["input_device_index"], 26)
        self.assertEqual(microphone.stream.options["rate"], 44100)
        self.assertEqual(capture.sample_rate, 16000)
        callback = microphone.stream.options["stream_callback"]
        # One second of a 1 kHz tone, split into the actual 20 ms capture chunks.
        tone = (10000 * np.sin(2 * np.pi * 1000 * np.arange(44100) / 44100)).astype(np.int16)
        for chunk in np.split(tone, 50):
            self.assertEqual(callback(chunk.tobytes(), 882, {}, 0), (None, pyaudio.paContinue))
        frames = []
        size = 0
        while size < 30000:
            frame = await asyncio.wait_for(capture._audio_in_queue.get(), 1)
            frames.append(frame)
            size += len(frame.audio)
        while not capture._audio_in_queue.empty():
            frames.append(capture._audio_in_queue.get_nowait())
        self.assertTrue(frames)
        self.assertTrue(all(f.sample_rate == 16000 and f.num_channels == 1 for f in frames))
        samples = np.frombuffer(b"".join(f.audio for f in frames), dtype=np.int16)
        # Streaming filters retain a short tail; duration and pitch must be preserved.
        self.assertGreater(len(samples), 15000)
        self.assertLessEqual(len(samples), 16000)
        spectrum = abs(np.fft.rfft(samples[1000:9000]))
        self.assertEqual(np.argmax(spectrum), 500)  # 1 kHz in 8000 samples at 16 kHz

    async def test_supported_16khz_capture_is_not_resampled(self):
        microphone = Microphone(rates=(16000, 44100))
        capture = await self.make_input(microphone)
        self.assertEqual(microphone.stream.options["rate"], 16000)
        pcm = b"\x01\x00" * 320
        microphone.stream.options["stream_callback"](pcm, 320, {}, 0)
        frame = await asyncio.wait_for(capture._audio_in_queue.get(), 1)
        self.assertEqual(frame.audio, pcm)
        self.assertEqual(frame.sample_rate, 16000)

    async def test_default_input_can_also_use_its_native_rate(self):
        microphone = Microphone()
        await self.make_input(microphone, device=None)
        self.assertEqual(microphone.stream.options["input_device_index"], 26)
        self.assertEqual(microphone.stream.options["rate"], 44100)

    async def test_non_rate_errors_are_not_hidden_by_fallback(self):
        microphone = Microphone(error=ValueError("Invalid number of channels", -9998))
        with self.assertRaisesRegex((ValueError, OSError), "Invalid number of channels"):
            await self.make_input(microphone)
        self.assertIsNone(microphone.stream)
