"""Existing OpenRouter model, with instructions loaded from the selected prompt file."""

from pipecat.services.openrouter.llm import OpenRouterLLMService

from sbem_voice.config import Settings


def create_llm(settings: Settings) -> OpenRouterLLMService:
    return OpenRouterLLMService(
        api_key=settings.openrouter_api_key,
        settings=OpenRouterLLMService.Settings(
            model=settings.llm_model, system_instruction=settings.load_system_prompt()
        ),
    )
