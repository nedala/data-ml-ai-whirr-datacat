#!/usr/bin/env bash
# appliance-chromium: the desktop's Chromium (supervisord program).
# Headful and windowed (not kiosk) on the XFCE display, with its own
# persistent profile and remote debugging on 127.0.0.1:9222 — this is the
# browser Playwright drives over CDP (http://browser:9223 via cdp_proxy.py),
# so pages and contexts it opens appear as windows / tabs on the desktop.
set -u
: "${BROWSER_GEOMETRY:=1920x1080x24}"
: "${BROWSER_HOME_URL:=about:blank}"
: "${BROWSER_SANDBOX:=auto}"          # auto | on | off
: "${BROWSER_EXTRA_FLAGS:=}"
# Origins allowed to open CDP websockets. Empty = only clients that send no
# Origin header (Playwright, Puppeteer, CDP libraries).
: "${CDP_ALLOW_ORIGINS:=}"

PROFILE="$HOME/.config/chromium/profile"      # bind mount: <data root>/<user>/browser
W="${BROWSER_GEOMETRY%%x*}"; rest="${BROWSER_GEOMETRY#*x}"; H="${rest%%x*}"

appliance-wait-x true || exit 1
# start once the window manager manages the screen (EWMH frame hints)
for _ in $(seq 1 100); do wmctrl -m >/dev/null 2>&1 && break; sleep 0.1; done
rm -f "$PROFILE"/Singleton{Lock,Socket,Cookie}

flags=(
  --user-data-dir="$PROFILE"
  --remote-debugging-port=9222
  --remote-debugging-address=127.0.0.1
  --no-first-run --no-default-browser-check
  --disable-dev-shm-usage
  --password-store=basic
  --disable-background-networking --disable-component-update --disable-sync --no-pings
  --disable-features=Translate,MediaRouter,OptimizationHints,OptimizationHintsFetching,OptimizationGuideModelDownloading,OptimizationTargetPrediction,SegmentationPlatform,AutofillServerCommunication
  --window-position=40,30
  --window-size="$((W - 80)),$((H - 110))"
  --test-type
)
[ -n "$CDP_ALLOW_ORIGINS" ] && flags+=(--remote-allow-origins="$CDP_ALLOW_ORIGINS")

proxy="${HTTPS_PROXY:-${https_proxy:-}}"
if [ -n "$proxy" ]; then
  bypass="<-loopback>"
  IFS=',' read -r -a np <<< "${NO_PROXY:-${no_proxy:-}}"
  for h in "${np[@]}"; do
    h="${h// /}"; [ -z "$h" ] && continue
    case "$h" in .*) h="*$h" ;; esac
    bypass="$bypass;$h"
  done
  flags+=(--proxy-server="$proxy" --proxy-bypass-list="$bypass")
fi

case "$BROWSER_SANDBOX" in
  on)  ;;
  off) flags+=(--no-sandbox) ;;
  *)   unshare --user --map-root-user --pid --fork true >/dev/null 2>&1 || flags+=(--no-sandbox) ;;
esac
# shellcheck disable=SC2206
[ -n "$BROWSER_EXTRA_FLAGS" ] && flags+=($BROWSER_EXTRA_FLAGS)

start_url=()
[ -f "$PROFILE/Default/Preferences" ] || start_url=("$BROWSER_HOME_URL")
exec env -u DBUS_SESSION_BUS_ADDRESS chromium "${flags[@]}" "${start_url[@]}"
