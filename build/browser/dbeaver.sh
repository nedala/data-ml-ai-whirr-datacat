#!/usr/bin/env bash
# DBeaver Community, started with the appliance egress proxy and trust store.
# - workspace: ~/.local/share/DBeaverData (persisted, see appliance-desktop-setup)
# - first start seeds the "Appliance warehouse" connection (postgres:5432,
#   bsp_edw, user analyst, password NOT stored: DBeaver asks for it)
# - JVM options are written to a per-user launcher ini (the install is read-only)
set -u
D=/opt/dbeaver
C="$HOME/.cache/appliance/dbeaver"
WS="$HOME/.local/share/DBeaverData/workspace6"
SEED=/usr/share/appliance/seed/dbeaver
mkdir -p "$C" "$WS/General/.dbeaver" "$WS/.metadata/.plugins/org.eclipse.core.runtime/.settings"

# 1. connection + quiet first-run prefs (seed once; the user owns them afterwards)
[ -f "$WS/General/.dbeaver/data-sources.json" ] \
  || cp "$SEED/data-sources.json" "$WS/General/.dbeaver/data-sources.json"
for f in "$SEED"/prefs/*.prefs; do
  t="$WS/.metadata/.plugins/org.eclipse.core.runtime/.settings/$(basename "$f")"
  [ -f "$t" ] || cp "$f" "$t"
done

# 2. trust store: the bundled JRE's cacerts + the appliance egress CA
store="$C/cacerts"
cp -f "$D/jre/lib/security/cacerts" "$store" && chmod 600 "$store"
if [ -s /trust/egress-ca.pem ]; then
  "$D/jre/bin/keytool" -importcert -noprompt -alias appliance-egress-ca \
    -file /trust/egress-ca.pem -keystore "$store" -storepass changeit >/dev/null 2>&1 \
    || echo "dbeaver: could not import /trust/egress-ca.pem" >&2
fi

# 3. launcher ini = shipped ini (absolute plugin paths) + appliance JVM options
ini="$C/dbeaver.ini"
sed -e "s#^plugins/#$D/plugins/#" "$D/dbeaver.ini" > "$ini"
{
  echo "-Djavax.net.ssl.trustStore=$store"
  echo "-Djavax.net.ssl.trustStorePassword=changeit"
  echo "-Ddbeaver.update.check=false"
  echo "-Dproduct.config.disable=true"      # no first-run "Product Configuration" wizard
  proxy="${HTTPS_PROXY:-${https_proxy:-}}"
  if [ -n "$proxy" ]; then
    hp="${proxy#*://}"; hp="${hp#*@}"; hp="${hp%%/*}"
    host="${hp%:*}"; port="${hp##*:}"; [ "$port" = "$hp" ] && port=3128
    nph="localhost|127.*"
    IFS=',' read -r -a np <<< "${NO_PROXY:-${no_proxy:-}}"
    for h in "${np[@]}"; do
      h="${h// /}"; [ -z "$h" ] && continue
      case "$h" in .*) h="*$h" ;; esac
      nph="$nph|$h"
    done
    for s in http https; do
      echo "-D$s.proxyHost=$host"; echo "-D$s.proxyPort=$port"
    done
    echo "-Dhttp.nonProxyHosts=$nph"
  fi
} >> "$ini"

# 4. open the main window maximised (SWT sizes it itself after the WM maps it)
(
  for _ in $(seq 1 90); do
    w=$(wmctrl -lx 2>/dev/null | awk '$3=="DBeaver.DBeaver" && / DBeaver [0-9]/ {print $1; exit}')
    if [ -n "$w" ]; then sleep 1; wmctrl -i -r "$w" -b add,maximized_vert,maximized_horz; exit 0; fi
    sleep 1
  done
) >/dev/null 2>&1 &

cd "$HOME"
exec env NO_AT_BRIDGE=1 GDK_BACKEND=x11 "$D/dbeaver" --launcher.ini "$ini" "$@" >/dev/null 2>&1
