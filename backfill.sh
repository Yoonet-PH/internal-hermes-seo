#!/bin/bash
# One-time backfill: audit every monitored site that has never been audited.
# Runs one fresh SEO Boss tick per site (fresh context = sharp per-client tasks),
# looping until no site is due for audit. Capped to avoid runaway cost.
PROMPT="$(cat ~/.hermes/scripts/boss_prompt.txt)"
PYV=~/.hermes/hermes-agent/venv/bin/python
LOG=~/.hermes/logs/backfill.log
: > "$LOG"
for i in $(seq 1 12); do
  NA=$("$PYV" ~/.hermes/scripts/seo_boss.py 2>/dev/null | grep -m1 '^NEXT_ACTION:')
  SITE=$("$PYV" ~/.hermes/scripts/seo_boss.py 2>/dev/null | grep -m1 '^SITE:' | cut -d'|' -f1)
  echo "[$(date +%T)] iter $i: ${NA} ${SITE}"
  echo "==== [$(date +%T)] iter $i: ${NA} ${SITE} ====" >> "$LOG"
  case "$NA" in
    *AUDIT*)
      hermes -z "$PROMPT" -m anthropic/claude-sonnet-4.6 --provider nous --yolo \
        -t terminal,file,web >> "$LOG" 2>&1
      ;;
    *)
      echo "[$(date +%T)] no more audits due — backfill complete after $((i-1)) sites"
      break
      ;;
  esac
done
echo "[$(date +%T)] DONE"
