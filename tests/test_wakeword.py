import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    FunctionCallResultFrame,
    FunctionCallResultProperties,
    FunctionCallsStartedFrame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    LLMFullResponseStartFrame,
    LLMMessagesUpdateFrame,
    TranscriptionFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
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
    async def test_playback_transcripts_without_local_voice_are_discarded(self):
        await self.session.wake()
        await self.guard.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        self.sent.clear()
        for cls in (InterimTranscriptionFrame, TranscriptionFrame):
            await self.guard.process_frame(
                cls(text="the bot hears its own reply", user_id="", timestamp=""),
                FrameDirection.DOWNSTREAM,
            )
        self.assertEqual(self.sent, [])

    async def test_echo_tail_blocks_late_transcripts_then_restores_quiet_speech(self):
        await self.session.wake()
        with patch("sbem_voice.wakeword.processors.monotonic", return_value=100.0) as clock:
            await self.guard.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
            await self.guard.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
            self.sent.clear()
            transcript = TranscriptionFrame(text="late echo", user_id="", timestamp="")
            clock.return_value = 100.7
            await self.guard.process_frame(transcript, FrameDirection.DOWNSTREAM)
            self.assertEqual(self.sent, [])
            clock.return_value = 100.81
            await self.guard.process_frame(transcript, FrameDirection.DOWNSTREAM)
            self.assertEqual(self.sent, [transcript])

    async def test_vad_confirmed_interruption_keeps_delayed_final_transcript(self):
        await self.session.wake()
        await self.guard.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await self.guard.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await self.guard.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        self.sent.clear()
        transcript = TranscriptionFrame(text="stop", user_id="", timestamp="")
        await self.guard.process_frame(transcript, FrameDirection.DOWNSTREAM)
        self.assertEqual(self.sent, [transcript])

    async def test_previous_user_turn_does_not_admit_echo_from_next_reply(self):
        await self.session.wake()
        await self.guard.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        await self.guard.process_frame(VADUserStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        await self.guard.process_frame(UserStoppedSpeakingFrame(), FrameDirection.UPSTREAM)
        await self.guard.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        self.sent.clear()
        await self.guard.process_frame(
            TranscriptionFrame(text="next reply echo", user_id="", timestamp=""),
            FrameDirection.DOWNSTREAM,
        )
        self.assertEqual(self.sent, [])

    async def test_playback_audio_still_reaches_local_voice_detection(self):
        await self.session.wake()
        await self.guard.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
        self.sent.clear()
        audio = InputAudioRawFrame(audio=b"\x01\x00" * 320, sample_rate=16000, num_channels=1)
        await self.guard.process_frame(audio, FrameDirection.DOWNSTREAM)
        self.assertEqual(self.sent, [audio])

    async def test_timeout_applies_to_next_transcript_even_without_another_wake(self):
        now = [100.0]
        self.session._clock = lambda: now[0]
        await self.session.wake()
        now[0] = 401.0
        await self.guard.process_frame(
            TranscriptionFrame(text="new request", user_id="", timestamp=""),
            FrameDirection.DOWNSTREAM,
        )
        self.assertIsInstance(self.sent[0], LLMMessagesUpdateFrame)

    async def test_close_during_user_speech_does_not_leave_next_wake_busy(self):
        await self.session.wake()
        await self.session.user_started()
        await self.session.request_end()
        await self.session.process_frame(LLMFullResponseStartFrame())
        await self.session.response_finished()
        self.assertFalse(self.session.awake)
        await self.session.wake()
        await asyncio.sleep(0.08)
        self.assertFalse(self.session.awake)

    async def test_session_context_retained_before_timeout_and_reset_after_idle_gap(self):
        now = [100.0]
        self.session._clock = lambda: now[0]
        await self.session.wake()
        await self.session.sleep()
        now[0] = 399.0
        await self.session.wake()
        frame = TranscriptionFrame(text="follow up", user_id="", timestamp="")
        await self.guard.process_frame(frame, FrameDirection.DOWNSTREAM)
        self.assertEqual(self.sent, [frame])
        await self.session.sleep()
        self.sent.clear()
        now[0] = 700.0
        await self.session.wake()
        await self.guard.process_frame(frame, FrameDirection.DOWNSTREAM)
        self.assertIsInstance(self.sent[0], LLMMessagesUpdateFrame)
        self.assertEqual(self.sent[0].messages, [])
        self.assertFalse(self.sent[0].run_llm)
        self.assertIs(self.sent[1], frame)
        self.sent.clear()
        await self.guard.process_frame(frame, FrameDirection.DOWNSTREAM)
        self.assertEqual(self.sent, [frame])

    async def test_long_response_does_not_expire_context(self):
        now = [100.0]
        self.session._clock = lambda: now[0]
        await self.session.wake()
        await self.session.user_started()
        await self.session.user_stopped(has_text=True)
        now[0] = 1000.0
        await self.session.process_frame(LLMFullResponseStartFrame())
        await self.session.response_finished()
        await self.session.sleep()
        now[0] = 1100.0
        await self.session.wake()
        frame = TranscriptionFrame(text="follow up", user_id="", timestamp="")
        await self.guard.process_frame(frame, FrameDirection.DOWNSTREAM)
        self.assertEqual(self.sent, [frame])

    async def test_end_conversation_blocks_input_finishes_reply_and_resets_next_wake(self):
        await self.session.wake()
        await self.session.request_end()
        self.assertTrue(self.session.awake)
        await self.audio()
        await self.guard.process_frame(
            TranscriptionFrame(text="late input", user_id="", timestamp=""),
            FrameDirection.DOWNSTREAM,
        )
        # Silence lets native VAD finish an already-started turn, without sending
        # microphone speech to STT or admitting another transcript.
        self.assertEqual(len(self.sent), 1)
        self.assertIsInstance(self.sent[0], InputAudioRawFrame)
        self.assertFalse(any(self.sent[0].audio))
        self.assertEqual(len(self.sent[0].audio), 640)
        await self.session.process_frame(LLMFullResponseStartFrame())
        await self.session.process_frame(BotStartedSpeakingFrame())
        await self.session.response_finished()
        self.assertTrue(self.session.awake)
        await self.session.process_frame(BotStoppedSpeakingFrame())
        self.assertFalse(self.session.awake)
        self.detector.detect_at = 1
        await self.audio(b"\x02\x00" * 1280)
        self.assertTrue(self.session.awake)
        self.sent.clear()
        await self.guard.process_frame(
            TranscriptionFrame(text="new conversation", user_id="", timestamp=""),
            FrameDirection.DOWNSTREAM,
        )
        self.assertIsInstance(self.sent[0], LLMMessagesUpdateFrame)

    async def test_end_waits_for_tool_result_and_its_final_response(self):
        await self.session.wake()
        await self.session.process_frame(
            FunctionCallsStartedFrame(function_calls=[SimpleNamespace(tool_call_id="moving")])
        )
        await self.session.request_end()
        await self.session.process_frame(LLMFullResponseStartFrame())
        await self.session.response_finished()
        self.assertTrue(self.session.awake)
        await self.session.process_frame(
            FunctionCallResultFrame(
                function_name="navigate_to",
                tool_call_id="moving",
                arguments={},
                result={"status": "succeeded"},
            )
        )
        # An earlier response's trailing end marker must not close the session.
        await self.session.response_finished()
        self.assertTrue(self.session.awake)
        await self.session.process_frame(LLMFullResponseStartFrame())
        await self.session.response_finished()
        self.assertFalse(self.session.awake)

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
