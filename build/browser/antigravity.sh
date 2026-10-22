#!/usr/bin/env bash
# Google Antigravity (VS Code-based agentic IDE) — a desktop app on the Linux
# Desktop (it has no web/server mode). Runs as uid 1000; user data and
# extensions persist in the desktop state folder (<data root>/<user>/desktop);
# opens ~/work by default. Its agent's browser integration uses the desktop
# Chromium (CHROME_PATH / BROWSER).
set -u
BIN=/usr/local/bin/antigravity-ide
[ -x "$BIN" ] || { xmessage -center "Antigravity is not installed in this image (built with ANTIGRAVITY=0 or the download failed)." 2>/dev/null; exit 1; }
STATE="${DESKTOP_STATE_DIR:-$HOME/.local/share/appliance}/antigravity"
mkdir -p "$STATE/user-data" "$STATE/extensions"
export CHROME_PATH=/usr/bin/chromium BROWSER=appliance-browser-window
args=(--user-data-dir "$STATE/user-data" --extensions-dir "$STATE/extensions" --disable-gpu)
unshare --user --map-root-user true >/dev/null 2>&1 || args+=(--no-sandbox)
[ $# -gt 0 ] || set -- "$HOME/work"
appliance-maximise 'antigravity' >/dev/null 2>&1 &
exec env NO_AT_BRIDGE=1 "$BIN" "${args[@]}" "$@" >/dev/null 2>&1
