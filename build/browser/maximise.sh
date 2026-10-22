#!/usr/bin/env bash
# appliance-maximise <WM_CLASS regex>: wait (up to 90 s) for the first NEW
# window whose WM_CLASS (wmctrl -lx, column 3) matches, then maximise it.
# Launchers run it in the background so desktop apps open maximised, as they
# did with the previous window manager; later windows (dialogs) are left alone.
set -u
re="$1"
cls() { wmctrl -lx 2>/dev/null | awk -v re="$re" 'tolower($3) ~ tolower(re) {print $1}'; }
before=" $(cls | tr '\n' ' ') "
for _ in $(seq 1 90); do
  for w in $(cls); do
    case "$before" in *" $w "*) continue ;; esac
    sleep 1
    exec wmctrl -i -r "$w" -b add,maximized_vert,maximized_horz
  done
  sleep 1
done
exit 0
