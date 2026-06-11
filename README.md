# Hermes SEO Boss

The deterministic engine behind Yoonet's autonomous SEO department. These scripts
run inside Hermes (`~/.hermes/scripts`) on a cron and do the un-glamorous,
repeatable work — sync, state, intelligence, technical health — so the LLM agent
only has to supply judgement and writing.

**Source of truth:** the Sheet's `Tracked Keywords` / `Position History` tabs,
fed by GSC + DataForSEO (`seo_intel.py` — cut over 11/06/2026, see
`SE_RANKING_OFFRAMP.md`). SE Ranking remains only as the technical site-audit
crawler.
**Management board:** one Google Sheet (the "Hermes SEO" sheet). Each monitored
site has a tab; the `Monitored Sites` tab is the registry/index.

## Scripts

| File | What it does | Cron |
|---|---|---|
| `seo_boss.py` | The deterministic boss engine (v3, slot-free). Each tick it builds the situation from the **Monitored Sites** registry + **Position History** tab (live positions via `seo_intel` only for the action site), handles **VERIFY** (real before/after written from history) and **CHASE** (overdue stamps) inline with no LLM, then picks the agent's ONE action: **AUDIT** (weekly per site) › **EMAIL** (monthly client update) › **NONE**. | via `seo_boss.sh` |
| `seo_boss.sh` | The cost-gated tick wrapper (no-agent cron). Runs the engine; only when `NEXT_ACTION != NONE` does it invoke the agent headlessly (model pinned to sonnet-4.6) with the report embedded in the prompt. Idle ticks cost zero tokens. | every 30 min |
| `seo_boss.py digest` | Deterministic weekly oversight digest → `Weekly Digest` tab. | `6f234883f85b` — Mon 08:00 NZ (agent narrative pinned to haiku-4.5) |
| `cron_watchdog.py` | Daily job-health check → **Ops Health** tab (paused / failing / stale jobs, missing config-backup key). Silent stdout when healthy. | daily 07:30 NZ |
| `config-backup.sh` | Weekly encrypted snapshot of the unique Hermes state to iCloud Drive (key: `~/.hermes/.config-backup-key`). | `ac1757d9c286` — Sun 07:00 NZ |
| `seo_tech.py` / `seo_tech.sh` | Daily technical sweep. Pulls each site's SE Ranking **Website Audit** (creating one if missing), extracts errors/warnings with affected-page counts, and writes a **TECHNICAL HEALTH** panel onto each site tab (cols L+) plus a sortable **Tech Health** roll-up tab (worst score first). Fully deterministic, no LLM. | `cc074ec2168b` — daily 05:00 NZ (runs before the day's boss work) |
| `boss_prompt.txt` | The agent prompt the boss cron delivers. |
| `backfill.sh` | One-time: loop a fresh boss tick per never-audited site until none are due. |
| `seo_boss_v1_backup.py` | Snapshot of the v1 boss before the v2 intelligence/verify rewrite. |
| `seo_intel.py` | **Slot-free** keyword intelligence. `keyword_intel_v2(domain)` sources positions from GSC (free where wired) → DataForSEO live SERP (pay-per-call), keeping history in the Sheet's `Position History` tab. **Wired into the boss since 11/06/2026** — SE Ranking tracking projects can be retired once a fortnight of history looks right. | — |
| `seed_tracked_keywords.py` | One-off (run 05/06/2026): built the `Tracked Keywords` tab from the live SE Ranking lists with each site's real location/device. | — |
| `SE_RANKING_OFFRAMP.md` | The plan + parity proof + cost model + cutover steps for retiring SE Ranking tracking slots. **Start here.** | — |

## How a site flows through it

1. **Tech sweep** (05:00) surfaces blockers per site on its tab.
2. **Boss audit** turns rankings + blockers into team tasks. The `Repo / Access`
   column on the registry routes the fix: a **Claude Code prompt** where Yoonet
   owns the repo, a **client recommendation** where the site is external.
3. VA marks a task **Done** → the next tick's deterministic **VERIFY** computes
   the real position change from the Position History tab and writes the result
   (no LLM). Overdue tasks are stamped the same way. Closed loop.

## Running locally

```bash
~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/seo_tech.py --no-recheck   # read-only sweep
~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/seo_boss.py                 # print next action
```

The venv supplies `yaml` + `googleapiclient`. The SE Ranking API key is read from
`~/.hermes/config.yaml` at runtime and is **not** in this repo. Nor are any
credentials, tokens, the venv, or local state — see `.gitignore`.
