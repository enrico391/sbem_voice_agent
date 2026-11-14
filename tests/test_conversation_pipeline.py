"""Exercise native tool dispatch with scripted completions and no provider calls."""

import asyncio
import base64
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from openai.types.chat import ChatCompletionChunk
from PIL import Image
from pipecat.frames.frames import EndFrame, LLMFullResponseEndFrame, LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.openrouter.llm import OpenRouterLLMService
from pipecat.workers.runner import WorkerRunner

from sbem_voice.robot.config import RobotConfig
from sbem_voice.robot.tools import RobotTools, robot_functions
from sbem_voice.wakeword.processors import ResponseCompletionProcessor, SessionActivityObserver
from sbem_voice.wakeword.session import ConversationSession
from sbem_voice.wakeword.tools import end_conversation


class ConversationPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_camera_tool_delivers_actual_image_to_next_llm_completion(self):
        camera_tool = next(fn for fn in robot_functions if fn.__name__ == "get_camera_image")
        captured = asyncio.Event()
        invocations = []
        bridge = SimpleNamespace(
            camera_snapshot=AsyncMock(
                return_value={
                    "width": 2,
                    "height": 1,
                    "step": 6,
                    "encoding": "rgb8",
                    "is_bigendian": 0,
                    "data": base64.b64encode(b"\xff\x00\x00" * 2).decode(),
                }
            )
        )
        robot = RobotTools(RobotConfig.load(Path("config/robot.example.json")), bridge)

        class ScriptedLLM(OpenRouterLLMService):
            async def get_chat_completions(self, context):
                invocation = await self.get_llm_adapter().get_llm_invocation_params(
                    context, convert_developer_to_user=True
                )
                invocations.append(invocation)
                if len(invocations) == 1:
                    delta = {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "camera-1",
                                "type": "function",
                                "function": {"name": "get_camera_image", "arguments": "{}"},
                            }
                        ]
                    }
                else:
                    delta = {"content": "The image is red."}
                    captured.set()

                async def chunks():
                    yield ChatCompletionChunk(
                        id="offline",
                        created=0,
                        model="offline",
                        object="chat.completion.chunk",
                        choices=[{"index": 0, "delta": delta}],
                    )

                return chunks()

        context = LLMContext(
            messages=[{"role": "user", "content": "What can you see?"}], tools=[camera_tool]
        )
        user, assistant = LLMContextAggregatorPair(context)
        llm = ScriptedLLM(api_key="offline-test")
        worker = PipelineWorker(
            Pipeline([user, llm, assistant]),
            app_resources=SimpleNamespace(robot=robot),
            enable_rtvi=False,
            idle_timeout_secs=None,
        )
        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await runner.add_workers(worker)

        @worker.event_handler("on_pipeline_started")
        async def ready(worker, frame):
            await worker.queue_frames([LLMRunFrame()])

        run = asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(captured.wait(), 5)
            self.assertEqual(len(invocations), 2)
            messages = invocations[-1]["messages"]
            images = [
                part
                for message in messages
                if isinstance(message.get("content"), list)
                for part in message["content"]
                if part.get("type") == "image_url"
            ]
            self.assertEqual(len(images), 1)
            url = images[0]["image_url"]["url"]
            self.assertTrue(url.startswith("data:image/jpeg;base64,"))
            with Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))) as image:
                self.assertEqual(image.size, (2, 1))
            tool_result = next(message for message in messages if message["role"] == "tool")
            self.assertIn("captured", tool_result["content"])
            self.assertNotIn("base64", tool_result["content"])
            bridge.camera_snapshot.assert_awaited_once()
            await worker.queue_frames([EndFrame()])
            await asyncio.wait_for(run, 3)
        finally:
            if not run.done():
                await runner.cancel()
                await run
            await llm._client.close()

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
