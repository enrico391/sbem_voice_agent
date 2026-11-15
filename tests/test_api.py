"""Exercise JSON boundaries and real Pipecat RTVI sessions without provider calls."""

import asyncio
import json
import os
import socket
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from openai.types.chat import ChatCompletionChunk
from pipecat.frames.frames import InputTransportMessageFrame, OutputTransportMessageUrgentFrame
from pipecat.services.openrouter.llm import OpenRouterLLMService
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from sbem_voice.config import Settings


class SerializerTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_errors_and_unsupported_messages_return_rtvi_errors(self):
        from sbem_voice.api.serializer import RTVIJSONSerializer

        serializer = RTVIJSONSerializer()
        for raw in ["{", "[]", "null", '"text"', b"binary", '{"type":"send-text"}']:
            with self.subTest(raw=raw):
                frame = await serializer.deserialize(raw)
                self.assertIsInstance(frame, OutputTransportMessageUrgentFrame)
                self.assertEqual(frame.message["type"], "error-response")
        raw = json.dumps({"label": "rtvi-ai", "type": "eval-context", "id": "bad"})
        frame = await serializer.deserialize(raw)
        self.assertEqual(frame.message["id"], "bad")
        self.assertIn("Unsupported", frame.message["data"]["error"])

    async def test_text_defaults_to_silent_and_roundtrips_rtvi_messages(self):
        from sbem_voice.api.serializer import RTVIJSONSerializer

        serializer = RTVIJSONSerializer()
        frame = await serializer.deserialize(
            json.dumps(
                {
                    "label": "rtvi-ai",
                    "type": "send-text",
                    "id": "hello",
                    "data": {"content": "Hello"},
                }
            )
        )
        self.assertIsInstance(frame, InputTransportMessageFrame)
        self.assertFalse(frame.message["data"]["options"]["audio_response"])
        message = {"label": "rtvi-ai", "type": "llm-raw-text", "data": {"text": "Hello"}}
        raw = await serializer.serialize(OutputTransportMessageUrgentFrame(message=message))
        self.assertEqual(json.loads(raw), message)

    async def test_partial_options_keep_text_only_default_and_reject_invalid_content(self):
        from sbem_voice.api.serializer import RTVIJSONSerializer

        serializer = RTVIJSONSerializer()
        envelope = {"label": "rtvi-ai", "type": "send-text", "id": "options"}
        frame = await serializer.deserialize(
            json.dumps(
                {
                    **envelope,
                    "data": {"content": "Append only", "options": {"run_immediately": False}},
                }
            )
        )
        self.assertIsInstance(frame, InputTransportMessageFrame)
        self.assertFalse(frame.message["data"]["options"]["audio_response"])
        self.assertFalse(frame.message["data"]["options"]["run_immediately"])
        for data in [
            {"content": " "},
            {"content": "x" * 16001},
            {"content": 123},
            {"content": "Hello", "options": {"audio_response": True}},
        ]:
            with self.subTest(data_type=type(data["content"])):
                frame = await serializer.deserialize(json.dumps({**envelope, "data": data}))
                self.assertEqual(frame.message["type"], "error-response")
                self.assertEqual(frame.message["id"], "options")

    async def test_excessively_nested_json_returns_error_instead_of_escaping_parser(self):
        from sbem_voice.api.serializer import RTVIJSONSerializer

        frame = await RTVIJSONSerializer().deserialize("[" * 2000 + "0" + "]" * 2000)
        self.assertIsInstance(frame, OutputTransportMessageUrgentFrame)
        self.assertEqual(frame.message["type"], "error-response")


