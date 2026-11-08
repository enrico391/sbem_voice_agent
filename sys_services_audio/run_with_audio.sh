#!/usr/bin/env bash
# Select PulseAudio's virtual endpoints and resolve the current PyAudio pulse index.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
config_file="$HOME/.config/sbem-audio/audio.json"
if [[ ! -f "$config_file" ]]; then
    config_file="$project_dir/sys_services_audio/audio.json"
fi
routing="$(/usr/bin/python3 "$project_dir/sys_services_audio/audio_service.py" \
    --config "$config_file" --routing)"
# Values are validated device names, not shell commands or arbitrary config text.
while IFS='=' read -r key value; do
    case "$key" in
        PULSE_SOURCE) export PULSE_SOURCE="$value" ;;
        PULSE_SINK) export PULSE_SINK="$value" ;;
    esac
done <<< "$routing"
# Fail before opening audio if echo cancellation is not ready yet.
pactl get-source-volume "$PULSE_SOURCE" >/dev/null
pactl get-sink-volume "$PULSE_SINK" >/dev/null
pulse_index="$(uv run python - <<'PY'
import pyaudio

audio = pyaudio.PyAudio()
try:
    devices = [i for i in range(audio.get_device_count())
               if (d := audio.get_device_info_by_index(i))['name'] == 'pulse'
               and d['maxInputChannels'] > 0 and d['maxOutputChannels'] > 0]
    if not devices:
        raise SystemExit('No duplex PyAudio pulse device found; check libasound2-plugins.')
    print(devices[0])
finally:
    audio.terminate()
PY
)"
exec uv run bot.py --input-device "$pulse_index" --output-device "$pulse_index" \
    --input-sample-rate 16000 --output-sample-rate 48000 --allow-interruptions "$@"
