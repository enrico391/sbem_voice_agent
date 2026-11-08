#!/usr/bin/env python3
"""Keep the documented PulseAudio AEC patch available in the desktop session."""

import argparse
import json
import logging
import math
import os
import re
import shlex
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger("sbem-audio")


@dataclass(frozen=True)
class Config:
    source_master: str = (
        "alsa_input.usb-Microsoft_Kinect_USB_Audio_A70776X01719234A-01.input-4-channels"
    )
    sink_master: str = "alsa_output.usb-Logitech_Logitech_G430_Gaming_Headset-00.analog-stereo"
    source_name: str = "pipecat_aec_mic"
    sink_name: str = "pipecat_aec_speaker"
    poll_secs: float = 5.0

    def __post_init__(self):
        names = (self.source_master, self.sink_master, self.source_name, self.sink_name)
        if any(not isinstance(n, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", n) for n in names):
            raise ValueError("Audio device names must contain only letters, numbers, _, . and -")
        if self.source_name == self.source_master or self.sink_name == self.sink_master:
            raise ValueError("Virtual audio names must differ from physical device names")
        if (
            isinstance(self.poll_secs, bool)
            or not math.isfinite(self.poll_secs)
            or self.poll_secs <= 0
        ):
            raise ValueError("poll_secs must be a positive finite number")

    def module_args(self):
        return [
            f"source_master={self.source_master}",
            f"sink_master={self.sink_master}",
            f"source_name={self.source_name}",
            f"sink_name={self.sink_name}",
            "aec_method=webrtc",
            "rate=16000",
            "channels=1",
            "channel_map=mono",
            'aec_args="analog_gain_control=0 digital_gain_control=0 extended_filter=1"',
        ]


def pactl(*args):
    try:
        result = subprocess.run(
            ["/usr/bin/pactl", *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
            env={**os.environ, "LC_ALL": "C"},
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"pactl {args[0]} failed: {exc.stderr.strip()}") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"pactl {args[0]} unavailable or timed out") from exc
    return result.stdout.strip()


def _arguments(text):
    return dict(token.split("=", 1) for token in shlex.split(text) if "=" in token)


class AudioController:
    def __init__(self, config, command=pactl):
        self.config = config
        self.command = command
        self._owned_id = None
        self._configured_id = None
        self._server_cookie = None

    def _check_server(self):
        # pa_server_info.cookie identifies the server instance, not the auth cookie.
        info = self.command("info")
        cookie = next(
            (
                line.partition(":")[2].strip()
                for line in info.splitlines()
                if line.partition(":")[0].strip() == "Cookie"
            ),
            None,
        )
        if not cookie:
            raise RuntimeError("Cannot identify the PulseAudio server instance")
        if cookie != self._server_cookie:
            self._owned_id = None
            self._configured_id = None
            self._server_cookie = cookie

    def _modules(self):
        result = []
        for line in self.command("list", "short", "modules").splitlines():
            fields = line.split("\t")
            if len(fields) >= 3:
                result.append((fields[0], fields[1], _arguments(fields[2])))
        return result

    def _matches(self, name, args):
        expected = _arguments(" ".join(self.config.module_args()))
        return name == "module-echo-cancel" and all(args.get(k) == v for k, v in expected.items())

    def ensure(self):
        self._check_server()
        config = self.config
        sources = {
            line.split("\t")[1]
            for line in self.command("list", "short", "sources").splitlines()
            if "\t" in line
        }
        sinks = {
            line.split("\t")[1]
            for line in self.command("list", "short", "sinks").splitlines()
            if "\t" in line
        }
        if config.source_master not in sources:
            raise RuntimeError(f"Waiting for microphone {config.source_master}")
        if config.sink_master not in sinks:
            raise RuntimeError(f"Waiting for speaker {config.sink_master}")
        matches = [
            m
            for m in self._modules()
            if m[2].get("source_name") == config.source_name
            or m[2].get("sink_name") == config.sink_name
        ]
        if matches:
            if len(matches) != 1 or not self._matches(matches[0][1], matches[0][2]):
                raise RuntimeError("AEC configuration conflict; inspect pactl list short modules")
            module_id = matches[0][0]
        else:
            if config.source_name in sources or config.sink_name in sinks:
                raise RuntimeError(
                    "AEC device name conflict; existing devices will not be replaced"
                )
            module_id = self.command("load-module", "module-echo-cancel", *config.module_args())
            if not module_id.isdecimal():
                raise RuntimeError("PulseAudio did not return a numeric AEC module ID")
            self._owned_id = module_id
            self._configured_id = None
            LOG.info("Created echo cancellation module %s", module_id)
        if module_id != self._configured_id:
            self.command("set-source-volume", config.source_master, "100%")
            self.command("set-source-volume", config.source_name, "100%")
            self._configured_id = module_id
            LOG.info("AEC ready: microphone=%s speaker=%s", config.source_name, config.sink_name)
        return module_id

    def close(self):
        self._check_server()
        if self._owned_id is None:
            return
        owned = self._owned_id
        self._owned_id = None
        self._configured_id = None
        for module_id, name, args in self._modules():
            if module_id == owned and self._matches(name, args):
                self.command("unload-module", module_id)
                LOG.info("Removed owned echo cancellation module %s", module_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("audio.json"))
    parser.add_argument("--routing", action="store_true", help="Print PulseAudio routing variables")
    args = parser.parse_args()
    try:
        config = Config(**json.loads(args.config.read_text()))
    except (OSError, ValueError, TypeError) as exc:
        parser.error(f"Invalid audio configuration: {exc}")
    if args.routing:
        print(f"PULSE_SOURCE={config.source_name}\nPULSE_SINK={config.sink_name}")
        return
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    controller = AudioController(config)
    last_error = None
    try:
        while not stop.is_set():
            try:
                controller.ensure()
                last_error = None
            except RuntimeError as exc:
                if str(exc) != last_error:
                    LOG.warning("%s; retrying every %s seconds", exc, config.poll_secs)
                    last_error = str(exc)
            stop.wait(config.poll_secs)
    finally:
        try:
            controller.close()
        except RuntimeError as exc:
            LOG.warning("Cleanup skipped: %s", exc)


if __name__ == "__main__":
    main()
