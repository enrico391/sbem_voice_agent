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
- Optional streaming text API over WebSocket/RTVI, with independent conversation
  history per connection and an API-only mode requiring no audio hardware.
- Persistent SQLite memory with tools to remember, retrieve, correct, and forget facts.
- Nine focused robot tools, with pushed progress and completion updates.
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
| `SESSION_TIMEOUT_SECS` | Inactivity gap before a fresh conversation context | `300` |
| `OPENROUTER_MODEL` | OpenRouter model identifier | `deepseek/deepseek-v4.1-flash` |
| `CARTESIA_VOICE_ID` | Cartesia voice identifier | See `src/sbem_voice/config.py` |
| `SYSTEM_PROMPT_FILE` | UTF-8 system prompt | `config/system_prompt.md` |
| `ROBOT_CONFIG` | Robot interface configuration | Unset; conversation only |
| `MEMORY_DB_PATH` | Persistent SQLite memory file, relative to the project root | `data/memory.sqlite3` |
| `MEMORY_USER_ID` | Application-selected memory profile | `default` |
| `API_ENABLED` | Enable the WebSocket API alongside voice (`true`/`false`) | `false` |
| `API_HOST` | API bind address | `0.0.0.0` |
| `API_PORT` | API listening port, 1–65535 | `8765` |
| `API_TOKEN` | Optional token required as `Authorization: Bearer <token>` | Unset |

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

The listening timeout pauses during speech, response generation, and pending tools.
Sleep keeps the process running. Follow-ups retain conversation history until
`SESSION_TIMEOUT_SECS` passes without conversation activity (five minutes by
default). After that gap, history is cleared before the next admitted request;
the system instructions, available tools, and saved SQLite memories remain.
Speech, response generation, and pending tools are never cut off by this timeout.
This inactivity policy does not limit the size of a continuously active conversation.
While asleep,
microphone audio is blocked before speech recognition, although the provider
connection may remain open for keepalives.

The `end_conversation()` tool lets SBEM handle a clear goodbye or a request to stop
listening. It blocks new conversational input, lets the final reply and pending
tool results finish, then returns to wake-word listening. The next wake starts a
fresh conversation. It does not shut down the app or cancel robot motion.

Press **Ctrl+C** to stop.

### Streaming text API

Enable the API alongside the microphone:

```bash
uv run bot.py --api
# With echo cancellation: ./sys_services_audio/run_with_audio.sh --api
```

Or run just the API, without microphone/speaker access, a wake-word model, STT or
TTS credentials. Only `OPENROUTER_API_KEY` is required for conversation:

```bash
uv run bot.py --api-only --api-port 8765
```

`API_ENABLED=true` in `.env` is equivalent to `--api`. `--api-host` and `--api-port`
override the environment settings. The API binds all network interfaces
(`0.0.0.0`) by default. Connect locally at `ws://127.0.0.1:8765/ws` or remotely at
`ws://<robot-ip>:8765/ws`. Set `API_TOKEN` to require a bearer token; remote deployments should
use a TLS reverse proxy (`wss://`). The example Python client reads `API_TOKEN`
from its environment and streams until **Ctrl+C**:

```bash
uv run python examples/api_client.py "What can you help me with?"
# Custom address:
uv run python examples/api_client.py --url ws://127.0.0.1:9000/ws "Hello"
```

The wire format is **JSON RTVI 2.2.0**, using Pipecat's native
`FastAPIWebsocketTransport`, `RTVIProcessor`, and `RTVIObserver`. No Protobuf or
Pipecat client SDK is required. First send a handshake and wait for `bot-ready`:

```json
{"label":"rtvi-ai","type":"client-ready","id":"ready","data":{"version":"2.2.0"}}
```

Then send text; repeat on the same connection for follow-up messages:

```json
{"label":"rtvi-ai","type":"send-text","id":"request-1","data":{"content":"What is the battery level?","options":{"audio_response":false}}}
```

Text streams back as `llm-raw-text` events whose `data.text` contains each chunk.
`bot-llm-started` / `bot-llm-stopped` bracket individual model generations. Tool
events include `llm-function-call-in-progress` and `llm-function-call-stopped`,
with function names and call IDs; arguments and results are not exposed in those
events. `user-input` acknowledges typed input with `data.msg_id`. Text chunks
and generation events are connection-scoped, without a per-request ID: keep one
active request per connection unless deliberately interrupting it.

