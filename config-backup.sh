#!/usr/bin/env bash
#
# config-backup.sh — weekly encrypted snapshot of this Hermes install's unique
# state to an offsite location (a folder in ai@'s Google Drive).
#
# Closes the gap that bens-playpen/hermes-seo-boss backs up only the *scripts* —
# the creds, config.yaml, profiles/personas, skills, memories and history have no
# cloud copy. This reuses migrate-export.sh to build the bundle, encrypts it with
# a local key, drops it offsite, and prunes to the last few weeks.
#
# Runs unattended as a Hermes cron (--no-agent). Non-destructive.
#
# KEY MODEL (important): the bundle is encrypted with ~/.hermes/.config-backup-key,
# which lives ONLY on this Mac. If the Mac dies, the offsite blobs are useless
# without that key — so the key is ALSO printed once at setup for you to store in
# your password manager. Off-machine key + offsite blob = real recovery.
#
# Decrypt later:
#   openssl enc -d -aes-256-cbc -pbkdf2 -pass file:KEYFILE \
#     -in hermes-config-<stamp>.tgz.enc -out hermes-config.tgz
#   tar xzf hermes-config.tgz   # -> hermes/...  (restore per MIGRATION.md)
#
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SCRIPTS="$HERMES_HOME/scripts"
PY="$HERMES_HOME/hermes-agent/venv/bin/python"
KEYFILE="${HERMES_BACKUP_KEY:-$HERMES_HOME/.config-backup-key}"
# Offsite destination is a folder in ai@'s Google Drive, uploaded via the Hermes
# service account (drive_backup.py). Override the folder id with HERMES_BACKUP_DRIVE_FOLDER.
DRIVE_FOLDER="${HERMES_BACKUP_DRIVE_FOLDER:-1eS8nOimWk7k76E112_tgxwzFdkKwpsLL}"
KEEP="${HERMES_BACKUP_KEEP:-8}"
LOG="$HERMES_HOME/logs/config-backup.log"

mkdir -p "$(dirname "$LOG")"
ts() { date '+%Y-%m-%d %H:%M:%S'; }
fail() { echo "[$(ts)] config-backup FAILED: $*" | tee -a "$LOG" >&2; exit 1; }

[ -f "$SCRIPTS/migrate-export.sh" ] || fail "migrate-export.sh not found in $SCRIPTS"
[ -f "$KEYFILE" ] || fail "no encryption key at $KEYFILE — run the one-time setup (see config-backup-setup)"

STAMP="$(date +%Y%m%d-%H%M%S)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 1. build the plaintext bundle (full state incl. sqlite history) into a temp dir
OUT_DIR="$TMP" bash "$SCRIPTS/migrate-export.sh" >>"$LOG" 2>&1 \
  || fail "migrate-export.sh errored (see $LOG)"
PLAIN="$(ls -t "$TMP"/hermes-migration-*.tgz 2>/dev/null | head -1)"
[ -n "$PLAIN" ] && [ -f "$PLAIN" ] || fail "bundle was not produced"

# 2. encrypt with the local key (non-interactive)
ENC="$TMP/hermes-config-$STAMP.tgz.enc"
openssl enc -aes-256-cbc -pbkdf2 -salt -pass file:"$KEYFILE" -in "$PLAIN" -out "$ENC" \
  || fail "openssl encryption failed"
rm -f "$PLAIN"

# 3. land it offsite — upload to ai@'s Google Drive (and prune to last $KEEP there)
SIZE="$(du -h "$ENC" | cut -f1)"
SHA="$(shasum -a 256 "$ENC" | cut -c1-12)"
"$PY" "$SCRIPTS/drive_backup.py" "$ENC" "$DRIVE_FOLDER" "$KEEP" >>"$LOG" 2>&1 \
  || fail "Drive upload failed (see $LOG)"

echo "[$(ts)] config backup ok: hermes-config-$STAMP.tgz.enc ($SIZE, sha $SHA) -> ai@ Drive/Hermes-Backups" >>"$LOG"

# concise stdout (delivered by the --no-agent cron; one line/week)
echo "Hermes config backed up to ai@ Drive (Hermes-Backups): hermes-config-$STAMP.tgz.enc ($SIZE)  [kept last $KEEP]"
