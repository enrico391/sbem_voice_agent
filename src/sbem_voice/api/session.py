"""One native Pipecat conversation per API connection, independent of audio."""

import asyncio

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frameworks.rtvi.observer import (
    RTVIFunctionCallReportLevel,
    RTVIObserverParams,
)
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

from sbem_voice.memory.store import MemoryStore
from sbem_voice.memory.tools import memory_functions
from sbem_voice.resources import AppResources
from sbem_voice.robot.tools import robot_functions
from sbem_voice.services.llm import create_llm

API_INSTRUCTIONS = (
    "This conversation is a text-only API session with its own history. "
    "No microphone, speaker or wake word is used. Reply directly to typed messages. "
    "The end_conversation tool is unavailable in this session: answer a goodbye "
    "with a brief farewell; the client closes the connection when finished. "
    "Saved memories use the application's configured profile, shared across sessions."
)


async def create_api_worker(transport, settings, robot):
    """Construct fresh context/services, sharing only the application resources."""
    memory = await asyncio.to_thread(MemoryStore, settings.memory_db_path, settings.memory_user_id)
    context = LLMContext(
        messages=[{"role": "developer", "content": API_INSTRUCTIONS}],
        tools=[*memory_functions, *(robot_functions if robot else [])],
    )
    user, assistant = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(user_turn_strategies=ExternalUserTurnStrategies()),
    )
    llm = create_llm(settings)
    return PipelineWorker(
        Pipeline([transport.input(), user, llm, transport.output(), assistant]),
        app_resources=AppResources(memory=memory, robot=robot),
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        enable_rtvi=True,
        rtvi_observer_params=RTVIObserverParams(
            llm_raw_text_enabled=True,
            function_call_report_level={"*": RTVIFunctionCallReportLevel.NAME},
        ),
        idle_timeout_secs=None,
    )
