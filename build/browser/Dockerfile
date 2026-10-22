# Appliance desktop — a real XFCE desktop (xfwm4 window manager, panel with
# launchers + window list, xfdesktop, Terminal, Thunar) on Xvfb, shown through
# noVNC, with Chromium as the browser people sign in with AND the browser
# Playwright drives over CDP. Every program runs under supervisord.
#
# Built ON the workspace image, so the desktop terminal has the SAME Python
# as JupyterLab / code-server (/usr/local/bin/python + every package).
#   docker build -t bsp/appliance:latest .            # first
#   docker build -t bsp/browser:latest build/browser
#   docker build --build-arg WITH_DBEAVER=0 ...       # leaner, without DBeaver
# Builds on amd64 and arm64 (arch-aware downloads).
# Ports: 6080 noVNC (gated nginx /browser/, no VNC password),
#        6081 noVNC with VNC_PASSWORD (dedicated host port via nginx),
#        9223 CDP for appliance-net (cdp_proxy.py -> 127.0.0.1:9222).
ARG BASE_IMAGE=bsp/appliance:latest
FROM ${BASE_IMAGE}

USER root
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG DEBIAN_FRONTEND=noninteractive

# Keep docs/man/translations of the packages added here out of the layer
# (deleting files that belong to the base image would save nothing).
RUN printf '%s\n' \
      'path-exclude=/usr/share/doc/*' 'path-include=/usr/share/doc/*/copyright' \
      'path-exclude=/usr/share/man/*' 'path-exclude=/usr/share/info/*' \
      'path-exclude=/usr/share/locale/*' 'path-include=/usr/share/locale/locale.alias' \
      > /etc/dpkg/dpkg.cfg.d/zz-appliance-desktop

