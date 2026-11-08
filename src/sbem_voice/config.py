"""Project settings; importing this module never loads credentials or starts services."""

import math
import os
from dataclasses import dataclass
from pathlib import Path


def _positive_float(name: str, default: str) -> float:
    try:
        value = float(os.getenv(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def _path(root: Path, name: str) -> Path | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    path = Path(value).expanduser()
    return (root / path).resolve() if not path.is_absolute() else path.resolve()


@dataclass(frozen=True)
class Settings:
    root: Path
    wakeword_model_path: Path | None
    wakeword_threshold: float
    conversation_idle_secs: float
    robot_config: Path | None
    deepgram_api_key: str
    cartesia_api_key: str
    openrouter_api_key: str
    cartesia_voice_id: str
    tts_provider: str
    deepgram_tts_voice: str
    llm_model: str
    system_prompt_file: Path

    @classmethod
    def from_env(cls, root: Path) -> "Settings":
        if (
            os.getenv("MCP_SERVERS_CONFIG", "").strip()
            and not os.getenv("ROBOT_CONFIG", "").strip()
        ):
            raise ValueError(
                "MCP_SERVERS_CONFIG has been replaced. Set ROBOT_CONFIG=config/robot.json"
            )
        threshold = _positive_float("WAKEWORD_THRESHOLD", "0.5")
        if threshold > 1:
            raise ValueError("WAKEWORD_THRESHOLD must be in (0, 1]")
        tts_provider = os.getenv("TTS_PROVIDER", "cartesia").strip().lower()
        if tts_provider not in ("cartesia", "deepgram"):
            raise ValueError("TTS_PROVIDER must be cartesia or deepgram")
        return cls(
            root=root.resolve(),
            wakeword_model_path=_path(root, "WAKEWORD_MODEL_PATH"),
            wakeword_threshold=threshold,
            conversation_idle_secs=_positive_float("CONVERSATION_IDLE_SECS", "30"),
            robot_config=_path(root, "ROBOT_CONFIG"),
            deepgram_api_key=os.getenv("DEEPGRAM_API_KEY", "").strip(),
            cartesia_api_key=os.getenv("CARTESIA_API_KEY", "").strip(),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            cartesia_voice_id=os.getenv(
                "CARTESIA_VOICE_ID", "86e30c1d-714b-4074-a1f2-1cb6b552fb49"
            ),
            tts_provider=tts_provider,
            deepgram_tts_voice=os.getenv("DEEPGRAM_TTS_VOICE", "").strip(),
            llm_model=os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash"),
            system_prompt_file=_path(root, "SYSTEM_PROMPT_FILE")
            or (root / "config/system_prompt.md").resolve(),
        )

    def load_system_prompt(self) -> str:
        """Read the selected UTF-8 prompt; never silently ignore configuration errors."""
        try:
            prompt = self.system_prompt_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise ValueError(
                f"Cannot read system prompt as UTF-8: {self.system_prompt_file}"
            ) from exc
        if not prompt:
            raise ValueError(f"The system prompt file is empty: {self.system_prompt_file}")
        return prompt

    def validate_runtime(self) -> None:
        path = self.wakeword_model_path
        if path is None or not path.is_file() or path.suffix.lower() != ".onnx":
            raise ValueError("Set WAKEWORD_MODEL_PATH to an existing openWakeWord .onnx model")
        missing = [
            name
            for name, value in (
                ("DEEPGRAM_API_KEY", self.deepgram_api_key),
                ("OPENROUTER_API_KEY", self.openrouter_api_key),
            )
            if not value
        ]
        if self.tts_provider == "cartesia" and not self.cartesia_api_key:
            missing.append("CARTESIA_API_KEY")
        if missing:
            raise ValueError(f"Set the following API keys in .env: {', '.join(missing)}")
        if self.tts_provider == "deepgram" and not self.deepgram_tts_voice:
            raise ValueError("Set DEEPGRAM_TTS_VOICE to your Deepgram TTS voice/model identifier")
        if self.robot_config is not None:
            from sbem_voice.robot.config import RobotConfig

            RobotConfig.load(self.robot_config)
        self.load_system_prompt()
