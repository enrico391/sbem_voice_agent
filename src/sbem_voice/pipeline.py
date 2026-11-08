"""Compose native Pipecat services with wake detection and direct robot tools."""

import asyncio

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.transports.base_transport import BaseTransport
from pipecat.workers.runner import WorkerRunner

from sbem_voice.audio.speaker_mute import SpeakerMuteStrategy
from sbem_voice.config import Settings
from sbem_voice.robot.tools import connect_robot, robot_functions
from sbem_voice.services.llm import create_llm
from sbem_voice.services.stt import create_stt
from sbem_voice.services.tts import create_tts
from sbem_voice.wakeword.detector import OpenWakeWordDetector
from sbem_voice.wakeword.processors import (
    AwakeTranscriptFilter,
    ResponseCompletionProcessor,
    SessionActivityObserver,
    TurnFinalizedFrame,
    WakeWordGate,
)
from sbem_voice.wakeword.session import ConversationSession


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments, settings: Settings):
    settings.validate_runtime()
    detector = await asyncio.to_thread(
        OpenWakeWordDetector, settings.wakeword_model_path, settings.wakeword_threshold
    )
    session = ConversationSession(settings.conversation_idle_secs)
    args = runner_args.cli_args
    async with connect_robot(settings.robot_config) as robot:
        stt = create_stt(settings)
        llm = create_llm(settings)
        tts = create_tts(settings)
        context = LLMContext(tools=robot_functions if robot else [])
        user, assistant = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                vad_analyzer=SileroVADAnalyzer(
                    params=VADParams(confidence=0.8, stop_secs=0.2, min_volume=0.5)
                ),
                user_mute_strategies=[]
                if getattr(args, "allow_interruptions", False)
                else [
                    SpeakerMuteStrategy(stt, echo_tail_secs=getattr(args, "echo_tail_secs", 0.8))
                ],
                # The session owns one native idle controller shared by wake/output.
                user_idle_timeout=0,
            ),
        )

        @user.event_handler("on_user_turn_stopped")
        async def on_user_turn_stopped(aggregator, strategy, message):
            await aggregator.push_frame(TurnFinalizedFrame(has_text=bool(message.content)))

        pipeline = Pipeline(
            [
                transport.input(),
                WakeWordGate(session, detector),
                stt,
                AwakeTranscriptFilter(session),
                user,
                llm,
                tts,
                transport.output(),
                ResponseCompletionProcessor(),
                assistant,
            ]
        )
        worker = PipelineWorker(
            pipeline,
            app_resources=robot,
            params=PipelineParams(
                audio_in_sample_rate=getattr(args, "input_sample_rate", 16000),
                enable_metrics=True,
                enable_usage_metrics=True,
            ),
            observers=[SessionActivityObserver(session)],
            enable_rtvi=False,
            # Sleeping is normal; keep listening indefinitely for the next wake.
            idle_timeout_secs=None,
            # Fail closed if local wake inference becomes unusable.
            processor_unusable_policy=ProcessorUnusablePolicy.CANCEL,
        )
        runner = WorkerRunner(
            handle_sigint=runner_args.handle_sigint, handle_sigterm=runner_args.handle_sigterm
        )

        @worker.event_handler("on_pipeline_started")
        async def on_pipeline_started(worker, frame):
            logger.info("Microphone and speaker ready; waiting for wake word. Ctrl+C to stop.")

        await runner.add_workers(worker)
        try:
            await runner.run()
        finally:
            await session.cleanup()


async def bot(runner_args: RunnerArguments, settings: Settings):
    from sbem_voice.audio.transport import create_transport

    await run_bot(create_transport(runner_args.cli_args), runner_args, settings)
