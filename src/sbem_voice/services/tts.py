"""Select the configured streaming speech synthesis provider."""

from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.tts import DeepgramTTSService
from pipecat.services.tts_service import TTSService

from sbem_voice.config import Settings


def create_tts(settings: Settings) -> TTSService:
    if settings.tts_provider == "deepgram":
        return DeepgramTTSService(
            api_key=settings.deepgram_api_key,
            settings=DeepgramTTSService.Settings(voice=settings.deepgram_tts_voice),
        )
    return CartesiaTTSService(
        api_key=settings.cartesia_api_key,
        settings=CartesiaTTSService.Settings(voice=settings.cartesia_voice_id),
    )
