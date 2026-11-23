#!/bin/zsh
set -eu
cd "$(dirname "$0")/.."
exec .venv/bin/python -m deskpet.m0 gui --profile scenario \
  --adapters examples/m0/cd-product.adapters.json --output "output/m0/c-simulation-$(date +%Y%m%d-%H%M%S)-$$"
