#!/usr/bin/env bash
# Removes voice dictation completely: service, kanata changes, and this folder.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
systemctl --user disable --now voice-dictation 2>/dev/null || true
rm -f ~/.config/systemd/user/voice-dictation.service
cp "$DIR/kanata.kbd.backup" ~/Kanata/kanata.kbd
sed -i 's| --port 127.0.0.1:7070||' ~/.config/systemd/user/kanata.service
gsettings set org.gnome.shell.extensions.dash-to-dock hot-keys true
systemctl --user daemon-reload
systemctl --user restart kanata
rm -rf "$DIR"
echo "Voice dictation removed."
