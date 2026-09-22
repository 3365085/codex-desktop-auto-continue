#!/usr/bin/env bash
set -euo pipefail

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_bin=$(command -v python3)

if [[ -x /usr/lib/chatgpt/resources/codex ]]; then
  codex_bin=/usr/lib/chatgpt/resources/codex
elif command -v codex >/dev/null 2>&1; then
  codex_bin=$(command -v codex)
else
  echo "No compatible Codex executable was found." >&2
  exit 1
fi

data_home=${XDG_DATA_HOME:-"${HOME}/.local/share"}
config_home=${XDG_CONFIG_HOME:-"${HOME}/.config"}
install_dir=${data_home}/codex-desktop-auto-continue
unit_dir=${config_home}/systemd/user
unit_path=${unit_dir}/codex-desktop-auto-continue.service

install -d -m 0755 "$install_dir" "$unit_dir"
install -m 0755 "$project_dir/codex_desktop_auto_continue.py" \
  "$install_dir/codex_desktop_auto_continue.py"

sed \
  -e "s|@PYTHON@|$python_bin|g" \
  -e "s|@WATCHER@|$install_dir/codex_desktop_auto_continue.py|g" \
  -e "s|@CODEX_BIN@|$codex_bin|g" \
  "$project_dir/systemd/codex-desktop-auto-continue.service.in" > "$unit_path"

systemctl --user daemon-reload
systemctl --user enable --now codex-desktop-auto-continue.service

echo "Installed and started codex-desktop-auto-continue.service"
echo "Status: systemctl --user status codex-desktop-auto-continue.service --no-pager"
echo "Logs:   journalctl --user -u codex-desktop-auto-continue.service -f"
