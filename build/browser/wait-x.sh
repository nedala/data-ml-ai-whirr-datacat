#!/usr/bin/env bash
# appliance-wait-x <cmd…>: wait (≤10 s) for the X display, then exec cmd.
set -u
for _ in $(seq 1 100); do xdpyinfo >/dev/null 2>&1 && exec "$@"; sleep 0.1; done
echo "X display ${DISPLAY:-:99} not available" >&2
exit 1
