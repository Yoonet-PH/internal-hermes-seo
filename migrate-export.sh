#!/usr/bin/env bash
#
# migrate-export.sh — package the UNIQUE state of this Hermes install into a
# single portable archive, for moving to another machine.
#
# Non-destructive: only reads ~/.hermes, never modifies it. Safe to run while
# the gateway is up and while this machine remains a standby fallback.
#
# What it carries (the ~40-55MB that is genuinely yours):
#   creds        .env, auth.json, config.yaml, google_token.json,
#                google_client_secret.json, channel_directory.json
#   personas     SOUL.md, profiles/*/SOUL.md, profiles/*/profile.yaml
#   schedule     cron/jobs.json (SEO Boss + Weekly Digest + Tech Sweep)
#   skills       skills/ (incl. the load-bearing google-workspace google_api.py
#                and the customised seo blueprint)
#   memory       memories/ (MEMORY.md, USER.md)
#   history      state.db + kanban.db  (safe sqlite snapshots; --no-history skips)
#
# What it DROPS (regenerable on the far side by re-running the installer):
#   hermes-agent/ (2.3G clone + venv), node/, all *_cache, logs/, sandboxes/,
#   bin/, the launchd plist, scripts/ (comes via `git clone` of hermes-seo-boss),
#   per-profile skills/ + sessions/ copies, runtime *.lock / *.pid / *-wal.
#
# Usage:
#   ./migrate-export.sh                 # plaintext .tgz, perms 600
#   ./migrate-export.sh --encrypt       # AES-256 encrypt (prompts passphrase)
#   ./migrate-export.sh --no-history    # config + SEO Boss only, no state.db
#   HERMES_HOME=/path ./migrate-export.sh
#
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
OUT_DIR="${OUT_DIR:-$HOME/hermes-migration}"
ENCRYPT=0
HISTORY=1

for arg in "$@"; do
  case "$arg" in
    --encrypt)    ENCRYPT=1 ;;
    --no-history) HISTORY=0 ;;
    -h|--help)    grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

[ -d "$HERMES_HOME" ] || { echo "no Hermes install at $HERMES_HOME" >&2; exit 1; }

STAMP="$(date +%Y%m%d-%H%M%S)"
STAGE="$(mktemp -d)"
ROOT="$STAGE/hermes"
mkdir -p "$ROOT"
trap 'rm -rf "$STAGE"' EXIT

say() { printf '  %s\n' "$*"; }
copy() { # copy if exists, preserving relative path under ROOT
  local src="$HERMES_HOME/$1"
  [ -e "$src" ] || { say "skip (absent): $1"; return; }
  mkdir -p "$ROOT/$(dirname "$1")"
  cp -p "$src" "$ROOT/$1"
  say "+ $1"
}

echo "Staging unique Hermes state from $HERMES_HOME"

# --- creds & top-level config ---
for f in .env auth.json config.yaml google_token.json google_client_secret.json \
         channel_directory.json SOUL.md; do
  copy "$f"
done

# --- schedule ---
copy "cron/jobs.json"

# --- memory ---
if [ -d "$HERMES_HOME/memories" ]; then
  mkdir -p "$ROOT/memories"
  # carry the markdown, not the .lock files
  find "$HERMES_HOME/memories" -maxdepth 1 -name '*.md' -exec cp -p {} "$ROOT/memories/" \;
  say "+ memories/*.md"
fi

# --- profiles: SOUL + profile.yaml + structure, MINUS the stock skills/ & sessions/ copies ---
if [ -d "$HERMES_HOME/profiles" ]; then
  rsync -a --exclude='skills/' --exclude='sessions/' --exclude='logs/' \
        "$HERMES_HOME/profiles/" "$ROOT/profiles/"
  say "+ profiles/ (minus stock skills/sessions/logs)"
fi

# --- skills: whole tree (load-bearing google_api.py + customised seo) ---
if [ -d "$HERMES_HOME/skills" ]; then
  rsync -a --exclude='__pycache__/' "$HERMES_HOME/skills/" "$ROOT/skills/"
  say "+ skills/"
fi

# --- history: safe live snapshots via sqlite .backup (consistent even mid-write) ---
if [ "$HISTORY" -eq 1 ]; then
  for db in state.db kanban.db; do
    if [ -f "$HERMES_HOME/$db" ]; then
      sqlite3 "$HERMES_HOME/$db" ".backup '$ROOT/$db'" \
        && say "+ $db (sqlite snapshot)" \
        || say "! $db snapshot failed (skipped)"
    fi
  done
else
  say "history skipped (--no-history)"
fi

# --- portability scan: warn on absolute /Users/<name> paths that may break ---
echo
echo "Portability check (absolute home paths in carried text configs):"
HITS=0
while IFS= read -r -d '' f; do
  n=$(grep -c "/Users/" "$f" 2>/dev/null || true)
  if [ "${n:-0}" -gt 0 ]; then
    rel="${f#$ROOT/}"
    say "$n hit(s): $rel"
    HITS=$((HITS+1))
  fi
done < <(find "$ROOT" \( -name '*.yaml' -o -name '*.json' -o -name '*.txt' -o -name '*.md' -o -name '*.sh' \) -print0)
[ "$HITS" -eq 0 ] && say "none — fully portable" \
  || say "→ if the new Mac's username differs from '$USER', sed-rewrite these on restore."

# --- pack ---
mkdir -p "$OUT_DIR"
ARCHIVE="$OUT_DIR/hermes-migration-$STAMP.tgz"
tar -czf "$ARCHIVE" -C "$STAGE" hermes
chmod 600 "$ARCHIVE"

if [ "$ENCRYPT" -eq 1 ]; then
  echo
  echo "Encrypting (AES-256, passphrase) — keep the passphrase safe, there is no recovery:"
  openssl enc -aes-256-cbc -pbkdf2 -salt -in "$ARCHIVE" -out "$ARCHIVE.enc"
  rm -f "$ARCHIVE"
  ARCHIVE="$ARCHIVE.enc"
  chmod 600 "$ARCHIVE"
fi

SIZE="$(du -h "$ARCHIVE" | cut -f1)"
SHA="$(shasum -a 256 "$ARCHIVE" | cut -d' ' -f1)"

echo
echo "Bundle ready:"
echo "  file   $ARCHIVE"
echo "  size   $SIZE"
echo "  sha256 $SHA"
echo
echo "This file contains LIVE secrets (Telegram token, model creds, SE Ranking key,"
echo "Google OAuth). Move it by AirDrop or USB, not cloud sync. Delete it from both"
echo "machines once the new install is verified. Restore steps: see MIGRATION.md."
