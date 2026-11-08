# SBEM Voice

A wake-word activated voice assistant for the SBEM robot, built with **Pipecat**.
Talk through a local microphone, ask follow-up questions, and control the robot
using native tool calling over **rosbridge**.

The application runs on Python 3.11. ROS 2 runs separately in its own environment,
so ROS Humble's Python 3.10 bindings do not need to be imported into the voice app.

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the assistant](#running-the-assistant)
- [Robot integration](#robot-integration)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Project structure](#project-structure)

## Features

- Local wake-word detection with openWakeWord.
- Speech recognition through Deepgram, conversation through OpenRouter, and speech
  synthesis through Cartesia or Deepgram.
- Follow-up conversations with a configurable inactivity timeout.
- Six focused robot tools, with pushed progress and completion updates.
- Position storage and lookup through SBEM's waypoint manager.
- PulseAudio echo cancellation for the Kinect microphone and Logitech speaker.
- Offline regression tests and a local WebSocket integration fixture.

```text
Microphone → Wake word → Speech recognition → LLM → Speech synthesis → Speaker
                                             ↕
                                      Native Python tools
                                             ↕
                                          rosbridge
                                             ↕
                                  ROS 2 / Nav2 / Waypoint manager
```

## Requirements

- Linux with microphone and speaker access; the audio setup targets an NVIDIA Jetson.
- Python **3.11** and **uv**.
- API keys for **Deepgram** and **OpenRouter**, plus **Cartesia** when selected for TTS.
- An openWakeWord **ONNX** model and its shared feature models.
- PulseAudio with its WebRTC echo cancellation backend for the audio launcher.
- For robot control: ROS 2, rosbridge with native ROS 2 action support, Nav2,
  docking actions, and
  [sbem_waypoint_manager](https://github.com/enrico391/Sbem_workspace/tree/jetson_container/src/sbem_waypoint_manager).

The application pins Pipecat to **1.13.0**. Robot integration targets ROS 2 Humble
and has been checked against rosbridge **2.0.8**.

## Installation

Run from the repository root.

### 1. Install audio dependencies

On Ubuntu / Jetson:

```bash
sudo apt-get install portaudio19-dev python3-dev build-essential \
  alsa-utils pulseaudio pulseaudio-utils libasound2-plugins
```

### 2. Install Python dependencies

```bash
uv sync --locked
cp -n .env.example .env
```

### 3. Configure credentials and wake-word models

Edit `.env` and set:

```dotenv
DEEPGRAM_API_KEY=your_deepgram_key
OPENROUTER_API_KEY=your_openrouter_key
CARTESIA_API_KEY=your_cartesia_key
WAKEWORD_MODEL_PATH=models/sbem.onnx
```

Provide your own wake-word model; a custom SBEM model is not bundled. The app does
not download models at startup. See [models/README.md](models/README.md) for
feature-model setup and an optional pretrained wake word for testing.

## Configuration

Settings are loaded from `.env` in the project root. Exported environment
variables take precedence. Relative file paths resolve from the project root.

| Variable | Purpose | Default |
| --- | --- | --- |
| `DEEPGRAM_API_KEY` | Speech recognition and optional Deepgram synthesis credentials | Required |
| `OPENROUTER_API_KEY` | LLM credentials | Required |
| `CARTESIA_API_KEY` | Cartesia speech synthesis credentials | Required when `TTS_PROVIDER=cartesia` |
| `TTS_PROVIDER` | Speech synthesis provider: `cartesia` or `deepgram` | `cartesia` |
| `DEEPGRAM_TTS_VOICE` | Deepgram TTS voice/model identifier | Required when `TTS_PROVIDER=deepgram` |
| `WAKEWORD_MODEL_PATH` | Path to an openWakeWord `.onnx` model | Required |
| `WAKEWORD_THRESHOLD` | Detection threshold, greater than 0 and at most 1 | `0.5` |
| `CONVERSATION_IDLE_SECS` | Listening window after an exchange | `30` |
| `OPENROUTER_MODEL` | OpenRouter model identifier | `deepseek/deepseek-v4.1-flash` |
| `CARTESIA_VOICE_ID` | Cartesia voice identifier | See `src/sbem_voice/config.py` |
| `SYSTEM_PROMPT_FILE` | UTF-8 system prompt | `config/system_prompt.md` |
| `ROBOT_CONFIG` | Robot interface configuration | Unset; conversation only |

Edit [config/system_prompt.md](config/system_prompt.md) to change the assistant's
role, language, or speaking style. Keep the spoken-output and tool-result rules.
Restart the assistant after changing configuration or the prompt.

To use Deepgram for both recognition and speech synthesis, set `TTS_PROVIDER=deepgram`
and `DEEPGRAM_TTS_VOICE` to your chosen Deepgram voice/model identifier in `.env`.
Both services use `DEEPGRAM_API_KEY`; `CARTESIA_API_KEY` is then optional. Restart
the assistant to apply the change.

## Running the assistant

### With microphone routing and echo cancellation

For the Kinect / Logitech setup, install the audio service once as your desktop
user, **without sudo**:

```bash
./sys_services_audio/install.sh
```

Check that it reports **AEC ready**:

```bash
systemctl --user status sbem-audio.service
pactl get-source-volume pipecat_aec_mic
pactl get-sink-volume pipecat_aec_speaker
```

Start the assistant:

```bash
./sys_services_audio/run_with_audio.sh
```

The launcher selects the echo-cancelled microphone and speaker, finds the current
PyAudio `pulse` device, and enables voice interruptions. Device indices do not
need to be hard-coded. For other hardware, edit the physical device names in
`~/.config/sbem-audio/audio.json`. See the
[audio service guide](sys_services_audio/README.md) for setup and maintenance.

### With default audio devices

```bash
uv run bot.py
```

List devices or inspect the command-line options:

```bash
uv run bot.py --list-devices
uv run bot.py --help
```

The installed entry point, `uv run sbem-voice`, accepts the same options.
`--allow-interruptions` enables listening during playback; use it with echo
cancellation or headphones.

### Conversation behavior

The assistant starts silently. Say the wake word followed by a command. Follow-up
questions do not need the wake word until the inactivity timeout expires.

The timeout pauses during speech, response generation, and pending tools. Sleep
keeps the process running and preserves conversation history. While asleep,
microphone audio is blocked before speech recognition, although the provider
connection may remain open for keepalives.

Press **Ctrl+C** to stop.

## Robot integration

Robot capabilities use **native Pipecat tool calling over rosbridge**. MCP is not
used by the runtime.

### 1. Configure the robot interfaces

```bash
cp -n config/robot.example.json config/robot.json
```

Add this to `.env`:

```dotenv
ROBOT_CONFIG=config/robot.json
```

Edit `config/robot.json` to match your robot. The example's navigation, docking,
and battery endpoints must be checked against your deployment.

| Configuration | Purpose |
| --- | --- |
| `url` | rosbridge WebSocket URL; accepts `${ENV_VARIABLE}` references |
| `frame_id` | Required navigation pose frame |
| `actions.*.name`, `type`, `goal` | Action endpoints, types, and goal templates |
| `actions.navigate_to.pose_field` | Goal field receiving the saved `PoseStamped` |
| `actions.*.feedback_fields` | Numeric feedback fields included in progress updates |
| `waypoint_manager.services.*` | Save, list, and get service names and types |
| `waypoint_manager.map_id` | Empty for the current map; explicit ID for saved-map list/get requests |
| `battery` | Topic, message type, dotted field paths, and percentage scale |
| `connect_timeout_secs`, `service_timeout_secs`, `action_timeout_secs` | Request deadlines |
| `telemetry_timeout_secs`, `telemetry_max_age_secs` | Battery wait timeout and maximum receipt age |
| `feedback_interval_secs` | Minimum interval between progress updates; default `15` |
| `feedback_max_updates` | Maximum progress updates per action; default `3`, or `0` to disable |

Use `battery.percentage_scale: 100` for a 0–1 fraction, or `1` for a percentage
already expressed as 0–100. The full configuration is in
[config/robot.example.json](config/robot.example.json).

### 2. Start the ROS services

In a separate terminal, source ROS and your robot workspace's `install/setup.bash`
so rosbridge can load the custom waypoint interfaces. Use the same `ROS_DOMAIN_ID`
as the robot.

```bash
source /opt/ros/humble/setup.bash
# Source your robot workspace's install/setup.bash here.
ros2 launch rosbridge_server rosbridge_websocket_launch.xml \
  send_action_goals_in_new_thread:=true call_services_in_new_thread:=true
```

Run the waypoint manager alongside the robot's localization, static map, and Nav2
bringup:

```bash
ros2 launch sbem_waypoint_manager waypoint_manager.launch.py
```

Keep those processes running, then start the voice assistant in another terminal.

### Available tools

| Tool | Behavior |
| --- | --- |
| `get_positions()` | Lists saved names through `list_locations` |
| `store_position(name)` | Saves the current pose through `save_location` with `overwrite: false` |
| `navigate_to(position_name)` | Retrieves the pose through `get_location`, then sends a navigation action |
| `cancel_navigation()` | Requests cancellation of this session's current navigation; its final outcome arrives through `navigate_to` |
| `dock()` | Runs the configured docking action |
| `undock()` | Runs the configured undocking action |
| `get_battery_status()` | Returns fresh battery percentage and voltage when available |

The waypoint manager owns position persistence, TF capture, and map metadata.
Navigation preserves its fresh pose timestamp and rejects locations that do not
match the current map or configured frame. Poses and map snapshots stay out of
the LLM conversation.

### Progress and execution behavior

Action feedback and final results are pushed over the WebSocket connection;
**the LLM does not poll**. Progress updates are compact and capped by the configured
interval and count. Final results are always delivered when received. Battery
telemetry remains cached outside conversation history until requested.

Motion and save tools run in the background, so speaking does not discard their
pending results. Only one motion can run at a time. Action success is checked
against ROS status and standard `success` / `error_code` result fields; custom
result formats need an adapter.

Navigation uses the manager's `get_location` service and a direct Nav2 action.
Its `goto_location` service reports acceptance only. Manager-side goal tracking
and map-change cancellation apply to goals the manager dispatches itself;
coordinate navigation ownership with other clients to avoid preemption.

Timeout or shutdown requests cancellation of pending actions. A cancellation
request does not confirm that the robot stopped. Commands are not automatically
retried. If a motion outcome is unknown, further motion is blocked until the
robot is checked and the app restarted. `cancel_navigation` targets navigation
only; it does not cancel docking or undocking. A confirmed ROS canceled result
is reported as `canceled`; sending the request reports `cancellation_requested`.
Cancellation during the saved-position lookup prevents the navigation goal from
being sent and reports `canceled` when that lookup finishes.
A disconnected bridge invalidates telemetry; restart the app to reconnect.

## Troubleshooting

### `Failed to get source information: No such entity`

The configured virtual microphone is missing. Check the audio service and devices:

```bash
systemctl --user status sbem-audio.service
journalctl --user -u sbem-audio.service -n 30
pactl list short sources
pactl list short sinks
```

An active service can still be waiting for hardware. Confirm that the physical
names match `~/.config/sbem-audio/audio.json`.

If the Kinect is connected and its microphone profile is active but no capture
source exists, stop any running assistant and reset the Kinect profile:

```bash
pactl set-card-profile alsa_card.usb-Microsoft_Kinect_USB_Audio_A70776X01719234A-01 off
pactl set-card-profile alsa_card.usb-Microsoft_Kinect_USB_Audio_A70776X01719234A-01 input:mic-array
```

That card name is specific to the configured Kinect; use `pactl list short cards`
to identify another device. The audio service retries every five seconds. Wait
for **AEC ready**, then rerun `./sys_services_audio/run_with_audio.sh`.

### Other common issues

| Symptom | Check |
| --- | --- |
| No duplex PyAudio `pulse` device | Install `libasound2-plugins` and check PulseAudio access |
| Wake word is not detected | Check the ONNX path, feature models, selected microphone, and threshold |
| Missing credentials at startup | Set Deepgram and OpenRouter keys, plus Cartesia's key when using Cartesia TTS |
| Robot tools are unavailable | Set `ROBOT_CONFIG`, then restart |
| rosbridge connection fails | Check its URL, running process, and network access |
| Waypoint services fail | Source the custom interfaces in rosbridge's environment and check the ROS domain |
| `MCP_SERVERS_CONFIG` migration error | Remove the old setting and configure `ROBOT_CONFIG` |

Discover the robot's interfaces from a ROS-sourced terminal:

```bash
ros2 action list -t
ros2 topic list -t
ros2 service list -t
```

## Development

Run the offline suite and Python checks:

```bash
uv run python tests/run.py
uv run ruff check src tests bot.py
uv run ruff format --check src tests bot.py
```

The tests cover wake-word gating, session timing, audio service behavior, robot
tool handling, and rosbridge communication, including a localhost WebSocket
fixture. They require no API keys, wake models, audio hardware, ROS installation,
or robot. The WebSocket fixture requires permission to bind a localhost port.

Validate real wake-word detection, acoustic echo cancellation, speech providers,
and robot actions separately on the target hardware.

## Project structure

```text
.
├── bot.py                     Application entry point
├── config/
│   ├── robot.example.json     Robot interface configuration template
│   └── system_prompt.md       Assistant instructions
├── models/                    Local wake-word models and setup guide
├── src/sbem_voice/
│   ├── audio/                 Audio transport and speaker muting
│   ├── robot/                 rosbridge client, configuration, and tools
│   ├── services/              STT, LLM, and TTS factories
│   ├── wakeword/              Wake detection and conversation lifecycle
│   ├── cli.py                 Command-line interface
│   ├── config.py              Application settings
│   └── pipeline.py            Pipecat pipeline assembly
├── sys_services_audio/        PulseAudio service and startup launcher
├── tests/                     Offline regression and integration tests
├── .env.example               Environment variable template
├── pyproject.toml             Python dependencies and tool configuration
└── uv.lock                    Locked dependencies
```