An LLM generation ending does **not** mean a background robot action has finished.
Keep reading: tool progress/results can trigger additional replies. A new
`send-text` interrupts the current reply by default (`run_immediately: true`);
`run_immediately: false` appends context without requesting a reply. This follows
native RTVI behavior. Requests are text-only: omit options or set
`audio_response: false`. Malformed input returns `error-response`; provider
failures may produce RTVI `error` events. Messages are limited to 64 KiB on the
wire and 16,000 characters of input text.

Every connection has an independent LLM context, including from microphone
conversations. Reconnecting starts fresh. API history lasts for the connection;
the microphone's idle-history reset does not apply. Saved SQLite memories still
use the configured `MEMORY_USER_ID` across interfaces; this is conversation
separation, not separate user accounts. All interfaces share one robot connection
and motion lock, so competing motion commands are rejected. Closing a connection
tears down its pipeline, not other conversations. It does not guarantee stopping
robot motion; request cancellation explicitly and check the result.

Close the WebSocket to end a session, or send:

```json
{"label":"rtvi-ai","type":"disconnect-bot","id":"bye"}
```

### Persistent memory

Memory tools are available with or without robot integration. The database and
its parent directory are created automatically on startup, using Python's built-in
SQLite library. No additional API key or dependency is needed. Saved facts survive
sleep and application restarts; the conversation transcript remains in process memory.

Try saying:

- "Remember that I prefer speaking Italian."
- "What do you remember about my language preference?"
- "Update my language preference to English."
- "Forget my language preference."

The tools are `remember_memory(content)`,
`retrieve_memory(query="", limit=20, offset=0)`,
`update_memory(memory_id, content)`, and `remove_memory(memory_id)`.
Retrieval uses literal, case-insensitive substring matching, including Unicode;
it is not semantic search. An empty query lists saved memories. Results include
record IDs, timestamps, `has_more`, and `next_offset` for pagination, with at most
50 records per page. Updates and removals target an exact retrieved ID. Each fact
is limited to 2000 characters, and identical facts in a profile are not duplicated.

This version remembers facts only through tool calls, guided by explicit user
requests. It does not automatically extract facts or preload a profile on every
turn. If you use a custom `SYSTEM_PROMPT_FILE`, include the persistent-memory
instructions from the default prompt so the assistant knows when to use the tools.

All speakers share `MEMORY_USER_ID=default` until identification is implemented.
Changing that setting selects a separate profile in the same database; it does
not identify a speaker or migrate old facts. The model cannot choose a user ID.
Future identification code can select the profile when creating session resources.

The database is local, unencrypted, and excluded from Git at the default path.
Exclude custom storage paths from version control too. Retrieved facts are sent
to the configured LLM as tool results. Forgetting deletes a stored record; it
does not erase previous turns from the running conversation or existing backups.
Stop the assistant before copying the database file for a simple backup.

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
| `camera.topic` | Camera topic using `sensor_msgs/msg/Image`; default `/camera/image_raw` |
| `camera.timeout_secs`, `camera.max_dimension` | Snapshot timeout and maximum output edge; defaults `5` seconds and `1024` pixels |
| `cmd_vel.topic` | Direct velocity topic using `geometry_msgs/msg/Twist`; default `/cmd_vel` |
| `cmd_vel.max_linear_x`, `cmd_vel.max_angular_z` | Absolute speed limits; defaults `0.2` m/s and `0.5` rad/s |
| `cmd_vel.max_duration_secs`, `cmd_vel.publish_rate_hz` | Command duration limit and publishing rate; defaults `5` seconds and `10` Hz (maximum `100` Hz) |
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
| `cmd_vel(linear_x, angular_z, duration_secs=1)` | Publishes bounded direct velocity for a short duration, then sends zero velocity; both speeds zero requests an early stop |
| `dock()` | Runs the configured docking action |
| `undock()` | Runs the configured undocking action |
| `get_battery_status()` | Returns fresh battery percentage, voltage, and charging status when available |
| `get_camera_image()` | Captures one camera frame and attaches a JPEG image to the LLM context for visual analysis |

Battery results include `charging: true` when `power_supply_status` is `1`, and
`charging: false` when it is `2`. Missing or other status values omit `charging`.

The camera tool subscribes on demand, waits for one newly received frame, then
unsubscribes. It accepts `rgb8`, `bgr8`, `rgba8`, `bgra8`, and `mono8`, including
padded image rows. It resizes the image while preserving its aspect ratio and
attaches the actual JPEG as an image message before the model's next response.
The tool result contains only capture status and dimensions. Raw images are
limited to 10 MiB and incoming WebSocket messages to 16 MiB. Depth and compressed
image topics are not supported by this tool.

