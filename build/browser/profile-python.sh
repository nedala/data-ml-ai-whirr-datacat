# Analyst desktop: one Python everywhere (sourced by login shells after
# /etc/profile, which resets PATH). Keep the user's package bin dir and the
# workspace interpreter (/usr/local/bin) ahead of Debian's /usr/bin/python3.
export PYTHONUSERBASE="${PYTHONUSERBASE:-/data/pyuser}"
case ":$PATH:" in
  *":$PYTHONUSERBASE/bin:/usr/local/bin:"*) ;;
  *) PATH="$PYTHONUSERBASE/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
     export PATH ;;
esac
