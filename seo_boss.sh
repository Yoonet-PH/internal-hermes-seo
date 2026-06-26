#!/usr/bin/env bash
#
# seo_boss.sh — the cost-gated boss tick (runs as a --no-agent Hermes cron).
#
# Runs the deterministic engine (seo_boss.py) first. Only when there is real
# work (NEXT_ACTION != NONE) does it spend an LLM run, invoking the agent
# headlessly with the situation report embedded in the prompt so the agent
# never re-runs the engine. Most ticks are NONE and now cost zero tokens.
#
# The model is pinned here so the boss is immune to changes in the global
# model.default (the desktop app rewrites that). Override via SEO_BOSS_MODEL.
#
set -euo pipefail

PY="$HOME/.hermes/hermes-agent/venv/bin/python"
HERMES="$HOME/.hermes/hermes-agent/venv/bin/hermes"
SCRIPTS="$HOME/.hermes/scripts"
MODEL="${SEO_BOSS_MODEL:-claude-sonnet-4-6}"
LOG="$HOME/.hermes/logs/seo_boss_tick.log"

# Hybrid mode: AUDIT is produced deterministically (local Gemma rewrite tasks +
# Claude Opus 4.8 client email) with no LLM agent. Enable with `--hybrid` or
# SEO_BOSS_HYBRID=1. Other actions still go to the agent.
HYBRID=0
case " $* " in *" --hybrid "*) HYBRID=1 ;; esac
[ "${SEO_BOSS_HYBRID:-0}" = "1" ] && HYBRID=1

mkdir -p "$(dirname "$LOG")"

REPORT="$("$PY" "$SCRIPTS/seo_boss.py" 2>>"$LOG")" || {
  echo "SEO Boss tick FAILED: seo_boss.py errored (see ~/.hermes/logs/seo_boss_tick.log)"
  exit 1
}
ACTION="$(printf '%s\n' "$REPORT" | sed -n 's/^NEXT_ACTION: //p' | head -1)"
SITE="$(printf '%s\n' "$REPORT" | sed -n 's/^SITE: \([^|]*\).*/\1/p' | head -1)"
echo "[$(date '+%F %T')] tick: ${ACTION:-unparseable}${SITE:+ — $SITE}" >>"$LOG"

if [ -z "$ACTION" ]; then
  echo "SEO Boss tick FAILED: report had no NEXT_ACTION line (see log)"
  exit 1
fi
if [ "$ACTION" = "NONE" ]; then
  exit 0   # silent: nothing to do, no agent, no spend
fi

if [ "$HYBRID" = "1" ] && [ "$ACTION" = "AUDIT" ]; then
  if "$PY" "$SCRIPTS/seo_boss.py" hybrid >>"$LOG" 2>&1; then
    echo "SEO Boss (hybrid) audited${SITE:+ — $SITE}"
    exit 0
  fi
  echo "SEO Boss tick FAILED: hybrid audit errored${SITE:+ ($SITE)} (see log)"
  exit 1
fi

PROMPT="$(cat "$SCRIPTS/boss_prompt.txt")

=== SITUATION REPORT (already generated this tick — act on this, do NOT re-run seo_boss.py without arguments) ===
$REPORT"

"$HERMES" -z "$PROMPT" -m "$MODEL" --provider anthropic --yolo -t terminal,file,web \
  >>"$LOG" 2>&1 || {
  echo "SEO Boss tick FAILED: agent run errored on $ACTION${SITE:+ ($SITE)} (see log)"
  exit 1
}
echo "SEO Boss acted: $ACTION${SITE:+ — $SITE}"
