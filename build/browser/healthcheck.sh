#!/bin/sh
# Every supervised program RUNNING, Chromium's DevTools answering, noVNC served.
supervisorctl -c /tmp/desktop/supervisord.conf status 2>/dev/null \
  | awk '$2 != "RUNNING" && !($1 ~ /^(x11vnc-pw|novnc-direct)$/ && $2 == "STOPPED") {bad=1; print} END {exit bad}' \
 && curl -sf -o /dev/null --max-time 3 "http://127.0.0.1:9222/json/version" \
 && curl -sf -o /dev/null --max-time 3 "http://127.0.0.1:9223/json/version" \
 && curl -sf -o /dev/null --max-time 3 "http://127.0.0.1:6080/vnc.html"
