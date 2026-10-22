#!/usr/bin/env bash
# File manager (Thunar) on the analyst's work folder; bookmarks list
# "My work" (/home/analyst/work) and "Shared lake" (/data/lake).
exec thunar "${1:-$HOME/work}" >/dev/null 2>&1
