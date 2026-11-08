"""Run with uv run bot.py from the project root."""

from sbem_voice.cli import main


async def bot(runner_args):
    """Preserve Pipecat's discoverable async entry point."""
    from pathlib import Path

    from dotenv import load_dotenv

    from sbem_voice.config import Settings
    from sbem_voice.pipeline import bot as run

    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env", override=False)
    settings = Settings.from_env(root)
    settings.validate_runtime()
    await run(runner_args, settings)


if __name__ == "__main__":
    main()
