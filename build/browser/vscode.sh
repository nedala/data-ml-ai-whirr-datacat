#!/usr/bin/env bash
# VS Code on the desktop = the appliance's code-server (same workspace,
# extensions and settings as /code/), opened in a Chromium app window.
# Its password is the VS Code credential in Studio → Credentials.
set -u
exec appliance-browser-window --app="http://code:8080/?folder=/home/analyst/work" "$@"
