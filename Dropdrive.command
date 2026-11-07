#!/bin/bash
# Double-click this file in Finder to run Dropdrive.
cd "$(dirname "$0")" || exit 1
clear

PY=""
for p in "$(command -v python3 2>/dev/null)" /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
  [ -n "$p" ] && [ -x "$p" ] || continue
  # /usr/bin/python3 is only a placeholder until Apple's developer tools are installed.
  if [ "$p" = "/usr/bin/python3" ] && ! xcode-select -p >/dev/null 2>&1; then continue; fi
  if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
    PY="$p"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "Dropdrive needs Apple's free Command Line Tools, which include Python."
  echo
  echo "A window will pop up asking to install them. Click Install, wait for it to"
  echo "finish (a few minutes), then double-click Dropdrive again."
  xcode-select --install >/dev/null 2>&1
  echo
  read -r -p "Press Enter to close this window."
  exit 1
fi

"$PY" dropdrive.py "$@"
echo
read -r -p "Press Enter to close this window."
