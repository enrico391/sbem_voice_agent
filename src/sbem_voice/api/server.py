"""Host native Pipecat WebSocket sessions alongside local voice or by themselves."""

import asyncio
import hmac
from contextlib import asynccontextmanager, contextmanager

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from loguru import logger
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.workers.runner import WorkerRunner

from sbem_voice.api.serializer import RTVIJSONSerializer
from sbem_voice.api.session import create_api_worker
from sbem_voice.robot.tools import connect_robot


class APIServer(uvicorn.Server):
    """Let the voice runner own process signals when both interfaces are active."""

    @contextmanager
    def capture_signals(self):
        yield


def create_app(settings, robot):
    """Bind the shared robot once; each accepted WebSocket creates a new context."""
    runners = set()

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            await asyncio.gather(*(runner.cancel() for runner in list(runners)))

    app = FastAPI(lifespan=lifespan)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        if settings.api_token and not hmac.compare_digest(
            websocket.headers.get("authorization", "").encode(),
            f"Bearer {settings.api_token}".encode(),
        ):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)
        runners.add(runner)
        try:
            transport = FastAPIWebsocketTransport(
                websocket,
                FastAPIWebsocketParams(
                    audio_in_enabled=False,
                    audio_out_enabled=False,
                    serializer=RTVIJSONSerializer(),
                ),
            )
            worker = await create_api_worker(transport, settings, robot)

            @transport.event_handler("on_client_disconnected")
            async def on_disconnected(transport, websocket):
                await runner.cancel()

            await runner.add_workers(worker)
            await runner.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("API conversation failed")
        finally:
            await runner.cancel()
            runners.discard(runner)
            if websocket.application_state.name != "DISCONNECTED":
                try:
                    await websocket.close()
                except WebSocketDisconnect:
                    pass

    return app


async def serve_api(settings, robot, *, voice_runner=None):
    """Supervise both interfaces; a failed listener must not fail silently."""
    config = uvicorn.Config(
        create_app(settings, robot),
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
        ws_max_size=65536,
        timeout_graceful_shutdown=5,
    )
    server = APIServer(config) if voice_runner else uvicorn.Server(config)

    async def serve():
        try:
            await server.serve()
        except SystemExit as exc:
            raise RuntimeError(
                f"Could not start API server on {settings.api_host}:{settings.api_port}"
            ) from exc

    if voice_runner is None:
        await serve()
        return
    api_task = asyncio.create_task(serve(), name="api-server")
    voice_task = asyncio.create_task(voice_runner.run(), name="local-voice")
    try:
        done, _ = await asyncio.wait((api_task, voice_task), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        server.should_exit = True
        await voice_runner.cancel()
        try:
            await asyncio.wait_for(asyncio.gather(api_task, voice_task, return_exceptions=True), 10)
        finally:
            for task in (api_task, voice_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(api_task, voice_task, return_exceptions=True)


async def run_api_only(settings):
    """Run without importing local audio, STT, TTS or wake-word inference."""
    settings.validate_runtime(audio=False)
    async with connect_robot(settings.robot_config) as robot:
        await serve_api(settings, robot)
