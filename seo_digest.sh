#!/bin/bash
# Cron wrapper: run the deterministic Weekly Digest with the Hermes venv Python
# (needs yaml + googleapiclient). Registered as a --no-agent script cron.
#
# Replaces the old LLM-agent digest job, which on Haiku flailed (hallucinated an
# xlsx, ran a /tmp script with the wrong Python) and still reported "ok". The
# engine writes the whole stats table deterministically and adds a one-shot Opus
# narrative; if the API is down the table still lands. No agent, no false green.
set -euo pipefail
PY="$HOME/.hermes/hermes-agent/venv/bin/python"
LOG="$HOME/.hermes/logs/seo_boss_tick.log"
mkdir -p "$(dirname "$LOG")"
echo "[$(date '+%F %T')] weekly digest: start" >>"$LOG"
if "$PY" "$HOME/.hermes/scripts/seo_boss.py" digest >>"$LOG" 2>&1; then
  echo "[$(date '+%F %T')] weekly digest: ok" >>"$LOG"
else
  echo "[$(date '+%F %T')] weekly digest: FAILED (see log)" >>"$LOG"
  exit 1
fi
