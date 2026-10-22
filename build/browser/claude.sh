#!/usr/bin/env bash
# Claude Code (CLI) in a terminal on the analyst's work folder. Settings and
# sign-in live in $CLAUDE_CONFIG_DIR (set by the workspace image). The shell
# stays open when claude exits.
cd "$HOME/work" 2>/dev/null || cd "$HOME"
exec xfce4-terminal --title "Claude Code" --geometry 132x40 --working-directory "$PWD" \
  -x bash -lc 'claude "$@"; echo; exec bash -l' claude "$@" >/dev/null 2>&1
