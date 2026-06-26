#!/usr/bin/env bash
#
# update_dataforseo.sh — point Hermes at a different DataForSEO account.
#
# Captures the API login + password via secure macOS dialogs (password hidden,
# never echoed), writes them to ~/.hermes/.env, and verifies them against the
# DataForSEO account endpoint — which costs nothing, so no paid lookup is spent.
#
set -euo pipefail
ENV="$HOME/.hermes/.env"
PY="$HOME/.hermes/hermes-agent/venv/bin/python"

ask() { local hidden=""; [ "${2:-}" = hidden ] && hidden="with hidden answer";
  osascript -e "display dialog \"$1\" default answer \"\" $hidden with title \"DataForSEO (ai@)\"" \
            -e 'text returned of result' 2>/dev/null; }

LOGIN="$(ask 'DataForSEO API login (the account email, from the DataForSEO dashboard):')" \
  || { echo "Cancelled — nothing written."; exit 1; }
PASS="$(ask 'DataForSEO API password:' hidden)" \
  || { echo "Cancelled — nothing written."; exit 1; }
LOGIN="$(printf '%s' "$LOGIN" | tr -d '[:space:]')"
PASS="$(printf '%s'  "$PASS"  | tr -d '[:space:]')"
[ -n "$LOGIN" ] && [ -n "$PASS" ] || { echo "Login or password was empty — nothing written."; exit 1; }

# verify FIRST (cost-free account endpoint) before touching the .env
echo "Verifying against DataForSEO..."
if ! DFS_LOGIN="$LOGIN" DFS_PASS="$PASS" "$PY" - <<'PY'
import os, sys, base64, json, urllib.request
auth = base64.b64encode(f"{os.environ['DFS_LOGIN']}:{os.environ['DFS_PASS']}".encode()).decode()
req = urllib.request.Request("https://api.dataforseo.com/v3/appendix/user_data",
                             headers={"Authorization": "Basic " + auth})
try:
    d = json.load(urllib.request.urlopen(req, timeout=30))
except Exception as e:
    print("  request failed:", str(e)[:160]); sys.exit(1)
if d.get("status_code") == 20000:
    u = d["tasks"][0]["result"][0]
    m = u.get("money", {})
    print(f"  OK — account: {u.get('login')} | balance: {m.get('balance')} {m.get('currency','')}")
    sys.exit(0)
print("  AUTH FAILED:", d.get("status_message")); sys.exit(1)
PY
then
  echo "Credentials did not verify — leaving ~/.hermes/.env unchanged."
  exit 1
fi

upsert() { grep -vE "^#?[[:space:]]*$1=" "$ENV" > "$ENV.tmp" && mv "$ENV.tmp" "$ENV"; printf '%s=%s\n' "$1" "$2" >> "$ENV"; }
upsert DATAFORSEO_LOGIN "$LOGIN"
upsert DATAFORSEO_PASSWORD "$PASS"
chmod 600 "$ENV"
echo "Updated DATAFORSEO_LOGIN=$LOGIN and DATAFORSEO_PASSWORD (hidden) in ~/.hermes/.env"
echo "Done. The SEO scripts read these from the file, so it takes effect on the next run."
