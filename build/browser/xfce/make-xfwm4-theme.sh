#!/usr/bin/env bash
# Build-time only: the "Appliance" xfwm4 theme = Greybird-accessibility (34 px
# title bar, Greybird look) with wider side/bottom borders, so windows are easy
# to grab and resize through noVNC (Greybird's own borders are 1-3 px).
set -euo pipefail
B="${1:-6}"                                   # border width in px
src=/usr/share/themes/Greybird-accessibility/xfwm4
dst=/usr/share/themes/Appliance/xfwm4
mkdir -p "$dst"
cp -a "$src/." "$dst/"

rep() { local s; printf -v s '%*s' "$2" ''; printf '%s' "${s// /$1}"; }   # rep <char> <n>
xpm() {  # xpm <file> <width> <height> <row>...   ('.' = frame colour, '+' = edge line)
  local f=$1 w=$2 h=$3 i=0; shift 3
  {
    printf '/* XPM */\nstatic char * border_xpm[] = {\n"%s %s 2 1",\n' "$w" "$h"
    printf '".\tc #DEDEDE s active_color_2",\n"+\tc #939393",\n'
    for r in "$@"; do i=$((i + 1)); [ "$i" -lt $# ] && printf '"%s",\n' "$r" || printf '"%s"};\n' "$r"; done
  } > "$dst/$f"
}
fill=$(rep . $((B - 1)))
rows_b=(); rows_bl=(); rows_br=()
for _ in $(seq 2 "$B"); do rows_b+=("."); rows_bl+=("+$fill"); rows_br+=("$fill+"); done
for s in active inactive; do
  xpm "left-$s.xpm"         "$B" 1    "+$fill"
  xpm "right-$s.xpm"        "$B" 1    "$fill+"
  xpm "bottom-$s.xpm"       1    "$B" "${rows_b[@]}" "+"
  xpm "bottom-left-$s.xpm"  "$B" "$B" "${rows_bl[@]}" "$(rep + "$B")"
  xpm "bottom-right-$s.xpm" "$B" "$B" "${rows_br[@]}" "$(rep + "$B")"
done
# readable inactive titles (Greybird's #bbbbbb on light grey is faint over VNC)
sed -i -e 's/^inactive_text_color=.*/inactive_text_color=#8c8c8c/' \
       -e '1i # Appliance: Greybird-accessibility with '"$B"' px borders (built by make-xfwm4-theme.sh)' "$dst/themerc"
