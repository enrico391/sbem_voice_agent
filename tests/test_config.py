import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sbem_voice.config import Settings


class SettingsTests(unittest.TestCase):
    def test_rejects_unknown_tts_provider(self):
        with patch.dict("os.environ", {"TTS_PROVIDER": "unknown"}, clear=True):
            with self.assertRaisesRegex(ValueError, "TTS_PROVIDER"):
                Settings.from_env(Path("/tmp"))

    def test_deepgram_tts_requires_voice_but_not_cartesia_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "wake.onnx").write_bytes(b"test fixture")
            (root / "prompt.md").write_text("Speak briefly.")
            env = {
                "WAKEWORD_MODEL_PATH": "wake.onnx",
                "SYSTEM_PROMPT_FILE": "prompt.md",
                "TTS_PROVIDER": "deepgram",
                "DEEPGRAM_API_KEY": "offline-test",
                "OPENROUTER_API_KEY": "offline-test",
            }
            with patch.dict("os.environ", env, clear=True):
                with self.assertRaisesRegex(ValueError, "DEEPGRAM_TTS_VOICE"):
                    Settings.from_env(root).validate_runtime()
            with patch.dict("os.environ", {**env, "DEEPGRAM_TTS_VOICE": "test-voice"}, clear=True):
                Settings.from_env(root).validate_runtime()
            with patch.dict("os.environ", {**env, "TTS_PROVIDER": "cartesia"}, clear=True):
                with self.assertRaisesRegex(ValueError, "CARTESIA_API_KEY"):
                    Settings.from_env(root).validate_runtime()

    def test_rejects_invalid_timeout_and_threshold(self):
        for key, values in {
            "CONVERSATION_IDLE_SECS": ["0", "-1", "nan", "inf", "oops"],
            "WAKEWORD_THRESHOLD": ["0", "1.1", "nan"],
        }.items():
            for value in values:
                with (
                    self.subTest(key=key, value=value),
                    patch.dict("os.environ", {key: value}, clear=True),
                    self.assertRaises(ValueError),
                ):
                    Settings.from_env(Path("/tmp"))

    def test_resolves_paths_relative_to_project_not_working_directory(self):
        with patch.dict(
            "os.environ",
            {"WAKEWORD_MODEL_PATH": "models/sbem.onnx", "ROBOT_CONFIG": "config/robot.json"},
            clear=True,
        ):
            settings = Settings.from_env(Path("/tmp/project"))
        self.assertEqual(settings.wakeword_model_path, Path("/tmp/project/models/sbem.onnx"))
        self.assertEqual(settings.robot_config, Path("/tmp/project/config/robot.json"))

    def test_old_mcp_configuration_requires_explicit_migration(self):
        with patch.dict("os.environ", {"MCP_SERVERS_CONFIG": "old.json"}, clear=True):
            with self.assertRaisesRegex(ValueError, "ROBOT_CONFIG"):
                Settings.from_env(Path("/tmp"))

    def test_runtime_requires_real_model_and_keys(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {}, clear=True):
            settings = Settings.from_env(Path(tmp))
            with self.assertRaisesRegex(ValueError, "WAKEWORD_MODEL_PATH"):
                settings.validate_runtime()
            model = Path(tmp) / "wake.onnx"
            model.write_bytes(b"test fixture")
            with patch.dict("os.environ", {"WAKEWORD_MODEL_PATH": str(model)}, clear=True):
                settings = Settings.from_env(Path(tmp))
            with self.assertRaisesRegex(ValueError, "DEEPGRAM_API_KEY"):
                settings.validate_runtime()
