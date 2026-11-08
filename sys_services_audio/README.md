# Persistent microphone routing and echo cancellation

This folder automates the working setup documented in [docs/audio.md](../docs/audio.md):
Xbox Kinect capture → WebRTC echo cancellation → `pipecat_aec_mic`, with playback
through `pipecat_aec_speaker` → Logitech G430.

The systemd **user service** runs as the desktop user who owns PulseAudio. It
loads `module-echo-cancel` with mono 16 kHz audio, automatic gain control disabled,
and the extended filter enabled. Physical and virtual capture gain are set to
100% (0 dB). It checks every five seconds and recreates missing AEC devices after
a PulseAudio restart or after the configured hardware becomes available.

## Install and start

Run from the project root, in the Jetson user's session where `pactl info` works:

```bash
./sys_services_audio/install.sh
systemctl --user status sbem-audio.service
journalctl --user -u sbem-audio.service -n 30
```

Use your ordinary desktop account, without sudo. The host needs PulseAudio,
`pactl` (`pulseaudio-utils`), the WebRTC echo cancellation backend, and system
Python 3.10 or newer. The service has no Python package dependencies and does not
need the application's uv environment to keep audio configured.

Installation copies:

- `audio_service.py` to `~/.local/lib/sbem-audio/`.
- `audio.json` and generated `audio-routing.env` to `~/.config/sbem-audio/`.
- `sbem-audio.service` to `~/.config/systemd/user/`.

Repeated installation updates code and the unit but preserves your installed
`audio.json`. It briefly stops an already running instance before updating it.
The service is enabled for future user sessions. To start the user manager at
boot before login on a headless robot, additionally run on the host:

```bash
sudo loginctl enable-linger "$USER"
```

The user must still have access to the sound devices. A service shown as
`active` may be waiting for PulseAudio or USB devices; look for **AEC ready** in
the journal and verify the virtual endpoints before starting the bot:

```bash
pactl list short sources
pactl list short sinks
```

No live installation or PulseAudio activation was performed in the coding
session: its sandbox could not connect to the host audio server or user manager.

## Run the voice interface through the patch

After the service reports ready and the root application's model/dependencies
are configured:

```bash
./sys_services_audio/run_with_audio.sh
```

The launcher checks that both virtual endpoints exist, exports `PULSE_SOURCE`
and `PULSE_SINK`, finds the current duplex PyAudio device named `pulse`, and
starts `uv run bot.py` with that device and `--allow-interruptions`. Its settings
come from the installed config, falling back to this folder's config if absent.
This does not change desktop default devices or reroute unrelated applications.
A `pulse` ALSA plugin may require `libasound2-plugins`.

Extra bot arguments are accepted, for example:

```bash
./sys_services_audio/run_with_audio.sh --output-sample-rate 44100
```

For another systemd user service that starts the bot, add:

```ini
[Unit]
Wants=sbem-audio.service
After=sbem-audio.service

[Service]
EnvironmentFile=%h/.config/sbem-audio/audio-routing.env
```

Ordering alone does not guarantee hardware readiness. Keep an endpoint readiness
check in the bot launcher, and use the PyAudio `pulse` device so the environment
routing takes effect.

## Change audio hardware

List the actual stable PulseAudio names:

```bash
pactl list short sources
pactl list short sinks
```

Stop the service, edit `~/.config/sbem-audio/audio.json`, then reinstall to
regenerate the routing environment and restart:

```bash
systemctl --user stop sbem-audio.service
${EDITOR:-nano} ~/.config/sbem-audio/audio.json
./sys_services_audio/install.sh
```

`source_master` and `sink_master` select the physical devices; `source_name` and
`sink_name` name the virtual devices. `poll_secs` controls retries. The repository
config contains the Kinect/Logitech names from the working setup. Edit it before
the first install if your device names differ.

A matching pre-existing echo cancellation module is reused and left loaded when
the service stops. A conflicting module or device using either virtual name is
reported and left untouched. Inspect `pactl list short modules` and unload only
the specific old SBEM/Pipecat module if you intentionally want to replace it.
Other echo cancellation modules are never removed.

The controller unloads a module it created when stopping, checking its current
ID and configuration first. Already running bot sessions should be stopped
before stopping/reconfiguring the audio service.

## Stop or remove

```bash
systemctl --user disable --now sbem-audio.service
rm ~/.config/systemd/user/sbem-audio.service
systemctl --user daemon-reload
```

This leaves the installed configuration and controller files for reuse. If you
no longer need them, remove `~/.config/sbem-audio` and
`~/.local/lib/sbem-audio`. The service does not alter PulseAudio's global config.

## Checks

```bash
uv run python tests/run.py
bash -n sys_services_audio/install.sh sys_services_audio/run_with_audio.sh
systemd-analyze --user verify sys_services_audio/sbem-audio.service
```

Offline tests cover module setup, gain, idempotence, server restart, unavailable
devices, conflicting configuration, and shutdown ownership. Real acoustic echo
cancellation and interruption quality still require physical testing using the
procedure in [docs/audio.md](../docs/audio.md).
