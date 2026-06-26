#!/usr/bin/env bash
#
# setup_telegram.sh — securely wire a Telegram bot into Hermes.
#
# Prompts for the bot token (hidden) and your numeric Telegram user ID via native
# macOS dialogs, writes them to ~/.hermes/.env without ever printing the token,
# then restarts the gateway and shows the Telegram connection log.
#
set -euo pipefail
ENV="$HOME/.hermes/.env"
HERMES="$HOME/.hermes/hermes-agent/venv/bin/hermes"

ask() { # $1 = prompt, $2 = "hidden" for password field
  local hidden=""
  [ "${2:-}" = "hidden" ] && hidden="with hidden answer"
  osascript -e "display dialog \"$1\" default answer \"\" $hidden with title \"Hermes Telegram setup\"" \
            -e 'text returned of result' 2>/dev/null
}

TOKEN="$(ask 'Step 1 of 2 — paste the BOT TOKEN from @BotFather:' hidden)" \
  || { echo "Cancelled — nothing written."; exit 1; }
USERID="$(ask 'Step 2 of 2 — enter your numeric Telegram USER ID (from @userinfobot):')" \
  || { echo "Cancelled — nothing written."; exit 1; }

TOKEN="$(printf '%s' "$TOKEN" | tr -d '[:space:]')"
USERID="$(printf '%s' "$USERID" | tr -d '[:space:]')"

[[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]] \
  || { echo "That does not look like a bot token (expected 123456789:ABC...). Nothing written."; exit 1; }
[[ "$USERID" =~ ^[0-9]+$ ]] \
  || { echo "The user ID should be all digits. Nothing written."; exit 1; }

upsert() { # remove any existing active/commented line for KEY, then append KEY=VALUE
  local key="$1" val="$2"
  grep -vE "^#?[[:space:]]*${key}=" "$ENV" > "$ENV.tmp" && mv "$ENV.tmp" "$ENV"
  printf '%s=%s\n' "$key" "$val" >> "$ENV"
}

upsert TELEGRAM_BOT_TOKEN "$TOKEN"
upsert TELEGRAM_ALLOWED_USERS "$USERID"
chmod 600 "$ENV"
echo "Saved TELEGRAM_BOT_TOKEN (hidden) and TELEGRAM_ALLOWED_USERS=$USERID to ~/.hermes/.env"

echo "Restarting the gateway to connect Telegram..."
"$HERMES" gateway restart >/dev/null 2>&1 || true
sleep 5
echo "--- recent Telegram log lines ---"
grep -iE "telegram" "$HOME/.hermes/logs/gateway.log" | tail -6 || echo "(no Telegram lines yet — check the full log)"
echo "Done. Open your bot in Telegram and send it 'hello' to test."
