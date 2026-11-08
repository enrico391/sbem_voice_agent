"""TTS provider selection uses real service constructors without connecting."""

import unittest
from pathlib import Path
from unittest.mock import patch

from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.tts import DeepgramTTSService

from sbem_voice.config import Settings
from sbem_voice.services.tts import create_tts


class TTSTests(unittest.TestCase):
    def test_defaults_to_cartesia_with_configured_voice(self):
        with patch.dict("os.environ", {"CARTESIA_VOICE_ID": "test-cartesia-voice"}, clear=True):
            settings = Settings.from_env(Path("/tmp"))
        service = create_tts(settings)
        self.assertIsInstance(service, CartesiaTTSService)
        self.assertEqual(service._settings.voice, "test-cartesia-voice")

    def test_selects_deepgram_with_configured_voice_and_key(self):
        with patch.dict(
            "os.environ",
            {
                "TTS_PROVIDER": "deepgram",
                "DEEPGRAM_TTS_VOICE": "test-deepgram-voice",
                "DEEPGRAM_API_KEY": "offline-test",
            },
            clear=True,
        ):
            settings = Settings.from_env(Path("/tmp"))
        service = create_tts(settings)
        self.assertIsInstance(service, DeepgramTTSService)
        self.assertEqual(service._settings.voice, "test-deepgram-voice")
        self.assertEqual(service._api_key, "offline-test")
