#!/usr/bin/env bash
# Desktop container entrypoint (compose service `browser`, uid 1000).
#
# Prepares the session (trust store, Chromium policy, VNC password file,
# persisted desktop state), then hands over to supervisord, which runs and
# restarts every program with logs on stdout:
#
#   xvfb        Xvfb :99 at BROWSER_GEOMETRY (default 1920x1080x24)
#   dbus        session D-Bus (xfconf, GTK apps)
#   xfsettingsd / xfwm4 / xfce4-panel / xfdesktop   the XFCE desktop (no session
#               manager: no lock screen, log-out or power management; compositor off)
#   chromium    headful, windowed, remote debugging on 127.0.0.1:9222, own profile
#   x11vnc      :5900 no password, loopback  → websockify :6080 (gated nginx /browser/)
#   x11vnc-pw   :5901 VNC_PASSWORD, loopback → websockify :6081 (dedicated host port)
#   cdp         :9223 CDP proxy for appliance-net (http://browser:9223), Host-rewriting
#
# `browser-entrypoint <cmd…>` runs a command instead (debug).
set -euo pipefail

log() { printf '%s browser: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

[[ $# -gt 0 ]] && exec "$@"
[[ ${EUID} -ne 0 ]] || { log "refusing to run as root"; exit 77; }

: "${DISPLAY:=:99}"
: "${BROWSER_GEOMETRY:=1920x1080x24}"
: "${BROWSER_HOME_URL:=about:blank}"
export DISPLAY BROWSER_GEOMETRY BROWSER_HOME_URL

RUN_DIR=/tmp/desktop
NSSDB="$HOME/.pki/nssdb"
mkdir -p "$RUN_DIR" "$NSSDB" "$HOME/Downloads" "$HOME/.config/chromium/profile"
rm -f "/tmp/.X${DISPLAY#:}-lock" "/tmp/.X11-unix/X${DISPLAY#:}" 2>/dev/null || true

# ── trust: the appliance egress CA into the NSS db Chromium reads ─────────
[[ -f "$NSSDB/cert9.db" ]] || certutil -d "sql:$NSSDB" -N --empty-password
if [[ -s /trust/egress-ca.pem ]]; then
  certutil -d "sql:$NSSDB" -D -n appliance-egress-ca >/dev/null 2>&1 || true
  certutil -d "sql:$NSSDB" -A -t "C,," -n appliance-egress-ca -i /trust/egress-ca.pem
  log "trusted /trust/egress-ca.pem"
else
  log "no /trust/egress-ca.pem yet; public trust only"
fi

# the appliance's own local CA signs the https facade (CN=<PUBLIC_FACADE_HOST>),
# so browsing https://localhost:<facade> inside the desktop needs no warning
if [[ -s /trust-tls/appliance-ca.pem ]]; then
  certutil -d "sql:$NSSDB" -D -n appliance-local-ca >/dev/null 2>&1 || true
  certutil -d "sql:$NSSDB" -A -t "C,," -n appliance-local-ca -i /trust-tls/appliance-ca.pem
  log "trusted /trust-tls/appliance-ca.pem"
fi

# ── managed homepage policy ───────────────────────────────────────────────
home_json=$(printf '%s' "$BROWSER_HOME_URL" | sed 's/\\/\\\\/g; s/"/\\"/g')
printf '{\n  "HomepageLocation": "%s"\n}\n' "$home_json" > /etc/chromium/policies/managed/zz-homepage.json

# ── VNC password for the dedicated (published) noVNC port ─────────────────
# The gated nginx route uses the password-less loopback VNC server; the host
# port bypasses the Studio gate, so it requires VNC_PASSWORD (from the vault).
umask 077
if [[ -n "${VNC_PASSWORD:-}" ]]; then
  x11vnc -storepasswd "$VNC_PASSWORD" "$RUN_DIR/vncpasswd" >/dev/null 2>&1
  DIRECT_VNC=true
else
  log "VNC_PASSWORD not set — the direct noVNC port stays closed (start the Desktop from Studio)"
  DIRECT_VNC=false
fi
umask 022
unset VNC_PASSWORD

# ── persisted desktop state (best effort) ─────────────────────────────────
appliance-desktop-setup || log "desktop setup reported a problem (apps may not persist state)"

# Google ADC (vault key → RAM file) + OpenCode defaults for every tool started below
# shellcheck source=build/services/gcp-adc.sh
[[ -r /opt/services/gcp-adc.sh ]] && source /opt/services/gcp-adc.sh

# ── supervisord config from the template ──────────────────────────────────
sed -e "s#@DIRECT_VNC@#${DIRECT_VNC}#g" /etc/appliance/supervisord.conf > "$RUN_DIR/supervisord.conf"
log "starting supervisord: display ${DISPLAY} ${BROWSER_GEOMETRY}, noVNC :6080 (gated) / :6081 (password), CDP :9223"
exec supervisord -n -c "$RUN_DIR/supervisord.conf"