class ScriptedLLM(OpenRouterLLMService):
    """Replace only provider I/O; use the real adapters and tool dispatch."""

    async def get_chat_completions(self, context):
        messages = context.get_messages()
        user_messages = [m for m in messages if m.get("role") == "user"]
        latest = user_messages[-1]["content"]
        if latest in ("remember a test fact", "dock"):
            has_result = any(m.get("role") == "tool" for m in messages)
            if not has_result:
                deltas = [
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "memory-1",
                                "type": "function",
                                "function": {
                                    "name": "dock" if latest == "dock" else "remember_memory",
                                    "arguments": "{}"
                                    if latest == "dock"
                                    else '{"content":"API test fact"}',
                                },
                            }
                        ]
                    }
                ]
            else:
                if latest == "dock":
                    result = next(
                        m["content"] for m in reversed(messages) if m.get("role") == "tool"
                    )
                    deltas = [{"content": str(result)}]
                else:
                    deltas = [{"content": "Saved "}, {"content": "the fact."}]
        elif latest == "history":
            deltas = [{"content": " | ".join(m["content"] for m in user_messages[:-1]) or "empty"}]
        else:
            deltas = [{"content": "Reply: "}, {"content": latest}]

        async def chunks():
            for delta in deltas:
                yield ChatCompletionChunk(
                    id="offline",
                    created=0,
                    model="offline",
                    object="chat.completion.chunk",
                    choices=[{"index": 0, "delta": delta, "finish_reason": None}],
                )
                await asyncio.sleep(0)

        return chunks()


class APIIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "prompt.md").write_text("Answer briefly. Use tools when requested.")
        with patch.dict(
            "os.environ",
            {
                "SYSTEM_PROMPT_FILE": "prompt.md",
                "OPENROUTER_API_KEY": "offline-test",
            },
            clear=True,
        ):
            self.settings = Settings.from_env(root)

    @asynccontextmanager
    async def server(self, *, settings=None, robot=None):
        from sbem_voice.api.server import APIServer, create_app

        settings = settings or self.settings
        app = create_app(settings, robot)
        import uvicorn

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = APIServer(uvicorn.Config(app, log_level="warning", lifespan="on"))
        llms = []

        def make_llm(settings):
            llm = ScriptedLLM(api_key="offline-test")
            llms.append(llm)
            return llm

        with patch("sbem_voice.api.session.create_llm", side_effect=make_llm):
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if task.done():
                            await task
                            raise AssertionError("Server exited before startup")
                        await asyncio.sleep(0.01)
                yield f"ws://127.0.0.1:{port}/ws", app, llms
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, 5)
                listener.close()

    async def ready(self, ws):
        await ws.send(
            json.dumps(
                {
                    "label": "rtvi-ai",
                    "type": "client-ready",
                    "id": "ready",
                    "data": {"version": "2.2.0"},
                }
            )
        )
        async with asyncio.timeout(5):
            while True:
                message = json.loads(await ws.recv())
                if message["type"] == "bot-ready":
                    return

    async def ask(self, ws, text, *, expected=None):
        await ws.send(
            json.dumps(
                {
                    "label": "rtvi-ai",
                    "type": "send-text",
                    "id": "request",
                    "data": {"content": text},
                }
            )
        )
        events, parts = [], []
        async with asyncio.timeout(5):
            while True:
                message = json.loads(await ws.recv())
                events.append(message)
                if message["type"] == "llm-raw-text":
                    parts.append(message["data"]["text"])
                if message["type"] == "bot-llm-stopped" and parts:
                    if expected is None or expected in "".join(parts):
                        return "".join(parts), events

    async def test_streams_chunks_retains_own_history_and_isolates_other_connections(self):
        async with self.server() as (url, app, llms):
            async with connect(url) as first, connect(url) as second:
                await self.ready(first)
                await self.ready(second)
                text, events = await self.ask(first, "private first conversation")
                self.assertEqual(text, "Reply: private first conversation")
                self.assertEqual(sum(e["type"] == "llm-raw-text" for e in events), 2)
                text, _ = await self.ask(second, "history")
                self.assertEqual(text, "empty")
                text, _ = await self.ask(first, "history")
                self.assertEqual(text, "private first conversation")
                self.assertEqual(len(llms), 2)
            async with connect(url) as reconnected:
                await self.ready(reconnected)
                text, _ = await self.ask(reconnected, "history")
                self.assertEqual(text, "empty")

    async def test_text_sessions_never_construct_a_speech_turn_model(self):
        with patch(
            "pipecat.audio.turn.smart_turn.local_smart_turn_v3.LocalSmartTurnAnalyzerV3",
            side_effect=AssertionError("Text API attempted to load a speech model"),
        ):
            async with self.server() as (url, _, _):
                async with connect(url) as ws:
                    await self.ready(ws)
                    text, _ = await self.ask(ws, "text without audio models")
                    self.assertEqual(text, "Reply: text without audio models")

    async def test_native_memory_tool_runs_and_streams_its_followup(self):
        from sbem_voice.memory.store import MemoryStore

        async with self.server() as (url, app, llms):
            async with connect(url) as ws:
                await self.ready(ws)
                text, events = await self.ask(
                    ws, "remember a test fact", expected="Saved the fact."
                )
                self.assertIn("Saved the fact.", text)
                self.assertTrue(any(e["type"] == "llm-function-call-in-progress" for e in events))
        store = MemoryStore(self.settings.memory_db_path, self.settings.memory_user_id)
        self.assertEqual(store.retrieve()["memories"][0]["content"], "API test fact")

    async def test_bad_message_does_not_break_session_or_other_client(self):
        async with self.server() as (url, app, llms):
            async with connect(url) as first, connect(url) as second:
                await self.ready(first)
                await self.ready(second)
                for raw in ["{", "[" * 2000 + "0" + "]" * 2000]:
                    await first.send(raw)
                    async with asyncio.timeout(3):
                        while True:
                            error = json.loads(await first.recv())
                            if error["type"] == "error-response":
                                break
                    self.assertEqual(error["type"], "error-response")
                text, _ = await self.ask(first, "still working")
                self.assertEqual(text, "Reply: still working")
                await first.close()
                text, _ = await self.ask(second, "survived disconnect")
                self.assertEqual(text, "Reply: survived disconnect")

    async def test_optional_bearer_token_is_checked_before_creating_a_session(self):
        async with self.server(settings=replace(self.settings, api_token="secret")) as (
            url,
            _,
            llms,
        ):
            for headers in [None, {"Authorization": "Bearer wrong"}]:
                with self.assertRaises(InvalidStatus):
                    async with connect(url, additional_headers=headers):
                        self.fail("Unauthorized connection accepted")
            self.assertEqual(len(llms), 0)
            async with connect(url, additional_headers={"Authorization": "Bearer secret"}) as ws:
                await self.ready(ws)
                text, _ = await self.ask(ws, "authorized")
                self.assertEqual(text, "Reply: authorized")

    async def test_api_motion_obeys_the_lock_already_held_by_voice(self):
        from sbem_voice.robot.config import RobotConfig
        from sbem_voice.robot.tools import RobotTools

        robot = RobotTools(RobotConfig.load(Path("config/robot.example.json")), bridge=None)
        async with robot.motion_lock:
            async with self.server(robot=robot) as (url, _, _):
                async with connect(url) as ws:
                    await self.ready(ws)
                    text, _ = await self.ask(ws, "dock", expected="Another motion is in progress")
                    self.assertIn("Another motion is in progress", text)

    async def test_api_only_cli_starts_without_wakeword_or_speech_credentials(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        env = {
            **os.environ,
            "OPENROUTER_API_KEY": "offline-test",
            "DEEPGRAM_API_KEY": "",
            "CARTESIA_API_KEY": "",
            "WAKEWORD_MODEL_PATH": "",
            "ROBOT_CONFIG": "",
            "SYSTEM_PROMPT_FILE": str(self.settings.system_prompt_file),
            "MEMORY_DB_PATH": str(self.settings.memory_db_path),
            "API_TOKEN": "",
            "API_HOST": "127.0.0.1",
            "API_ENABLED": "false",
        }
        log_path = Path(self.tmp.name) / "api-process.log"
        with log_path.open("w") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "bot.py",
                "--api-only",
                "--api-port",
                str(port),
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                async with asyncio.timeout(12):
                    while True:
                        if process.returncode is not None:
                            self.fail(log_path.read_text())
                        try:
                            ws = await connect(f"ws://127.0.0.1:{port}/ws")
                            break
                        except OSError:
                            await asyncio.sleep(0.05)
                try:
                    await self.ready(ws)
                    await ws.send(
                        json.dumps(
                            {
                                "label": "rtvi-ai",
                                "type": "disconnect-bot",
                                "id": "bye",
                            }
                        )
                    )
                    await asyncio.wait_for(ws.wait_closed(), 3)
                    self.assertEqual(ws.close_code, 1000)
                finally:
                    await ws.close()
            finally:
                if process.returncode is None:
                    process.terminate()
                await asyncio.wait_for(process.wait(), 8)
        self.assertNotIn("Traceback", log_path.read_text())


class APIHostingTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_shutdown_closes_active_api_sessions_and_listener(self):
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineWorker
        from pipecat.processors.frame_processor import FrameProcessor
        from pipecat.workers.runner import WorkerRunner

        from sbem_voice.api.server import serve_api

        class VoiceLoop(FrameProcessor):
            async def process_frame(self, frame, direction):
                await super().process_frame(frame, direction)
                await self.push_frame(frame, direction)

        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "offline-test"}, clear=True):
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            settings = replace(
                Settings.from_env(Path.cwd()),
                api_port=port,
                memory_db_path=Path(tmp.name) / "memory.sqlite3",
            )
        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        await runner.add_workers(
            PipelineWorker(
                Pipeline([VoiceLoop()]),
                enable_rtvi=False,
                idle_timeout_secs=None,
            )
        )
        serving = asyncio.create_task(serve_api(settings, None, voice_runner=runner))
        try:
            async with asyncio.timeout(5):
                while True:
                    try:
                        ws = await connect(f"ws://127.0.0.1:{port}/ws")
                        break
                    except OSError:
                        if serving.done():
                            await serving
                        await asyncio.sleep(0.01)
            try:
                await ws.send(
                    json.dumps(
                        {
                            "label": "rtvi-ai",
                            "type": "client-ready",
                            "id": "ready",
                            "data": {"version": "2.2.0"},
                        }
                    )
                )
                async with asyncio.timeout(5):
                    while json.loads(await ws.recv())["type"] != "bot-ready":
                        pass
                await runner.cancel()
                await asyncio.wait_for(serving, 8)
                await asyncio.wait_for(ws.wait_closed(), 1)
                self.assertEqual(ws.close_code, 1012)
                with self.assertRaises(OSError):
                    async with connect(f"ws://127.0.0.1:{port}/ws"):
                        self.fail("API listener survived voice shutdown")
            finally:
                await ws.close()
        finally:
            await runner.cancel()
            if not serving.done():
                await asyncio.wait_for(serving, 8)

    async def test_listener_failure_stops_voice_and_reports_bind_error(self):
        from sbem_voice.api.server import serve_api

        class VoiceRunner:
            def __init__(self):
                self.stopped = asyncio.Event()

            async def run(self):
                await self.stopped.wait()

            async def cancel(self):
                self.stopped.set()

        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            with patch.dict("os.environ", {}, clear=True):
                settings = Settings.from_env(Path("/tmp"))
            voice = VoiceRunner()
            with self.assertRaisesRegex(RuntimeError, "Could not start API server"):
                await asyncio.wait_for(
                    serve_api(
                        replace(settings, api_port=occupied.getsockname()[1]),
                        None,
                        voice_runner=voice,
                    ),
                    5,
                )
            self.assertTrue(voice.stopped.is_set())
