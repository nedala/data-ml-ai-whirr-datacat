#!/usr/bin/env bash
# appliance-xfce <program…>: run an XFCE component on the virtual display
# with the session D-Bus and the XFCE environment (supervisord program).
set -u
appliance-wait-x true || exit 1
export DBUS_SESSION_BUS_ADDRESS="unix:path=/tmp/desktop/dbus.sock"
for _ in $(seq 1 50); do [ -S /tmp/desktop/dbus.sock ] && break; sleep 0.1; done
export XDG_CURRENT_DESKTOP=XFCE XDG_SESSION_DESKTOP=xfce XDG_MENU_PREFIX=xfce- \
       XDG_DATA_DIRS=/usr/local/share:/usr/share XDG_CONFIG_DIRS=/etc/xdg NO_AT_BRIDGE=1
if [ "${1:-}" = xfwm4 ]; then
  xsetroot -solid '#1c2127' 2>/dev/null || true
  xset s off s noblank -dpms 2>/dev/null || true   # no screensaver / blanking
fi
exec "$@"
