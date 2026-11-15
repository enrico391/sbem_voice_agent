"""Run the API pipeline with Pipecat's native, development-only eval transport."""

from pathlib import Path

from dotenv import load_dotenv
from pipecat.evals.transport import EvalTransportParams
from pipecat.runner.utils import create_transport
from pipecat.workers.runner import WorkerRunner

from sbem_voice.api.session import create_api_worker
from sbem_voice.config import Settings
from sbem_voice.robot.tools import connect_robot


async def bot(runner_args):
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=False)
    settings = Settings.from_env(root)
    settings.validate_runtime(audio=False)
    transport = await create_transport(
        runner_args,
        {
            "eval": lambda: EvalTransportParams(audio_in_enabled=False, audio_out_enabled=False),
        },
    )
    async with connect_robot(settings.robot_config) as robot:
        worker = await create_api_worker(transport, settings, robot)
        runner = WorkerRunner(
            handle_sigint=runner_args.handle_sigint,
            handle_sigterm=runner_args.handle_sigterm,
        )

        @transport.event_handler("on_client_disconnected")
        async def on_disconnected(transport, websocket):
            await runner.cancel()

        await runner.add_workers(worker)
        await runner.run()


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
