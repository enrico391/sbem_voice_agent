"""Exercise native tool dispatch with scripted completions and no provider calls."""

import asyncio
import unittest
from types import SimpleNamespace

from openai.types.chat import ChatCompletionChunk
from pipecat.frames.frames import EndFrame, LLMFullResponseEndFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.openrouter.llm import OpenRouterLLMService
from pipecat.workers.runner import WorkerRunner

from sbem_voice.wakeword.processors import ResponseCompletionProcessor, SessionActivityObserver
from sbem_voice.wakeword.session import ConversationSession
from sbem_voice.wakeword.tools import end_conversation


class ConversationPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_end_tool_finishes_farewell_without_ending_worker(self):
        session = ConversationSession(30)
        closed = asyncio.Event()
        replies = []

        class ScriptedLLM(OpenRouterLLMService):
            calls = 0

            async def get_chat_completions(self, context):
                self.calls += 1
                delta = (
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "end-1",
                                "type": "function",
                                "function": {"name": "end_conversation", "arguments": "{}"},
                            }
                        ]
                    }
                    if self.calls == 1
                    else {"content": "Goodbye."}
                )

                async def chunks():
                    yield ChatCompletionChunk(
                        id="offline",
                        created=0,
                        model="offline",
                        object="chat.completion.chunk",
                        choices=[{"index": 0, "delta": delta}],
                    )

                return chunks()

        class Output(FrameProcessor):
            async def setup(self, setup):
                await super().setup(setup)
                await session.setup(setup)

            async def cleanup(self):
                await session.cleanup()
                await super().cleanup()

            async def process_frame(self, frame, direction):
                await super().process_frame(frame, direction)
                if isinstance(frame, LLMFullResponseEndFrame) and session.closing:
                    replies.append("reply drained")
                await self.push_frame(frame, direction)

        class Observer(SessionActivityObserver):
            async def on_push_frame(self, data):
                await super().on_push_frame(data)
                if session.closing and not session.awake:
                    closed.set()

        context = LLMContext(
            messages=[{"role": "user", "content": "Goodbye"}], tools=[end_conversation]
        )
        user, assistant = LLMContextAggregatorPair(context)
        llm = ScriptedLLM(api_key="offline-test")
        worker = PipelineWorker(
            Pipeline([user, llm, Output(), ResponseCompletionProcessor(), assistant]),
            app_resources=SimpleNamespace(session=session),
            enable_rtvi=False,
            idle_timeout_secs=None,
            observers=[Observer(session)],
        )
        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await runner.add_workers(worker)

        @worker.event_handler("on_pipeline_started")
        async def ready(worker, frame):
            await session.wake()
            await worker.queue_frames([LLMRunFrame()])

        run = asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(closed.wait(), 5)
            self.assertFalse(run.done())
            self.assertIn("Goodbye.", str(context.get_messages()))
            self.assertTrue(replies)
            await worker.queue_frames([EndFrame()])
            await asyncio.wait_for(run, 3)
        finally:
            if not run.done():
                await runner.cancel()
                await run
            await llm._client.close()