Use an OpenRouter model that supports both image inputs and tools. The project
default supports both; custom `OPENROUTER_MODEL` selections must also do so.
For a RealSense color camera, configure the topic in `config/robot.json`:

```json
"camera": {
  "topic": "/camera0/camera/color/image_raw",
  "timeout_secs": 5,
  "max_dimension": 1024
}
```

Ask, for example, “What can you see?” or “Read the label in front of you.” The
model receives a snapshot, not continuous video, and reports unavailable images
or uncertain details rather than inventing a view. Images remain in the current
conversation context and follow its normal reset behavior; they are not written
to disk or saved as persistent memories by the camera tool.

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

For example, `cmd_vel(0.1, 0, 1)` requests forward motion at 0.1 m/s for one
second; `cmd_vel(0, -0.3, 1)` requests a right turn. Negative `linear_x` moves
backward; positive `angular_z` turns left. The other axes are always zero.
Commands outside the configured limits are rejected. Existing configurations
without a `cmd_vel` block use the defaults above; add the block from the example
to change them. The velocity topic must accept `Twist`, not `TwistStamped`.

Direct velocity shares the motion lock with navigation, docking, and undocking.
`cmd_vel(0, 0)` ends this session's active velocity stream early; it does not
cancel navigation or docking. The stream also attempts to send zero velocity
on cancellation or app shutdown. A `command_sent` result confirms only that the
commands were sent to rosbridge, and `stop_requested` means the active stream
was signaled to end; neither verifies physical movement or stopping. Connection
or send failures leave motion uncertain and block further nonzero commands.
Keep the base controller's command timeout enabled so it stops if the connection
or process is lost. Coordinate the velocity topic or command multiplexer with
other robot controllers; the app's motion lock only covers its own commands.
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
uv run ruff check src tests examples evals bot.py
uv run ruff format --check src tests examples evals bot.py
```

The tests cover wake-word gating, session timing, audio service behavior, robot
tool handling, and rosbridge communication, plus real API WebSockets, streamed
text/tool replies, history isolation, authorization and API-only CLI startup.
They require no API keys, wake models, audio hardware, ROS installation,
or robot. The WebSocket fixture requires permission to bind a localhost port.

Validate real wake-word detection, acoustic echo cancellation, speech providers,
and robot actions separately on the target hardware.

For a live, headless text check, run the same API pipeline through Pipecat's
native eval transport in a separate development server. This costs two requests
to the configured LLM. No speech models or judge are needed; the scenario uses
deterministic text assertions. The eval controls are not exposed on the public
API endpoint.

```bash
# Terminal 1: disable robot tools and keep test memories separate.
ROBOT_CONFIG= MEMORY_DB_PATH=/tmp/sbem-api-eval.sqlite3 \
  uv run python evals/bot.py --host 127.0.0.1 --port 8766

# Terminal 2: native Pipecat EvalSession library (no CLI extras required).
uv run python evals/run_text.py
```

If the `pipecat-ai[evals]` extra is already installed, the equivalent CLI is
`uv run pipecat eval run evals/api_text.yaml --bot-url ws://127.0.0.1:8766/ws -v`.
That optional extra includes local speech dependencies; it is not needed for
the API or the supplied text-only runner.

## Project structure

```text
.
├── bot.py                     Application entry point
├── config/
│   ├── robot.example.json     Robot interface configuration template
│   └── system_prompt.md       Assistant instructions
├── evals/                     Native headless text eval and development runner
├── examples/api_client.py     JSON RTVI streaming client
├── models/                    Local wake-word models and setup guide
├── src/sbem_voice/
│   ├── api/                   WebSocket server, isolated sessions, JSON serializer
│   ├── audio/                 Audio transport and speaker muting
│   ├── memory/                SQLite persistence and native memory tools
│   ├── robot/                 rosbridge client, configuration, and tools
│   ├── services/              STT, LLM, and TTS factories
│   ├── wakeword/              Wake detection and conversation lifecycle
│   ├── cli.py                 Command-line interface
│   ├── config.py              Application settings
│   ├── pipeline.py            Pipecat pipeline assembly
│   └── resources.py           Shared memory and robot resources
├── sys_services_audio/        PulseAudio service and startup launcher
├── tests/                     Offline regression and integration tests
├── .env.example               Environment variable template
├── pyproject.toml             Python dependencies and tool configuration
└── uv.lock                    Locked dependencies
```
