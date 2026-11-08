import unittest
from unittest.mock import patch

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InputAudioRawFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.stt_service import STTService

from sbem_voice.audio.speaker_mute import SpeakerMuteStrategy


class LocalSTT(STTService):
    """Use real STT muting, with no network or pipeline scheduling."""

    def __init__(self):
        super().__init__()
        self.received_audio = []

    async def queue_frame(self, frame, direction=FrameDirection.DOWNSTREAM, callback=None):
        await self.process_frame(frame, direction)

    async def run_stt(self, audio):
        self.received_audio.append(audio)
        yield None


class SpeakerMuteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stt = LocalSTT()
        self.strategy = SpeakerMuteStrategy(self.stt)
        self.audio = InputAudioRawFrame(audio=b"\x01\x00" * 320, sample_rate=16000, num_channels=1)

    async def capture(self):
        await self.stt.process_audio_frame(self.audio, FrameDirection.DOWNSTREAM)

    async def test_speaker_audio_never_reaches_recognition(self):
        await self.capture()
        self.assertEqual(len(self.stt.received_audio), 1)
        self.assertTrue(await self.strategy.process_frame(BotStartedSpeakingFrame()))
        await self.capture()
        self.assertEqual(len(self.stt.received_audio), 1)

    async def test_late_echo_is_suppressed_then_listening_resumes(self):
        with patch("sbem_voice.audio.speaker_mute.monotonic", return_value=10.0) as clock:
            await self.strategy.process_frame(BotStartedSpeakingFrame())
            clock.return_value = 20.0
            self.assertTrue(await self.strategy.process_frame(BotStoppedSpeakingFrame()))
            clock.return_value = 20.7
            echo = TranscriptionFrame(text="speaker echo", user_id="", timestamp="")
            self.assertTrue(await self.strategy.process_frame(echo))
            await self.capture()
            self.assertEqual(self.stt.received_audio, [])
            clock.return_value = 20.81
            self.assertFalse(await self.strategy.process_frame(self.audio))
            await self.capture()
            self.assertEqual(len(self.stt.received_audio), 1)

    async def test_new_playback_extends_the_mute_window(self):
        with patch("sbem_voice.audio.speaker_mute.monotonic", return_value=10.0) as clock:
            await self.strategy.process_frame(BotStartedSpeakingFrame())
            clock.return_value = 11.0
            await self.strategy.process_frame(BotStoppedSpeakingFrame())
            clock.return_value = 11.5
            await self.strategy.process_frame(BotStartedSpeakingFrame())
            clock.return_value = 12.0
            self.assertTrue(await self.strategy.process_frame(Frame()))
            clock.return_value = 13.0
            await self.strategy.process_frame(BotStoppedSpeakingFrame())
            clock.return_value = 13.7
            self.assertTrue(await self.strategy.process_frame(Frame()))
            clock.return_value = 13.81
            self.assertFalse(await self.strategy.process_frame(Frame()))


if __name__ == "__main__":
    unittest.main()