# Debian 13 (trixie) packages. python3-websockify pulls Debian's python3
# (3.13); it is not on the analyst's path ahead of /usr/local/bin (see
# /etc/profile.d below). XFCE: only the pieces the desktop uses, without
# recommends (so no xfce4-session, xfce4-screensaver / light-locker,
# xfce4-power-manager or goodies); greybird = the Xfce look; librsvg2-common
# renders the SVG icons.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      chromium chromium-sandbox \
      xvfb x11-utils x11-xserver-utils wmctrl xdotool x11vnc \
      xfwm4 xfce4-panel xfce4-whiskermenu-plugin xfdesktop4 xfce4-settings xfconf \
      xfce4-terminal thunar xfce4-notifyd greybird-gtk-theme adwaita-icon-theme librsvg2-common \
      dbus-daemon dbus-session-bus-common \
      python3-websockify supervisor libglib2.0-bin \
      socat tini curl ca-certificates libnss3-tools util-linux \
      fonts-liberation fonts-dejavu-core fonts-noto-core fonts-noto-color-emoji fontconfig \
 && cd /tmp && apt-get download novnc \
 && dpkg-deb -x novnc_*.deb /tmp/novnc && cp -a /tmp/novnc/usr/share/novnc /usr/share/novnc \
 && rm -rf /tmp/novnc /tmp/novnc_*.deb \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* /var/cache/debconf/*-old

# --- DBeaver Community (SQL IDE; bundles its own JRE) -----------------------
ARG WITH_DBEAVER=1
ARG DBEAVER_VERSION=26.2.2
ARG DBEAVER_SHA256_ARM64=c64401a72d6bd001a828094192704653cd51bc75c14b602e37b9a3fd0ea4b0ad
ARG DBEAVER_SHA256_AMD64=27b39a79a69ec70f9f95c6ee3b68514fcc7677acef93287e8df8e13cc53384d5
RUN set -eu; [ "$WITH_DBEAVER" = 1 ] || exit 0; \
    case "$(dpkg --print-architecture)" in \
      arm64) a=aarch64; sum="$DBEAVER_SHA256_ARM64" ;; \
      amd64) a=x86_64;  sum="$DBEAVER_SHA256_AMD64" ;; \
      *) echo "DBeaver: unsupported arch"; exit 0 ;; \
    esac; \
    curl -fsSL -o /tmp/dbeaver.tgz \
      "https://github.com/dbeaver/dbeaver/releases/download/${DBEAVER_VERSION}/dbeaver-ce-${DBEAVER_VERSION}-linux-${a}.tar.gz"; \
    echo "$sum  /tmp/dbeaver.tgz" | sha256sum -c -; \
    tar -xzf /tmp/dbeaver.tgz -C /opt; rm -f /tmp/dbeaver.tgz; \
    # AI chat needs WebKitGTK (+244 MB) and an external LLM key: leave its bundles out
    sed -i '/^org\.jkiss\.dbeaver\.\(model\|ui\)\.ai,/d' \
      /opt/dbeaver/configuration/org.eclipse.equinox.simpleconfigurator/bundles.info; \
    chown -R root:root /opt/dbeaver

# --- Google Antigravity (agentic IDE; desktop app, no server mode) ----------
# Installed BY DEFAULT from Google's official download location (the
# standalone IDE tarball listed on https://antigravity.google/download?os=linux;
# the old APT/RPM repositories are legacy 1.x only and no longer updated).
# INSTALL_ANTIGRAVITY=0 (`make build ANTIGRAVITY=0`) leaves it out. The build
# NEVER fails on it: 3 attempts, then a loud warning and no marker file, so
# the launcher, desktop icon and Studio tile hide themselves.
ARG INSTALL_ANTIGRAVITY=1
ARG ANTIGRAVITY_VERSION=2.5.5-4923483625488384
ARG ANTIGRAVITY_BASE_URL=https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable
RUN set -u; mkdir -p /opt/appliance-features; [ "$INSTALL_ANTIGRAVITY" = 1 ] || { echo "Antigravity: skipped (INSTALL_ANTIGRAVITY=0)"; exit 0; }; \
    case "$(dpkg --print-architecture)" in amd64) a=linux-x64 ;; arm64) a=linux-arm ;; *) a="" ;; esac; \
    ok=0; \
    if [ -n "$a" ]; then \
      for i in 1 2 3; do \
        if curl -fsSL --connect-timeout 20 -o /tmp/ag.tgz "${ANTIGRAVITY_BASE_URL}/${ANTIGRAVITY_VERSION}/${a}/Antigravity%20IDE.tar.gz"; then ok=1; break; fi; \
        echo "Antigravity download attempt $i failed"; sleep $((i * 5)); \
      done; \
    fi; \
    if [ "$ok" = 1 ] && mkdir -p /opt/antigravity && tar -xzf /tmp/ag.tgz -C /opt/antigravity --strip-components=1; then \
      bin=""; for c in antigravity "antigravity-ide" "Antigravity IDE" Antigravity; do \
        [ -x "/opt/antigravity/$c" ] && [ ! -d "/opt/antigravity/$c" ] && { bin="/opt/antigravity/$c"; break; }; done; \
      [ -n "$bin" ] || bin="$(find /opt/antigravity -maxdepth 1 -type f -perm -u+x ! -name '*.so*' ! -name 'chrome*' -iname '*antigravity*' | head -1)"; \
      if [ -n "$bin" ]; then \
        ln -sf "$bin" /usr/local/bin/antigravity-ide; \
        apt-get update -qq && apt-get install -y --no-install-recommends libgbm1 libasound2t64 libxss1 libxkbfile1 \
          libsecret-1-0 gnome-keyring libgtk-3-0t64 libnotify4 xdg-utils x11-utils >/dev/null || echo "Antigravity: some runtime libraries missing"; \
        rm -rf /var/lib/apt/lists/*; \
        icon="$(find /opt/antigravity -path '*resources/app/resources/linux/*.png' | head -1)"; \
        mkdir -p /usr/share/appliance/icons; [ -n "$icon" ] && cp "$icon" /usr/share/appliance/icons/antigravity.png; \
        chown -R root:root /opt/antigravity; chmod -R a+rX /opt/antigravity; \
        [ -f /opt/antigravity/chrome-sandbox ] && chmod 4755 /opt/antigravity/chrome-sandbox; \
        echo "${ANTIGRAVITY_VERSION}" > /opt/appliance-features/antigravity; \
        echo "Antigravity ${ANTIGRAVITY_VERSION} installed: $bin"; \
      else ok=0; fi; \
    else ok=0; fi; \
    rm -f /tmp/ag.tgz; \
    if [ "$ok" != 1 ]; then rm -rf /opt/antigravity; \
      echo "############################################################################"; \
      echo "## WARNING: Antigravity could NOT be installed (download unreachable or    ##"; \
      echo "## unexpected archive layout). The image is built WITHOUT it; its launcher, ##"; \
      echo "## desktop icon and Studio tile are hidden. Re-run make build to retry.     ##"; \
      echo "############################################################################"; \
    fi; exit 0

# The workspace image already has analyst (uid 1000, group analysts gid 1000),
# /home/analyst/work, /data/lake and /data/pyuser; add the desktop's dirs.
RUN getent passwd 1000 >/dev/null \
 || { groupadd -g 1000 analyst && useradd -u 1000 -g 1000 -m -d /home/analyst -s /bin/bash analyst; } \
 && install -d -o 1000 -g 1000 /home/analyst/.config /home/analyst/.config/chromium \
      /home/analyst/Downloads /home/analyst/.pki /home/analyst/.pki/nssdb \
      /home/analyst/work /home/analyst/.local /home/analyst/.local/share \
      /home/analyst/.local/share/appliance /data/lake /data/pyuser \
 && mkdir -p /etc/chromium/policies/managed /etc/chromium/policies/recommended \
 && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

# Static managed policy (root-owned). The homepage policy file is rendered at
# start from BROWSER_HOME_URL, so that single file is owned by the runtime user.
COPY policies/appliance.json /etc/chromium/policies/managed/appliance.json
RUN touch /etc/chromium/policies/managed/zz-homepage.json \
 && chown 1000:1000 /etc/chromium/policies/managed/zz-homepage.json \
 && chmod 0644 /etc/chromium/policies/managed/*.json

COPY novnc/index.html /usr/share/novnc/index.html
# XFCE system defaults (xfconf channels: window manager, panel, desktop,
# shortcuts, look, terminal, Thunar); a user's own changes go to ~/.config/xfce4.
COPY xfce/xfconf/ /etc/xdg/xfce4/xfconf/xfce-perchannel-xml/
COPY xfce/xfconf/xfce4-panel.xml /etc/xdg/xfce4/panel/default.xml
COPY xfce/helpers.rc /etc/xdg/xfce4/helpers.rc
COPY xfce/helpers/ /usr/share/xfce4/helpers/
COPY xfce/make-xfwm4-theme.sh /tmp/make-xfwm4-theme.sh
COPY gtk-settings.ini /etc/gtk-3.0/settings.ini
COPY profile-python.sh /etc/profile.d/zz-appliance-python.sh
COPY seed/ /usr/share/appliance/seed/
COPY applications/ /usr/share/applications/
COPY entrypoint.sh /usr/local/bin/browser-entrypoint
COPY supervisord.conf /etc/appliance/supervisord.conf
COPY xfce-run.sh /usr/local/bin/appliance-xfce
COPY wait-x.sh /usr/local/bin/appliance-wait-x
COPY chromium.sh /usr/local/bin/appliance-chromium
COPY cdp_proxy.py /usr/local/lib/appliance/cdp_proxy.py
COPY desktop-setup.sh /usr/local/bin/appliance-desktop-setup
COPY healthcheck.sh /usr/local/bin/browser-healthcheck
COPY restore-windows.sh /usr/local/bin/appliance-restore-windows
COPY browser-window.sh /usr/local/bin/appliance-browser-window
COPY files.sh /usr/local/bin/appliance-files
COPY dbeaver.sh /usr/local/bin/appliance-dbeaver
COPY claude.sh /usr/local/bin/appliance-claude
COPY vscode.sh /usr/local/bin/appliance-vscode
COPY antigravity.sh /usr/local/bin/appliance-antigravity
COPY maximise.sh /usr/local/bin/appliance-maximise
COPY icons/ /usr/share/appliance/icons/
# Drop launchers (panel, menu favourites, shortcuts, .desktop) for apps not in
# this build. Hide menu entries analysts should not need (raw Chromium / VS
# Code would bypass the appliance launchers; display settings would fight
# Xvfb); Settings -> Appearance / Window Manager / Keyboard stay available.
# Menu categories get Adwaita icons that exist (its full-colour category
# icons are gone; elementary-xfce would add ~68 MB).
ARG XFWM4_BORDER=6
RUN chmod 0755 /usr/local/bin/browser-entrypoint /usr/local/bin/browser-healthcheck \
      /usr/local/bin/appliance-* \
 && chmod -R a+rX /etc/appliance /usr/local/lib/appliance \
 && chmod 0644 /etc/profile.d/zz-appliance-python.sh \
 && bash /tmp/make-xfwm4-theme.sh "$XFWM4_BORDER" && rm -f /tmp/make-xfwm4-theme.sh \
 && update-alternatives --set x-terminal-emulator /usr/bin/xfce4-terminal.wrapper \
 && x=/etc/xdg/xfce4/xfconf/xfce-perchannel-xml \
 && drop() { rm -f "/usr/local/bin/appliance-$1" "/usr/share/applications/appliance-$1.desktop"; \
             sed -i "/appliance-$1/d" /etc/xdg/xfce4/panel/default.xml \
               "$x/xfce4-panel.xml" "$x/xfce4-keyboard-shortcuts.xml"; } \
 && { [ -x /opt/dbeaver/dbeaver ] || drop dbeaver; } \
 && { [ -f /opt/appliance-features/antigravity ] || drop antigravity; } \
 && { [ -f /usr/share/appliance/icons/antigravity.png ] || sed -i 's#^Icon=.*#Icon=applications-development#' /usr/share/applications/appliance-antigravity.desktop 2>/dev/null || true; } \
 && { command -v claude >/dev/null || drop claude; } \
 && { [ -f /opt/dbeaver/dbeaver.png ] && sed -i 's#^Icon=.*#Icon=/opt/dbeaver/dbeaver.png#' \
        /usr/share/applications/appliance-dbeaver.desktop || true; } \
 && for a in chromium com.microsoft.VSCode thunar thunar-bulk-rename xfce4-terminal \
      xfce4-terminal-emulator xfce4-file-manager xfce4-web-browser xfce4-mail-reader \
      xfce-display-settings xfce4-accessibility-settings xfce4-color-settings \
      xfce-workspaces-settings xfce4-mime-settings xfce4-settings-editor \
      python3.13 x11vnc jupyterlab; do \
      for f in "/usr/share/applications/$a.desktop" "/usr/local/share/applications/$a.desktop"; do \
      [ -f "$f" ] || continue; \
      sed -i '/^NoDisplay=/d; 0,/^\[Desktop Entry\]/s//[Desktop Entry]\nNoDisplay=true/' "$f"; \
    done; done \
 && sed -i 's#^Exec=chromium\b.*#Exec=appliance-browser-window %U#' /usr/share/applications/chromium.desktop \
 && sed -i -e 's/^Icon=applications-accessories$/Icon=applications-utilities-symbolic/' \
      -e 's/^Icon=applications-development$/Icon=applications-engineering-symbolic/' \
      -e 's/^Icon=applications-internet$/Icon=network-workgroup-symbolic/' \
      -e 's/^Icon=applications-office$/Icon=x-office-document-symbolic/' \
      -e 's/^Icon=applications-other$/Icon=preferences-other-symbolic/' \
      -e 's/^Icon=preferences-desktop.*$/Icon=preferences-system-symbolic/' \
      -e 's/^Icon=applications-system$/Icon=applications-system-symbolic/' \
      /usr/share/desktop-directories/xfce-*.directory \
 && update-desktop-database -q /usr/share/applications

# PATH keeps the workspace image's order (/data/pyuser/bin, /usr/local/bin
# before /usr/bin), so `python`/`python3` is the analyst interpreter, never
# Debian's /usr/bin/python3. PYTHONUSERBASE/PYTHONPATH are inherited from the
# workspace image.
ENV HOME=/home/analyst \
    PATH=/data/pyuser/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONUSERBASE=/data/pyuser \
    DISPLAY=:99 \
    BROWSER_GEOMETRY=1920x1080x24 \
    BROWSER_HOME_URL=about:blank \
    NOVNC_PORT=6080 \
    CDP_INTERNAL_PORT=9222 \
    CDP_PUBLIC_PORT=9223 \
    DESKTOP_STATE_DIR=/home/analyst/.local/share/appliance

# Sanity: one Python everywhere, desktop tools present.
# (CLAUDE_CONFIG_DIR / DISABLE_AUTOUPDATER for Claude Code come from the base.)
RUN test "$(command -v python)" = /usr/local/bin/python \
 && test "$(command -v python3)" = /usr/local/bin/python3 \
 && test "$(bash -lc 'command -v python3')" = /usr/local/bin/python3 \
 && python -c "import pandas, duckdb, ipykernel, playwright" \
 && command -v chromium Xvfb x11vnc websockify socat xfwm4 xfce4-panel xfdesktop xfsettingsd \
      xfce4-terminal thunar wmctrl xset certutil supervisord supervisorctl >/dev/null \
 && test -f /usr/share/themes/Appliance/xfwm4/themerc \
 && ! command -v xfce4-session xfce4-screensaver light-locker xflock4 xfce4-power-manager xscreensaver >/dev/null \
 && test -f /usr/share/novnc/vnc.html \
 && test -z "$(find /home/analyst -xdev ! -uid 1000 -print -quit)"

USER 1000:1000
WORKDIR /home/analyst
EXPOSE 6080 6081 9223

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["/usr/local/bin/browser-healthcheck"]

# Replaces the workspace image's entrypoint (and clears its CMD ["start"]).
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/browser-entrypoint"]
CMD []
