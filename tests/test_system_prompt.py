"""Prompt configuration errors must be caught before starting cloud services."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sbem_voice.config import Settings


class SystemPromptTests(unittest.TestCase):
    def test_loads_editable_default_prompt_from_project_root(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {}, clear=True):
            root = Path(tmp)
            (root / "config").mkdir()
            (root / "config/system_prompt.md").write_text(
                "Reply briefly as SBEM.\n", encoding="utf-8"
            )
            settings = Settings.from_env(root)
            self.assertEqual(settings.load_system_prompt(), "Reply briefly as SBEM.")

    def test_custom_path_resolves_against_project_root_and_preserves_unicode(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"SYSTEM_PROMPT_FILE": "italian.txt"}, clear=True),
        ):
            root = Path(tmp)
            (root / "italian.txt").write_text("Sei SBEM. Parla con semplicità.\n", encoding="utf-8")
            settings = Settings.from_env(root)
            self.assertEqual(settings.system_prompt_file, root / "italian.txt")
            self.assertEqual(settings.load_system_prompt(), "Sei SBEM. Parla con semplicità.")

    def test_missing_empty_or_invalid_utf8_prompt_fails_clearly(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict("os.environ", {"SYSTEM_PROMPT_FILE": "instructions.txt"}, clear=True),
        ):
            root = Path(tmp)
            path = root / "instructions.txt"
            settings = Settings.from_env(root)
            with self.assertRaisesRegex(ValueError, "system prompt"):
                settings.load_system_prompt()
            for content in (b" \n\t", b"\xff\xfe"):
                path.write_bytes(content)
                with (
                    self.subTest(content=content),
                    self.assertRaisesRegex(ValueError, "system prompt"),
                ):
                    settings.load_system_prompt()
