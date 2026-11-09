"""End conversation requests preserve the worker and defer sleep until output drains."""

import unittest
from types import SimpleNamespace

from pipecat.frames.frames import LLMFullResponseStartFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager

from sbem_voice.wakeword.session import ConversationSession
from sbem_voice.wakeword.tools import end_conversation


class ConversationToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_tool_reports_request_then_sleeps_after_final_response(self):
        session = ConversationSession(30)
        tasks = TaskManager()
        await session.setup(
            FrameProcessorSetup(clock=None, task_manager=tasks, pipeline_worker=None)
        )
        results = []

        async def callback(result):
            results.append(result)

        try:
            await session.wake()
            params = SimpleNamespace(
                app_resources=SimpleNamespace(session=session), result_callback=callback
            )
            await end_conversation(params)
            self.assertEqual(results, [{"status": "ending"}])
            self.assertTrue(session.awake)
            await session.process_frame(LLMFullResponseStartFrame())
            await session.response_finished()
            self.assertFalse(session.awake)
            await end_conversation(params)
            self.assertEqual(results[-1], {"status": "already_asleep"})
            await session.wake()
            self.assertTrue(session.awake)
            self.assertFalse(session.closing)
        finally:
            await session.cleanup()
        self.assertEqual(list(tasks.current_tasks()), [])

    async def test_end_tool_schema_has_no_model_selected_session(self):
        schema = LLMContext(tools=[end_conversation]).tools.standard_tools[0].to_default_dict()
        self.assertEqual(schema["name"], "end_conversation")
        self.assertEqual(schema["parameters"]["properties"], {})
