#!/usr/bin/env bash
# Run as the desktop user who owns PulseAudio, without sudo.
set -euo pipefail
if [[ "$EUID" -eq 0 ]]; then
    echo "Run this installer as your desktop user, without sudo." >&2
    exit 1
fi
service_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
command -v pactl >/dev/null
command -v systemctl >/dev/null
config_dir="$HOME/.config/sbem-audio"
lib_dir="$HOME/.local/lib/sbem-audio"
unit_dir="$HOME/.config/systemd/user"
install -d "$config_dir" "$lib_dir" "$unit_dir"
# Stop the previous controller before replacing its code/configured files.
if systemctl --user is-active --quiet sbem-audio.service; then
    systemctl --user stop sbem-audio.service
fi
install -m 644 "$service_dir/audio_service.py" "$lib_dir/audio_service.py"
install -m 644 "$service_dir/sbem-audio.service" "$unit_dir/sbem-audio.service"
if [[ ! -f "$config_dir/audio.json" ]]; then
    install -m 644 "$service_dir/audio.json" "$config_dir/audio.json"
fi
/usr/bin/python3 "$lib_dir/audio_service.py" --config "$config_dir/audio.json" --routing \
    > "$config_dir/audio-routing.env"
systemctl --user daemon-reload
systemctl --user enable --now sbem-audio.service
printf '%s\n' 'Service enabled. Check: systemctl --user status sbem-audio.service' \
    'Audio readiness: journalctl --user -u sbem-audio.service -n 30'
