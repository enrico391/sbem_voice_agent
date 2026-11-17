"""Local audio with native-rate microphone capture and pipeline-rate resampling."""

import asyncio

import pyaudio
from loguru import logger
from pipecat.audio.resamplers.soxr_stream_resampler import SOXRStreamAudioResampler
from pipecat.frames.frames import InputAudioRawFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.local.audio import (
    LocalAudioInputTransport,
    LocalAudioTransport,
    LocalAudioTransportParams,
)


class NativeRateAudioInput(LocalAudioInputTransport):
    """Keep the selected hardware device when it cannot capture at the bot's rate."""

    async def setup(self, setup):
        # Initialize Pipecat at the pipeline rate, then open hardware independently.
        # LocalAudioInputTransport.setup opens hardware at the pipeline rate directly.
        await BaseInputTransport.setup(self, setup)
        device_index = self._params.input_device_index
        device = (
            self._py_audio.get_default_input_device_info()
            if device_index is None
            else self._py_audio.get_device_info_by_index(device_index)
        )
        device_index = int(device["index"])
        audio_format = self._py_audio.get_format_from_width(2)
        options = {
            "input_device": device_index,
            "input_channels": self._params.audio_in_channels,
            "input_format": audio_format,
        }
        self._capture_rate = self.sample_rate
        try:
            self._py_audio.is_format_supported(self._capture_rate, **options)
        except ValueError as exc:
            if exc.args[-1] != pyaudio.paInvalidSampleRate:
                raise
            self._capture_rate = int(device["defaultSampleRate"])
            self._py_audio.is_format_supported(self._capture_rate, **options)
            logger.info(
                "Microphone {} ({}): capturing at {} Hz, resampling to {} Hz",
                device_index,
                device["name"],
                self._capture_rate,
                self.sample_rate,
            )
        self._capture_resampler = SOXRStreamAudioResampler()
        self._in_stream = self._py_audio.open(
            format=audio_format,
            channels=self._params.audio_in_channels,
            rate=self._capture_rate,
            frames_per_buffer=self._capture_rate // 50,
            stream_callback=self._audio_in_callback,
            input=True,
            input_device_index=device_index,
            start=False,
        )

    def _audio_in_callback(self, in_data, frame_count, time_info, status):
        # Match the native transport's thread-to-event-loop handoff. Resampling
        # stays on the event loop, with one continuous filter across callbacks.
        asyncio.run_coroutine_threadsafe(self._push_capture_audio(in_data), self.get_event_loop())
        return (None, pyaudio.paContinue)

    async def _push_capture_audio(self, audio):
        audio = await self._capture_resampler.resample(audio, self._capture_rate, self.sample_rate)
        if audio:
            await self.push_audio_frame(
                InputAudioRawFrame(
                    audio=audio,
                    sample_rate=self.sample_rate,
                    num_channels=self._params.audio_in_channels,
                )
            )


class NativeRateAudioTransport(LocalAudioTransport):
    """Use native-rate input while retaining Pipecat's speaker and lifecycle handling."""

    def input(self):
        if self._input is None:
            self._input = NativeRateAudioInput(self._pyaudio, self._params)
        return self._input


def create_transport(args) -> LocalAudioTransport:
    return NativeRateAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            input_device_index=getattr(args, "input_device", None),
            output_device_index=getattr(args, "output_device", None),
            audio_in_sample_rate=getattr(args, "input_sample_rate", 16000),
            audio_out_sample_rate=getattr(args, "output_sample_rate", 48000),
        )
    )
