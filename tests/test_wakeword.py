import asyncio
import threading
import unittest
from types import SimpleNamespace

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    FunctionCallResultFrame,
    FunctionCallResultProperties,
    FunctionCallsStartedFrame,
    InputAudioRawFrame,
    LLMFullResponseStartFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager

from sbem_voice.wakeword.processors import AwakeTranscriptFilter, WakeWordGate
from sbem_voice.wakeword.session import ConversationSession


class Detector:
    def __init__(self, detect_at=1):
        self.chunks = []
        self.detect_at = detect_at
        self.resets = 0
        self.thread_ids = []

    def predict(self, pcm):
        self.thread_ids.append(threading.get_ident())
        self.chunks.append(pcm)
        return len(self.chunks) == self.detect_at

    def reset(self):
        self.resets += 1


class WakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_intermediate_tool_update_keeps_session_awake_until_final_result(self):
        await self.session.wake()
        await self.session.process_frame(
            FunctionCallsStartedFrame(function_calls=[SimpleNamespace(tool_call_id="moving")])
        )
        await self.session.process_frame(
            FunctionCallResultFrame(
                function_name="navigate_to",
                tool_call_id="moving",
                arguments={},
                result={"status": "in_progress"},
                properties=FunctionCallResultProperties(is_final=False),
            )
        )
        await self.session.response_finished()
        await asyncio.sleep(0.08)
        self.assertTrue(self.session.awake)
        await self.session.process_frame(
            FunctionCallResultFrame(
                function_name="navigate_to",
                tool_call_id="moving",
                arguments={},
                result={"status": "succeeded"},
            )
        )
        await self.session.response_finished()
        await asyncio.sleep(0.08)
        self.assertFalse(self.session.awake)

    async def asyncSetUp(self):
        self.tasks = TaskManager()
        self.setup = FrameProcessorSetup(clock=None, task_manager=self.tasks, pipeline_worker=None)
        self.session = ConversationSession(idle_secs=0.04)
        self.detector = Detector(detect_at=2)
        self.gate = WakeWordGate(self.session, self.detector)
        await self.gate.setup(self.setup)
        self.sent = []

        async def collect(frame, direction=FrameDirection.DOWNSTREAM):
            self.sent.append(frame)

        self.gate.push_frame = collect
        self.guard = AwakeTranscriptFilter(self.session)
        self.guard.push_frame = collect

    async def asyncTearDown(self):
        await self.session.cleanup()
        await self.gate.cleanup()
        await self.guard.cleanup()
        self.assertEqual(list(self.tasks.current_tasks()), [])

    async def audio(self, pcm=b"\x01\x00" * 320, rate=16000):
        await self.gate.process_frame(
            InputAudioRawFrame(audio=pcm, sample_rate=rate, num_channels=1),
            FrameDirection.DOWNSTREAM,
        )
        await self.gate._jobs.join()

    async def test_sleep_blocks_audio_but_not_control_frames(self):
        await self.audio()
        control = Frame()
        await self.gate.process_frame(control, FrameDirection.DOWNSTREAM)
        self.assertEqual(self.sent, [control])
        self.assertFalse(self.session.awake)

    async def test_detects_exact_chunks_off_thread_and_preserves_command_remainder(self):
        # One frame ends in a detection chunk; the rest may already contain a command.
        pcm = b"\x01\x00" * 1280 + b"\x02\x00" * 1280 + b"\x03\x00" * 400
        await self.audio(pcm)
        self.assertTrue(self.session.awake)
        self.assertEqual(self.detector.chunks, [b"\x01\x00" * 1280, b"\x02\x00" * 1280])
        self.assertTrue(all(t != threading.get_ident() for t in self.detector.thread_ids))
        self.assertEqual(
            b"".join(f.audio for f in self.sent), b"\x02\x00" * 1280 + b"\x03\x00" * 400
        )
        self.assertTrue(all(f.sample_rate == 16000 for f in self.sent))
        self.sent.clear()
        await self.audio()
        self.assertEqual(len(self.detector.chunks), 2)
        self.assertEqual(len(self.sent), 1)

    async def test_partial_chunks_are_retained_until_detection(self):
        self.detector.detect_at = 1
        for _ in range(3):
            await self.audio()
        self.assertFalse(self.session.awake)
        await self.audio()
        self.assertTrue(self.session.awake)
        self.assertEqual(len(self.detector.chunks[0]), 2560)

    async def test_resamples_8khz_input_for_wake_detector(self):
        self.detector.detect_at = 1
        for _ in range(15):
            await self.audio(b"\x01\x00" * 160, rate=8000)
        self.assertTrue(self.session.awake)
        self.assertEqual(len(self.detector.chunks[0]), 2560)
        self.assertEqual(self.sent[0].sample_rate, 8000)

    async def test_initial_wake_without_command_expires_and_late_transcripts_are_blocked(self):
        await self.session.wake()
        await asyncio.sleep(0.07)
        self.assertFalse(self.session.awake)
        await self.guard.process_frame(
            TranscriptionFrame(text="late", user_id="", timestamp=""), FrameDirection.DOWNSTREAM
        )
        self.assertEqual(self.sent, [])
        await self.session.wake()
        transcript = TranscriptionFrame(text="hello", user_id="", timestamp="")
        await self.guard.process_frame(transcript, FrameDirection.DOWNSTREAM)
        self.assertEqual(self.sent, [transcript])

    async def test_pending_response_and_speech_pause_idle_then_restart(self):
        await self.session.wake()
        await self.session.user_started()
        await self.session.user_stopped(has_text=True)
        await asyncio.sleep(0.07)
        self.assertTrue(self.session.awake)
        await self.session.process_frame(LLMFullResponseStartFrame())
        await self.session.process_frame(BotStartedSpeakingFrame())
        await self.session.response_finished()  # after the output transport
        await asyncio.sleep(0.07)
        self.assertTrue(self.session.awake)
        await self.session.process_frame(BotStoppedSpeakingFrame())
        await asyncio.sleep(0.02)
        self.assertTrue(self.session.awake)
        await asyncio.sleep(0.05)
        self.assertFalse(self.session.awake)

    async def test_empty_turn_and_silent_response_rearm_idle(self):
        for has_text in (False, True):
            await self.session.wake()
            await self.session.user_started()
            await self.session.user_stopped(has_text=has_text)
            if has_text:
                await self.session.process_frame(LLMFullResponseStartFrame())
                await self.session.response_finished()
            await asyncio.sleep(0.07)
            self.assertFalse(self.session.awake)

    async def test_multiple_tools_keep_session_awake_until_resulting_response(self):
        await self.session.wake()
        calls = [SimpleNamespace(tool_call_id="one"), SimpleNamespace(tool_call_id="two")]
        await self.session.process_frame(FunctionCallsStartedFrame(function_calls=calls))
        await self.session.process_frame(
            FunctionCallResultFrame(function_name="a", tool_call_id="one", arguments={}, result={})
        )
        await asyncio.sleep(0.07)
        self.assertTrue(self.session.awake)
        await self.session.process_frame(
            FunctionCallResultFrame(function_name="a", tool_call_id="two", arguments={}, result={})
        )
        await self.session.response_finished()
        await asyncio.sleep(0.07)
        self.assertFalse(self.session.awake)

    async def test_late_turn_callback_cannot_reopen_completed_response(self):
        await self.session.wake()
        await self.session.user_started()
        await self.session.process_frame(LLMFullResponseStartFrame())
        await self.session.response_finished()
        # Pipecat's event callbacks are scheduled in tasks, while frames flow.
        await self.session.user_stopped(has_text=True)
        await asyncio.sleep(0.07)
        self.assertFalse(self.session.awake)

    async def test_empty_turn_finalization_uses_observer_fifo(self):
        from pipecat.frames.frames import UserStartedSpeakingFrame, UserStoppedSpeakingFrame
        from pipecat.observers.base_observer import FramePushed

        from sbem_voice.wakeword.processors import SessionActivityObserver, TurnFinalizedFrame

        observer = SessionActivityObserver(self.session)
        await self.session.wake()
        for frame in [
            UserStartedSpeakingFrame(),
            UserStoppedSpeakingFrame(),
            TurnFinalizedFrame(has_text=False),
        ]:
            await observer.on_push_frame(
                FramePushed(
                    source=None,
                    destination=None,
                    frame=frame,
                    direction=FrameDirection.DOWNSTREAM,
                    timestamp=0,
                )
            )
        await asyncio.sleep(0.07)
        self.assertFalse(self.session.awake)

    async def test_slow_detector_keeps_backlog_bounded_and_control_flow_responsive(self):
        import time

        from pipecat.frames.frames import CancelFrame

        def slow_predict(pcm):
            time.sleep(0.15)
            return False

        self.detector.predict = slow_predict
        started = asyncio.get_running_loop().time()
        for _ in range(50):
            await self.gate.process_frame(
                InputAudioRawFrame(audio=b"\x01\x00" * 1280, sample_rate=16000, num_channels=1),
                FrameDirection.DOWNSTREAM,
            )
        control = Frame()
        await self.gate.process_frame(control, FrameDirection.DOWNSTREAM)
        self.assertIn(control, self.sent)
        self.assertLess(asyncio.get_running_loop().time() - started, 0.1)
        self.assertLessEqual(self.gate._jobs.qsize(), 4)
        await self.gate.process_frame(CancelFrame(), FrameDirection.DOWNSTREAM)

    async def test_sleep_rearms_detector_and_clears_partial_audio(self):
        await self.audio()
        await self.session.wake()
        await self.session.sleep()
        self.detector.detect_at = 1
        await self.audio(b"\x02\x00" * 1280)
        self.assertTrue(self.session.awake)
        self.assertEqual(self.detector.chunks, [b"\x02\x00" * 1280])
        self.assertEqual(self.detector.resets, 1)
