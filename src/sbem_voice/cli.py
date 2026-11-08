"""Command-line entry point for the local voice interface."""

import argparse
import asyncio
import math
from pathlib import Path


def main():
    """Select local audio devices and start without a web server."""
    parser = argparse.ArgumentParser(description="Talk to Pipecat using local mic and speakers.")
    parser.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    parser.add_argument(
        "--input-device", type=int, help="Microphone PyAudio index (default: system)"
    )
    parser.add_argument("--output-device", type=int, help="Speaker PyAudio index (default: system)")
    parser.add_argument(
        "--input-sample-rate",
        type=int,
        choices=(8000, 16000),
        default=16000,
        help="Mic rate in Hz (Silero VAD supports 8000 or 16000)",
    )
    parser.add_argument(
        "--output-sample-rate", type=int, default=48000, help="Speaker rate in Hz (default: 48000)"
    )
    parser.add_argument(
        "--echo-tail-secs",
        type=float,
        default=0.8,
        help="Keep speech recognition muted after playback (default: 0.8 seconds)",
    )
    parser.add_argument(
        "--allow-interruptions",
        action="store_true",
        help="Listen during playback; use headphones or externally configured echo cancellation",
    )
    args = parser.parse_args()
    if not math.isfinite(args.echo_tail_secs) or args.echo_tail_secs < 0:
        parser.error("--echo-tail-secs must be finite and non-negative")
    if args.output_sample_rate <= 0:
        parser.error("--output-sample-rate must be positive")

    if args.list_devices:
        import pyaudio

        audio = pyaudio.PyAudio()
        try:
            if audio.get_device_count() == 0:
                print("No audio devices found. Check /dev/snd and run on the Jetson host.")
            for index in range(audio.get_device_count()):
                device = audio.get_device_info_by_index(index)
                print(
                    f"{index}: {device['name']} "
                    f"(inputs={device['maxInputChannels']}, "
                    f"outputs={device['maxOutputChannels']}, "
                    f"default rate={int(device['defaultSampleRate'])} Hz)"
                )
        finally:
            audio.terminate()
        return

    from dotenv import load_dotenv
    from pipecat.runner.types import RunnerArguments

    from sbem_voice.config import Settings

    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)
    try:
        settings = Settings.from_env(root)
        settings.validate_runtime()
    except ValueError as exc:
        parser.error(str(exc))

    from sbem_voice.pipeline import bot

    runner_args = RunnerArguments()
    runner_args.handle_sigint = True
    runner_args.handle_sigterm = True
    runner_args.cli_args = args
    asyncio.run(bot(runner_args, settings))
