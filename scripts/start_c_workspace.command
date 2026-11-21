#!/bin/zsh
# macOS: open the shared C workspace and its desktop pet entry.
set -eu
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  print "请先在仓库目录运行：uv sync --python 3.12 --extra dev --locked"
  read -r "?按回车关闭…"
  exit 1
fi
exec .venv/bin/python -m deskpet.m0 gui --profile scenario \
  --adapters examples/m0/c-desktop.adapters.json --output output/m0/c-desktop
