"""Custom wake gating and ordered adapters around native Pipecat lifecycle."""

import asyncio
from dataclasses import dataclass

from loguru import logger
from pipecat.audio.resamplers.soxr_stream_resampler import SOXRStreamAudioResampler
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    SystemFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from sbem_voice.wakeword.session import ConversationSession


@dataclass
class TurnFinalizedFrame(SystemFrame):
    """Route aggregator callback results through the native observer FIFO."""

    has_text: bool


@dataclass
class ResponseDrainedFrame(SystemFrame):
    """Response output has drained; observed after its start and tool events."""


class WakeWordGate(FrameProcessor):
    """Bound inference work so slow models never stall capture/control frames.

    Retain at most 500 ms of capture and four 80 ms inference jobs. On overload
    discard oldest queued work and reset the detector across audio gaps. The
    native processor's audio queue can drain without waiting for inference.
    """

    CHUNK_BYTES = 1280 * 2

    def __init__(self, session: ConversationSession, detector, **kwargs):
        super().__init__(**kwargs)
        self._session = session
        self._detector = detector
        self._buffer = bytearray()
        self._capture = bytearray()
        self._capture_total = 0
        self._detector_offset = 0
        self._generation = session.generation
        self._resampler = SOXRStreamAudioResampler()
        self._jobs = asyncio.Queue(maxsize=4)
        self._inference_task = None
        self._overloaded = False

    async def setup(self, setup):
        await super().setup(setup)
        await self._session.setup(setup)
        self._inference_task = self.create_task(self._infer(), name="wake-inference")

    def _discard_jobs(self):
        while not self._jobs.empty():
            self._jobs.get_nowait()
            self._jobs.task_done()

    async def _stop_inference(self):
        if self._inference_task is not None:
            await self.cancel_task(self._inference_task)
            self._inference_task = None
        self._discard_jobs()

    async def cleanup(self):
        await self._stop_inference()
        await self._session.cleanup()
        await super().cleanup()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (EndFrame, CancelFrame)):
            await self._stop_inference()
            await self._session.cleanup()
        if direction != FrameDirection.DOWNSTREAM or not isinstance(frame, InputAudioRawFrame):
            await self.push_frame(frame, direction)
            return
        if self._session.awake:
            await self.push_frame(frame, direction)
            return
        if frame.num_channels != 1 or frame.sample_rate not in (8000, 16000):
            raise ValueError("Wake detection requires mono 8 kHz or 16 kHz int16 PCM")
        if self._generation != self._session.generation:
            self._buffer.clear()
            self._capture.clear()
            self._capture_total = 0
            self._detector_offset = 0
            self._discard_jobs()
            self._resampler = SOXRStreamAudioResampler()
            self._generation = self._session.generation
        self._capture.extend(frame.audio)
        self._capture_total += len(frame.audio)
        del self._capture[: -frame.sample_rate]  # 500 ms of mono int16 PCM
        pcm = await self._resampler.resample(frame.audio, frame.sample_rate, 16000)
        self._buffer.extend(pcm)
        while len(self._buffer) >= self.CHUNK_BYTES:
            chunk = bytes(self._buffer[: self.CHUNK_BYTES])
            del self._buffer[: self.CHUNK_BYTES]
            if self._jobs.full():
                self._jobs.get_nowait()
                self._jobs.task_done()
                if not self._overloaded:
                    logger.warning(
                        "Wake inference overloaded; dropping old audio. Tune on the Jetson."
                    )
                    self._overloaded = True
            self._jobs.put_nowait(
                (self._generation, self._detector_offset, chunk, frame.sample_rate)
            )
            self._detector_offset += self.CHUNK_BYTES

    async def _infer(self):
        generation = self._generation
        previous_offset = -self.CHUNK_BYTES
        while True:
            job_generation, offset, chunk, rate = await self._jobs.get()
            try:
                if job_generation != self._session.generation or self._session.awake:
                    continue
                if generation != job_generation or offset != previous_offset + self.CHUNK_BYTES:
                    await asyncio.to_thread(self._detector.reset)
                generation = job_generation
                previous_offset = offset
                detected = await asyncio.to_thread(self._detector.predict, chunk)
                if not detected or job_generation != self._session.generation:
                    continue
                capture_start = self._capture_total - len(self._capture)
                if rate == 16000:
                    # Never activate from audio that fell outside the bounded ring.
                    if offset < capture_start:
                        continue
                    audio = bytes(self._capture[offset - capture_start :])
                else:
                    # Keep native capture rate for STT/VAD. Resampler delay means
                    # a short source pre-roll is safer than estimating its offset.
                    audio = bytes(self._capture)
                self._buffer.clear()
                self._capture.clear()
                self._discard_jobs()
                self._overloaded = False
                await self._session.wake()
                await self.push_frame(
                    InputAudioRawFrame(audio=audio, sample_rate=rate, num_channels=1)
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.push_error(
                    "Wake inference failed", exception=exc, force_treat_as_permanent=True
                )
                return
            finally:
                self._jobs.task_done()


class AwakeTranscriptFilter(FrameProcessor):
    """Ignore transcripts admitted asleep or queued across a sleep/wake boundary."""

    def __init__(self, session: ConversationSession, **kwargs):
        super().__init__(**kwargs)
        self._session = session

    async def queue_frame(self, frame, direction=FrameDirection.DOWNSTREAM, callback=None):
        if direction == FrameDirection.DOWNSTREAM and isinstance(
            frame, (TranscriptionFrame, InterimTranscriptionFrame)
        ):
            frame.metadata["sbem_activation"] = (
                self._session.generation if self._session.awake else None
            )
        await super().queue_frame(frame, direction, callback)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(
            frame, (TranscriptionFrame, InterimTranscriptionFrame)
        ):
            admitted = frame.metadata.get("sbem_activation", self._session.generation)
            if not self._session.awake or admitted != self._session.generation:
                return
        await self.push_frame(frame, direction)


class SessionActivityObserver(BaseObserver):
    """Serialize all lifecycle transitions through one native observer queue."""

    def __init__(self, session: ConversationSession):
        super().__init__(observe_every_push=False)
        self._session = session

    async def on_push_frame(self, data: FramePushed):
        if data.direction != FrameDirection.DOWNSTREAM:
            return
        frame = data.frame
        if isinstance(frame, UserStartedSpeakingFrame):
            await self._session.user_started()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            await self._session.user_stopped(has_text=True)
        elif isinstance(frame, TurnFinalizedFrame):
            await self._session.finalize_turn(has_text=frame.has_text)
        elif isinstance(frame, ResponseDrainedFrame):
            await self._session.response_finished()
        else:
            await self._session.process_frame(frame)


class ResponseCompletionProcessor(FrameProcessor):
    """Emit an ordered marker only after the response crosses the output transport."""

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMFullResponseEndFrame):
            await self.push_frame(ResponseDrainedFrame(), direction)
        await self.push_frame(frame, direction)
