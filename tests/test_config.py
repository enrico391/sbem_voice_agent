import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sbem_voice.config import Settings


class SettingsTests(unittest.TestCase):
    def test_session_timeout_default_and_override(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Settings.from_env(Path("/tmp")).session_timeout_secs, 300)
        with patch.dict("os.environ", {"SESSION_TIMEOUT_SECS": "600"}, clear=True):
            self.assertEqual(Settings.from_env(Path("/tmp")).session_timeout_secs, 600)

    def test_memory_defaults_and_custom_user_and_path(self):
        with patch.dict("os.environ", {}, clear=True):
            settings = Settings.from_env(Path("/tmp/project"))
        self.assertEqual(settings.memory_db_path, Path("/tmp/project/data/memory.sqlite3"))
        self.assertEqual(settings.memory_user_id, "default")
        with patch.dict(
            "os.environ",
            {"MEMORY_DB_PATH": "custom/facts.sqlite3", "MEMORY_USER_ID": " alice "},
            clear=True,
        ):
            settings = Settings.from_env(Path("/tmp/project"))
        self.assertEqual(settings.memory_db_path, Path("/tmp/project/custom/facts.sqlite3"))
        self.assertEqual(settings.memory_user_id, "alice")

    def test_invalid_memory_user_fails_early(self):
        for user_id in ["", "   ", "x" * 129]:
            with (
                self.subTest(user_id=user_id),
                patch.dict("os.environ", {"MEMORY_USER_ID": user_id}, clear=True),
                self.assertRaisesRegex(ValueError, "MEMORY_USER_ID"),
            ):
                Settings.from_env(Path("/tmp"))

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
            "SESSION_TIMEOUT_SECS": ["0", "-1", "nan", "inf", "oops"],
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
