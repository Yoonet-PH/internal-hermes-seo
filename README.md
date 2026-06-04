# Hermes SEO Boss

The deterministic engine behind Yoonet's autonomous SEO department. These scripts
run inside Hermes (`~/.hermes/scripts`) on a cron and do the un-glamorous,
repeatable work — sync, state, intelligence, technical health — so the LLM agent
only has to supply judgement and writing.

**Source of truth:** SE Ranking (via its MCP endpoint) — *migrating off tracking
slots; see `SE_RANKING_OFFRAMP.md` and `seo_intel.py`.*
**Management board:** one Google Sheet (the "Hermes SEO" sheet). Each monitored
site has a tab; the `Monitored Sites` tab is the registry/index.

## Scripts

| File | What it does | Cron |
|---|---|---|
| `seo_boss.py` | The boss. Each tick it syncs sites from SE Ranking, builds per-keyword intelligence (positions, movement, striking distance), and picks the ONE next action: **AUDIT** (weekly per site) › **VERIFY** (team-done tasks, real before/after from position history) › **EMAIL** (monthly client update) › **CHASE** (overdue) › **NONE**. Hands the agent a rich brief; the agent writes tasks (each with a paste-ready Claude Code prompt) and client emails. | `ee2489024ace` — every 30 min |
| `seo_boss.py digest` | Deterministic weekly oversight digest → `Weekly Digest` tab. | `6f234883f85b` — Mon 08:00 NZ |
| `seo_tech.py` / `seo_tech.sh` | Daily technical sweep. Pulls each site's SE Ranking **Website Audit** (creating one if missing), extracts errors/warnings with affected-page counts, and writes a **TECHNICAL HEALTH** panel onto each site tab (cols L+) plus a sortable **Tech Health** roll-up tab (worst score first). Fully deterministic, no LLM. | `cc074ec2168b` — daily 05:00 NZ (runs before the day's boss work) |
| `boss_prompt.txt` | The agent prompt the boss cron delivers. |
| `backfill.sh` | One-time: loop a fresh boss tick per never-audited site until none are due. |
| `seo_boss_v1_backup.py` | Snapshot of the v1 boss before the v2 intelligence/verify rewrite. |
| `seo_intel.py` | **Slot-free** keyword intelligence. `keyword_intel_v2(domain)` is a drop-in for `seo_boss.keyword_intel(site_id)` that sources positions from DataForSEO live SERP (universal, pay-per-call) and GSC (free where we have access), keeping history in the Sheet. Removes the need for an SE Ranking tracking project per site. Not yet wired into the cron — see the off-ramp doc. | — |
| `seed_tracked_keywords.py` | One-off (run 05/06/2026): built the `Tracked Keywords` tab from the live SE Ranking lists with each site's real location/device. | — |
| `SE_RANKING_OFFRAMP.md` | The plan + parity proof + cost model + cutover steps for retiring SE Ranking tracking slots. **Start here.** | — |

## How a site flows through it

1. **Tech sweep** (05:00) surfaces blockers per site on its tab.
2. **Boss audit** turns rankings + blockers into team tasks. The `Repo / Access`
   column on the registry routes the fix: a **Claude Code prompt** where Yoonet
   owns the repo, a **client recommendation** where the site is external.
3. VA marks a task **Done** → boss **VERIFY** computes the real position change
   from SE Ranking history and writes the result. Closed loop.

## Running locally

```bash
~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/seo_tech.py --no-recheck   # read-only sweep
~/.hermes/hermes-agent/venv/bin/python ~/.hermes/scripts/seo_boss.py                 # print next action
```

The venv supplies `yaml` + `googleapiclient`. The SE Ranking API key is read from
`~/.hermes/config.yaml` at runtime and is **not** in this repo. Nor are any
credentials, tokens, the venv, or local state — see `.gitignore`.
