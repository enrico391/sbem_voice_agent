"""Native worker integration: real frames and STT base, no audio devices or APIs."""

import asyncio
import unittest

from pipecat.frames.frames import EndFrame, InputAudioRawFrame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import STTService
from pipecat.workers.runner import WorkerRunner

from sbem_voice.wakeword.processors import (
    AwakeTranscriptFilter,
    SessionActivityObserver,
    WakeWordGate,
)
from sbem_voice.wakeword.session import ConversationSession


class SyntheticDetector:
    def predict(self, pcm):
        return any(pcm)

    def reset(self):
        pass


class RecordingSTT(STTService):
    def __init__(self):
        super().__init__(settings=STTSettings(model=None, language=None), ttfs_p99_latency=1.0)
        self.audio = []

    async def run_stt(self, audio):
        self.audio.append(audio)
        yield TranscriptionFrame(text="recognized", user_id="", timestamp="")


class TranscriptCollector(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.transcripts = []
        self.received = asyncio.Event()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            self.transcripts.append(frame.text)
            self.received.set()
        await self.push_frame(frame, direction)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_worker_blocks_sleeping_audio_and_rearms_after_idle(self):
        session = ConversationSession(idle_secs=0.04)
        stt = RecordingSTT()
        collector = TranscriptCollector()
        worker = PipelineWorker(
            Pipeline(
                [
                    WakeWordGate(session, SyntheticDetector()),
                    stt,
                    AwakeTranscriptFilter(session),
                    collector,
                ]
            ),
            enable_rtvi=False,
            idle_timeout_secs=None,
            observers=[SessionActivityObserver(session)],
        )
        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await runner.add_workers(worker)
        started = asyncio.Event()

        @worker.event_handler("on_pipeline_started")
        async def ready(worker, frame):
            started.set()

        run = asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(started.wait(), 3)
            await worker.queue_frames(
                [
                    InputAudioRawFrame(audio=b"\x00\x00" * 1280, sample_rate=16000, num_channels=1),
                    InputAudioRawFrame(audio=b"\x01\x00" * 1280, sample_rate=16000, num_channels=1),
                ]
            )
            await asyncio.wait_for(collector.received.wait(), 3)
            self.assertEqual(stt.audio, [b"\x01\x00" * 1280])
            self.assertEqual(collector.transcripts, ["recognized"])
            await asyncio.sleep(0.08)
            self.assertFalse(session.awake)
            collector.received.clear()
            await worker.queue_frames(
                [
                    TranscriptionFrame(text="late echo", user_id="", timestamp=""),
                    InputAudioRawFrame(audio=b"\x00\x00" * 1280, sample_rate=16000, num_channels=1),
                    InputAudioRawFrame(audio=b"\x02\x00" * 1280, sample_rate=16000, num_channels=1),
                ]
            )
            await asyncio.wait_for(collector.received.wait(), 3)
            self.assertEqual(collector.transcripts, ["recognized", "recognized"])
            self.assertEqual(len(stt.audio), 2)
            await worker.queue_frames([EndFrame()])
            await asyncio.wait_for(run, 3)
        finally:
            if not run.done():
                await runner.cancel()
                await run
