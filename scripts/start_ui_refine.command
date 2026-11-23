#!/bin/zsh
set -eu
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  print "请先运行：uv sync --python 3.12 --extra dev --locked"
  exit 1
fi
exec .venv/bin/python -m deskpet.m0 gui --profile scenario --scenario ui-refine \
  --adapters examples/m0/ui-refine.adapters.json --output output/m0/ui-refine "$@"
