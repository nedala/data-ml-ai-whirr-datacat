#!/usr/bin/env bash
# Open a Chromium window in the running profile (optionally on the given
# URLs: links opened from Files, the terminal or other apps land here). The
# supervisor owns the main Chromium process (and restarts it); this only asks
# that process for a new window, so a second instance never fights over the
# profile.
set -u
PROFILE="$HOME/.config/chromium/profile"
# This short-lived process starts Chromium's zygote before it hands over to
# the running browser: without user namespaces (Docker's default seccomp) it
# needs --no-sandbox like the main process, or it crashes before handing over.
args=(--user-data-dir="$PROFILE" --new-window --test-type)
unshare --user --map-root-user true >/dev/null 2>&1 || args+=(--no-sandbox)
for _ in $(seq 1 20); do                      # supervisor restarts within ~2 s
  if [ -L "$PROFILE/SingletonLock" ]; then
    appliance-maximise '^chromium' >/dev/null 2>&1 &   # the new window opens maximised
    exec chromium "${args[@]}" "$@" >/dev/null 2>&1
  fi
  sleep 0.5
done
echo "browser is not running (the appliance restarts it automatically)" >&2
exit 1
