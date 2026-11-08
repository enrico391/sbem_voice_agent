"""Deepgram recognition with automatic multilingual transcription."""

from pipecat.services.deepgram.stt import DeepgramSTTService

from sbem_voice.config import Settings


def create_stt(settings: Settings) -> DeepgramSTTService:
    # Muted audio still passes through so the echo-tail strategy can unmute STT.
    return DeepgramSTTService(
        api_key=settings.deepgram_api_key,
        audio_passthrough=True,
        settings=DeepgramSTTService.Settings(language="multi"),
    )
