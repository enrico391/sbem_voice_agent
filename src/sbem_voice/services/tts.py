"""Existing Cartesia voice configuration."""

from pipecat.services.cartesia.tts import CartesiaTTSService

from sbem_voice.config import Settings


def create_tts(settings: Settings) -> CartesiaTTSService:
    return CartesiaTTSService(
        api_key=settings.cartesia_api_key,
        settings=CartesiaTTSService.Settings(voice=settings.cartesia_voice_id),
    )
