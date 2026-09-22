#!/usr/bin/env bash
set -euo pipefail

data_home=${XDG_DATA_HOME:-"${HOME}/.local/share"}
config_home=${XDG_CONFIG_HOME:-"${HOME}/.config"}
install_dir=${data_home}/codex-desktop-auto-continue
unit_path=${config_home}/systemd/user/codex-desktop-auto-continue.service

systemctl --user disable --now codex-desktop-auto-continue.service 2>/dev/null || true
rm -f "$unit_path"
rm -rf "$install_dir"
systemctl --user daemon-reload

echo "Removed codex-desktop-auto-continue.service and $install_dir"
