#!/bin/zsh
set -eu
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  print "请先运行：uv sync --python 3.12 --extra dev --locked"
  read -r "?按回车关闭…"
  exit 1
fi
exec .venv/bin/python -m deskpet.m0 gui --profile realtime \
  --adapters examples/m0/c-desktop.adapters.json --output "output/m0/c-desktop-$(date +%Y%m%d-%H%M%S)-$$"
