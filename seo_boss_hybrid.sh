#!/usr/bin/env bash
#
# seo_boss_hybrid.sh — cron entrypoint for LIVE HYBRID mode.
#
# The no-agent cron runner invokes a script by bare path with no arguments
# (cron/scheduler.py: argv = [bash, script_path]), so it cannot pass `--hybrid`
# to seo_boss.sh directly. This thin wrapper supplies it.
#
# Hybrid mode: a due AUDIT is produced deterministically — local Gemma writes the
# on-page rewrite tasks, Claude (Opus 4.8) drafts the client email — with no LLM
# agent. VERIFY/CHASE/EMAIL/NONE behave exactly as in seo_boss.sh.
#
# Reversible: to disable hybrid, point the cron back at seo_boss.sh.
exec "$HOME/.hermes/scripts/seo_boss.sh" --hybrid
