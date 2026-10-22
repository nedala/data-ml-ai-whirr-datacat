#!/usr/bin/env bash
# Prepare the analyst's desktop session (runs once per container start, before
# the window manager). Everything here is best effort: a failure is logged by
# the supervisor and never stops the container.
#
# Persisted state: one folder, $DESKTOP_STATE_DIR (bind mount
# ./data/<user>/desktop -> /home/analyst/.local/share/appliance). The apps'
# usual config locations are symlinked into it, so their own CLIs and defaults
# keep working:
#   ~/.local/share/DBeaverData     -> $STATE/dbeaver          (workspace, connections, drivers)
#   ~/.config/Code                 -> $STATE/vscode/user-data (settings, state)
#   ~/.vscode                      -> $STATE/vscode/home      (extensions)
#   ~/.local/share/keyrings        -> $STATE/keyrings         (Secret Service store)
#   ~/.config/xfce4                -> $STATE/xfce4            (desktop: panel, shortcuts, theme
#                                     changes; system defaults in /etc/xdg/xfce4)
#   ~/.bash_history                -> $STATE/bash_history
set -u
STATE="${DESKTOP_STATE_DIR:-$HOME/.local/share/appliance}"
mkdir -p "$STATE" "$HOME/.config" "$HOME/.local/share" "$HOME/.cache" || exit 1
[ -w "$STATE" ] || { echo "desktop-setup: $STATE is not writable" >&2; exit 1; }

link() {  # link <path in $HOME> <dir|file under $STATE>
  local target="$STATE/$2" path="$HOME/$1"
  if [ "${3:-dir}" = dir ]; then mkdir -p "$target"; else touch "$target"; fi
  mkdir -p "$(dirname "$path")"
  if [ -e "$path" ] && [ ! -L "$path" ]; then rm -rf "$path"; fi
  ln -sfn "$target" "$path"
}
link .local/share/DBeaverData dbeaver
link .config/Code vscode/user-data
link .vscode vscode/home
link .local/share/keyrings keyrings
link .antigravity antigravity/home          # Antigravity extensions / agent state
link .gemini antigravity/gemini             # Antigravity agent config
link .config/Antigravity antigravity/config
link .config/xfce4 xfce4
link .bash_history bash_history file
chmod 700 "$STATE/keyrings" 2>/dev/null || true

# The panel keeps its own copy of each launcher (~/.config/xfce4/panel/
# launcher-N/*.desktop). Refresh the copies of the appliance launchers from
# the image, so a new image's icons/commands win over a persisted old copy.
for f in "$STATE"/xfce4/panel/launcher-*/*.desktop; do
  [ -f "$f" ] || continue
  src=$(sed -n 's#^X-XFCE-Source=file://##p' "$f")
  case "$src" in /usr/share/applications/appliance-*.desktop) ;; *) continue ;; esac
  [ -f "$src" ] && { grep -v '^X-XFCE-Source=' "$src"; echo "X-XFCE-Source=file://$src"; } > "$f"
done

# File manager (Thunar) bookmarks.
mkdir -p "$HOME/work" "$HOME/.config/gtk-3.0"
{
  echo "file://$HOME/work My work"
  [ -d /data/lake ] && echo "file:///data/lake Shared lake"
  echo "file://$HOME/Downloads Downloads"
} > "$HOME/.config/gtk-3.0/bookmarks"
# Desktop icons (xfdesktop shows ~/Desktop): seeded each start from the image's
# launchers for apps present in this build; executable + checksum-trusted so
# xfdesktop launches them without the "untrusted launcher" prompt.
DESK="$HOME/Desktop"
mkdir -p "$DESK"
for app in browser terminal files vscode antigravity dbeaver; do
  src="/usr/share/applications/appliance-$app.desktop"
  dst="$DESK/appliance-$app.desktop"
  if [ -f "$src" ]; then
    cp -f "$src" "$dst" && chmod 755 "$dst"
    if command -v gio >/dev/null 2>&1; then
      gio set -t string "$dst" metadata::xfce-exe-checksum "$(sha256sum "$dst" | cut -d' ' -f1)" 2>/dev/null || true
    fi
  else
    rm -f "$dst"
  fi
done
exit 0
