#!/bin/zsh
set -eu
cd "$(dirname "$0")"
bundle="$PWD/.mochi-native/Mochi.app"
if [[ -x "$bundle/Contents/MacOS/Mochi" ]]; then
  if (( $# )); then
    exec "$bundle/Contents/MacOS/Mochi" "$@"
  fi
  exec open "$bundle"
fi
if [[ ! -x .venv/bin/python ]]; then
  print "First run: uv sync --python 3.12 --locked"
  exit 1
fi
exec .venv/bin/python -m deskpet "$@"
