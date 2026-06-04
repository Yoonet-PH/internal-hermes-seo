# Migrating Hermes (the SEO Boss + agent harness) to another Mac

This moves the whole Hermes install to a new Mac while keeping the **old Mac as a
stopped standby fallback**. The heavy 2.3GB `hermes-agent` clone + venv + node are
*not* moved — they regenerate when you reinstall. Only the ~40-55MB of genuinely
unique state travels, and the SEO Boss scripts arrive via `git clone`.

## The two hard rules

1. **One gateway at a time.** Telegram only lets one machine long-poll
   `@Riseofthelobterbot`. Two running gateways = a permanent `409` conflict, and
   two running gateways would also double-run the SEO Boss crons and double-write
   the Google Sheet. So: stop the old gateway **before** starting the new one.
   Because you're keeping the old Mac as a fallback, you switch which machine is
   live by stopping one and starting the other — never both.

2. **Don't trust `.env` to survive the desktop app.** The app owns `.env` and has
   scrubbed the live Telegram token before. After restoring, set Telegram up
   through the app (or re-add the token and `hermes gateway restart`), then verify.
   Do **not** revoke/regenerate the token in BotFather — the old Mac needs the same
   token to work as a fallback.

---

## A. On the OLD Mac (this machine) — export

```bash
~/.hermes/scripts/migrate-export.sh            # plaintext bundle, perms 600
# or, if it'll touch cloud/USB you don't fully trust:
~/.hermes/scripts/migrate-export.sh --encrypt  # AES-256, prompts a passphrase
```

Produces `~/hermes-migration/hermes-migration-<stamp>.tgz` (`.enc` if encrypted)
and prints its size + sha256. The script is read-only — it does not touch the live
install, so this Mac keeps working as-is.

Move the bundle to the new Mac by **AirDrop or USB**, not cloud sync — it holds the
Telegram token, model creds, the SE Ranking key and Google OAuth.

> Leave the old gateway running for now. You only stop it at step C, the moment
> before the new one goes live.

---

## B. On the NEW Mac — fresh install + lay down state

1. **Install Hermes fresh.** Use the same installer you used originally (install
   method on the old Mac is recorded as `git`). This creates `~/.hermes` with a new
   `hermes-agent` clone, venv, node and stock skills/profiles. Don't configure
   anything yet. Confirm the CLI works: `hermes --version`.

2. **Stop the brand-new gateway if the installer auto-started it**, so it isn't
   polling while you swap files in:
   ```bash
   hermes gateway stop 2>/dev/null || true
   launchctl bootout gui/$(id -u)/ai.hermes.gateway 2>/dev/null || true
   ```

3. **Unpack the bundle over the fresh install:**
   ```bash
   cd /tmp
   # if encrypted: openssl enc -d -aes-256-cbc -pbkdf2 -in hermes-migration-<stamp>.tgz.enc -out hm.tgz && tar xzf hm.tgz
   tar xzf ~/Downloads/hermes-migration-<stamp>.tgz       # -> /tmp/hermes/...
   rsync -a /tmp/hermes/ ~/.hermes/                        # overlays creds, profiles, skills, crons, dbs
   rm -rf /tmp/hermes
   ```

4. **Clone the SEO Boss scripts** (not in the bundle — they live in git):
   ```bash
   git clone https://github.com/bens-playpen/hermes-seo-boss.git ~/.hermes/scripts
   ```

5. **Path fix (only if the new Mac's username ≠ `home`).** The export prints any
   absolute `/Users/...` paths it carried (currently just one, in `auth.json`).
   If your new username differs:
   ```bash
   OLD=/Users/home; NEW=$HOME
   sed -i '' "s#$OLD#$NEW#g" ~/.hermes/auth.json ~/.hermes/config.yaml ~/.hermes/cron/jobs.json
   ```

---

## C. Cutover — make the new Mac live

Do these two in immediate succession so the bot is never double-polled:

1. **Old Mac — go to standby:**
   ```bash
   hermes gateway stop
   launchctl bootout gui/$(id -u)/ai.hermes.gateway   # so it won't relaunch at next login
   ```

2. **New Mac — go live:**
   ```bash
   # set Telegram via the desktop app (preferred), OR confirm the carried token then restart
   hermes gateway install     # writes a fresh launchd plist with THIS Mac's paths
   hermes gateway start
   ```

---

## D. Verify on the new Mac

```bash
# token is valid and this machine owns the poll (read-only):
TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' ~/.hermes/.env | cut -d= -f2-)
curl -s "https://api.telegram.org/bot$TOKEN/getMe" | python3 -m json.tool

hermes gateway status            # should be running, no 409 in logs/gateway.error.log
```

- Send a Telegram message to **@Riseofthelobterbot** — it should answer. If it says
  "Unauthorized user", check `TELEGRAM_ALLOWED_USERS` is the **numeric** id
  `8577725778`, and that there's no stray `TELEGRAM_PROXY=` line.
- **Crons carried over?** `cat ~/.hermes/cron/jobs.json` should list `ee2489024ace`
  (SEO Boss), `6f234883f85b` (Weekly Digest), `cc074ec2168b` (Tech Sweep). They tick
  while the gateway runs.
- **Boss smoke test (headless, writes to the live Sheet):**
  ```bash
  hermes -z "$(cat ~/.hermes/scripts/boss_prompt.txt)" \
    -m anthropic/claude-sonnet-4.6 --provider nous --yolo -t terminal,file,web
  ```
  Confirm it reaches the "Hermes SEO" Google Sheet and the SE Ranking MCP. If model
  errors with `HTTP 400: Model parameter is required`, `config.yaml` lost its
  `model.default` — re-add `default: anthropic/claude-sonnet-4.6` / `provider: nous`.

---

## E. Switching back to the fallback (old Mac), later

Reverse of cutover — stop the live one first:
```bash
# on whichever Mac is currently live:
hermes gateway stop && launchctl bootout gui/$(id -u)/ai.hermes.gateway
# on the Mac you want live:
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.hermes.gateway.plist
hermes gateway start
```
The old Mac's config will be whatever it had at export time; if you've since changed
config/personas on the new Mac, re-export and lay it down before relying on the
fallback.

---

## Cleanup

Once the new Mac is verified, **delete the bundle from both machines and Downloads**
— it contains live secrets:
```bash
rm -f ~/hermes-migration/hermes-migration-*.tgz*  ~/Downloads/hermes-migration-*.tgz*
```
