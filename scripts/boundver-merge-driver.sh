#!/usr/bin/env bash
set -euo pipefail

# Git merge driver interface:
#   %O = ancestor file, %A = current file (must write result), %B = other file
# We intentionally ignore file inputs and regenerate canonical lockfile from config.

LOCKFILE_PATH="${1:-boundary.lock.json}"
CONFIG_PATH="${BOUNDVER_CONFIG:-boundary.config.json}"
SOURCE_MODE="${BOUNDVER_SOURCE:-head}"

boundver generate \
  --config "$CONFIG_PATH" \
  --out "$LOCKFILE_PATH" \
  --source "$SOURCE_MODE" \


exit 0
