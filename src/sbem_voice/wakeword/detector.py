"""Synchronous openWakeWord inference, called off the audio event loop."""

from pathlib import Path

import numpy as np


class OpenWakeWordDetector:
    def __init__(self, model_path: Path, threshold: float):
        try:
            from openwakeword.model import Model
        except ImportError as exc:
            raise RuntimeError(
                "openWakeWord is missing; run uv sync from the project root"
            ) from exc
        try:
            self._model = Model(wakeword_models=[str(model_path)], inference_framework="onnx")
        except Exception as exc:
            raise RuntimeError(
                "Could not load openWakeWord ONNX models. See models/README.md for feature models."
            ) from exc
        self._threshold = threshold

    def predict(self, pcm: bytes) -> bool:
        scores = self._model.predict(np.frombuffer(pcm, dtype=np.int16))
        return any(float(score) >= self._threshold for score in scores.values())

    def reset(self):
        self._model.reset()
