"""Exercise AEC lifecycle without touching a user's PulseAudio server."""

import importlib.util
import sys
import unittest
from pathlib import Path

module_path = Path(__file__).resolve().parents[1] / "sys_services_audio/audio_service.py"
spec = importlib.util.spec_from_file_location("sbem_audio_service", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class FakePulse:
    def __init__(self):
        self.modules = []
        self.sources = {"mic"}
        self.sinks = {"speaker"}
        self.calls = []
        self.counter = 20
        self.cookie = "server-one"

    def __call__(self, *args):
        self.calls.append(args)
        if args == ("info",):
            return f"Server Name: pulseaudio\nCookie: {self.cookie}"
        if args == ("list", "short", "sources"):
            return "\n".join(f"{i}\t{name}\tdriver" for i, name in enumerate(self.sources))
        if args == ("list", "short", "sinks"):
            return "\n".join(f"{i}\t{name}\tdriver" for i, name in enumerate(self.sinks))
        if args == ("list", "short", "modules"):
            return "\n".join(f"{i}\t{name}\t{params}\t0" for i, name, params in self.modules)
        if args[0] == "load-module":
            self.counter += 1
            self.modules.append((str(self.counter), args[1], " ".join(args[2:])))
            self.sources.add("pipecat_aec_mic")
            self.sinks.add("pipecat_aec_speaker")
            return str(self.counter)
        if args[0] == "unload-module":
            self.modules = [m for m in self.modules if m[0] != args[1]]
            self.sources.discard("pipecat_aec_mic")
            self.sinks.discard("pipecat_aec_speaker")
            return ""
        if args[0] == "set-source-volume":
            return ""
        raise AssertionError(args)


class AudioServiceTests(unittest.TestCase):
    def setUp(self):
        self.config = module.Config(source_master="mic", sink_master="speaker")
        self.pulse = FakePulse()
        self.controller = module.AudioController(self.config, self.pulse)

    def test_initial_setup_creates_aec_once_and_applies_working_gain(self):
        self.controller.ensure()
        self.controller.ensure()
        loads = [c for c in self.pulse.calls if c[0] == "load-module"]
        self.assertEqual(len(loads), 1)
        self.assertIn("source_master=mic", loads[0])
        self.assertIn(
            'aec_args="analog_gain_control=0 digital_gain_control=0 extended_filter=1"', loads[0]
        )
        self.assertIn(("set-source-volume", "mic", "100%"), self.pulse.calls)
        self.assertIn(("set-source-volume", "pipecat_aec_mic", "100%"), self.pulse.calls)

    def test_audio_server_restart_recreates_missing_module(self):
        self.controller.ensure()
        self.pulse.modules.clear()
        self.pulse.sources = {"mic"}
        self.pulse.sinks = {"speaker"}
        self.controller.ensure()
        self.assertEqual(len([c for c in self.pulse.calls if c[0] == "load-module"]), 2)

    def test_reuses_matching_module_without_owning_or_unloading_it(self):
        self.pulse.modules.append(("8", "module-echo-cancel", " ".join(self.config.module_args())))
        self.pulse.sources.add("pipecat_aec_mic")
        self.pulse.sinks.add("pipecat_aec_speaker")
        self.controller.ensure()
        self.controller.close()
        self.assertFalse(any(c[0] in ("load-module", "unload-module") for c in self.pulse.calls))

    def test_conflicting_module_is_never_unloaded(self):
        self.pulse.modules.append(
            ("8", "module-echo-cancel", "source_name=pipecat_aec_mic source_master=wrong")
        )
        with self.assertRaisesRegex(RuntimeError, "conflict"):
            self.controller.ensure()
        self.controller.close()
        self.assertFalse(any(c[0] in ("load-module", "unload-module") for c in self.pulse.calls))

    def test_stop_unloads_owned_module_but_not_reused_id_after_restart(self):
        self.controller.ensure()
        self.controller.close()
        self.assertIn(("unload-module", "21"), self.pulse.calls)
        self.controller.ensure()
        self.pulse.modules = [("22", "module-null-sink", "sink_name=other")]
        self.controller.close()
        self.assertNotIn(("unload-module", "22"), self.pulse.calls)

    def test_server_restart_with_identical_id_and_config_relinquishes_ownership(self):
        self.controller.ensure()
        self.pulse.cookie = "server-two"
        # A different owner independently restored the exact config as ID 21.
        self.pulse.calls.clear()
        self.controller.ensure()
        self.controller.close()
        self.assertFalse(any(c[0] == "unload-module" for c in self.pulse.calls))
        self.assertIn(("set-source-volume", "mic", "100%"), self.pulse.calls)

    def test_stop_after_unobserved_server_restart_does_not_unload_reused_id(self):
        self.controller.ensure()
        self.pulse.cookie = "server-two"
        self.pulse.calls.clear()
        self.controller.close()
        self.assertFalse(any(c[0] == "unload-module" for c in self.pulse.calls))

    def test_unplugged_devices_are_waited_for_without_creating_module(self):
        self.pulse.sources.clear()
        with self.assertRaisesRegex(RuntimeError, "mic"):
            self.controller.ensure()
        self.assertFalse(any(c[0] == "load-module" for c in self.pulse.calls))
