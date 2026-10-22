#!/usr/bin/env bash
# Un-minimise and raise every application window (panel launcher, Super+D,
# Ctrl+Alt+R). The panel and the desktop background (sticky, desktop -1) are
# skipped. If Chromium has no window at all, open one.
set -u
ids=$(wmctrl -l 2>/dev/null | awk '$2 != "-1" {print $1}')
for w in $ids; do
  wmctrl -i -r "$w" -b remove,hidden,shaded 2>/dev/null
  wmctrl -i -a "$w" 2>/dev/null
done
wmctrl -l -x 2>/dev/null | awk 'tolower($3) ~ /^chromium/ {f=1} END {exit !f}' || exec appliance-browser-window
