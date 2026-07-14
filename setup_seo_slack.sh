#!/usr/bin/env bash
#
# setup_seo_slack.sh — securely wire the SEO Boss's Slack bot token into Hermes.
#
# Prompts for the bot token via a native macOS dialog (hidden field), verifies it
# against Slack's auth.test before writing anything, and stores it in
# ~/.hermes/.env. The token is never printed, never echoed, and never reaches a
# terminal transcript.
#
# It is written as SEO_SLACK_BOT_TOKEN, NOT SLACK_BOT_TOKEN, on purpose: the
# Hermes gateway watches SLACK_BOT_TOKEN and would try to bring the *interactive*
# Slack adapter up on every restart. Under its own name the SEO push layer can
# speak and cannot listen — which is exactly what Phase 1 wants.
#
set -euo pipefail
ENV="$HOME/.hermes/.env"
PY="$HOME/.hermes/hermes-agent/venv/bin/python"

TOKEN="$(osascript \
  -e 'display dialog "Paste the Slack BOT TOKEN (starts with xoxb-) from api.slack.com → OAuth & Permissions:" default answer "" with hidden answer with title "Hermes SEO — Slack setup"' \
  -e 'text returned of result' 2>/dev/null)" \
  || { echo "Cancelled — nothing written."; exit 1; }

TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
[[ "$TOKEN" == xoxb-* ]] \
  || { echo "That does not look like a bot token (expected xoxb-...). Nothing written."; exit 1; }

# Verify BEFORE writing, so a bad paste never lands in .env.
WHO="$(curl -sS -X POST https://slack.com/api/auth.test \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-type: application/x-www-form-urlencoded" \
     | "$PY" -c 'import json,sys; r=json.load(sys.stdin); print(("OK "+r.get("team","?")+" as "+r.get("user","?")) if r.get("ok") else "FAIL "+str(r.get("error")))')"

case "$WHO" in
  OK*) ;;
  *) echo "Slack rejected that token ($WHO). Nothing written."; exit 1 ;;
esac

# remove any existing active/commented line, then append
grep -vE '^#?[[:space:]]*SEO_SLACK_BOT_TOKEN=' "$ENV" > "$ENV.tmp" && mv "$ENV.tmp" "$ENV"
printf 'SEO_SLACK_BOT_TOKEN=%s\n' "$TOKEN" >> "$ENV"
chmod 600 "$ENV"

echo "Verified and saved: $WHO"
echo
echo "Next:"
echo "  1. Invite the bot to each site channel in Slack:  /invite @<bot name>"
echo "  2. Fill the 'Slack Channel' column in Monitored Sites (e.g. #seo-a1-electrical)"
echo "  3. Prove one channel:   python seo_boss.py slack-test '#seo-a1-electrical'"
echo "  4. Introduce the backlog: python seo_boss.py slack-backlog 'A1 Electrical'"
