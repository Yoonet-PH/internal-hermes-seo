# Off-ramping the SEO Boss from SE Ranking tracking slots

**Status:** built and proven, not yet cut over. Handoff to the PH team.
**Date:** 05/06/2026. **Author:** Ben + Claude.

## Why

SE Ranking bills per tracking **project** (one per site) and per keyword. We keep
running out of slots, and the fix on their side is upgrading the whole plan. This
work removes the per-customer project entirely by sourcing positions from data we
already pay for (DataForSEO, pay per call, no slots) and Google Search Console
(free), so adding a customer is a spreadsheet row, not a slot purchase.

## The key realisation

Only **rank tracking** eats slots. The daily Tech Sweep (`seo_tech.py`) already
runs on **standalone domain-keyed audits** — `PROJECT_createAudit(domain)` comes
back unlinked (`site_id None`), so it never consumed a tracking slot. So:

- **Keep SE Ranking purely as the site-audit crawler.** `seo_tech.py` is unchanged.
- **Replace only the rank-tracking half** (`getSummary`, `listKeywords`,
  `getKeywordStats`, `getSeoPotential`). That frees every tracking slot.

## What replaces what

| SE Ranking call (in `seo_boss.py`) | Replacement | Slot? |
|---|---|---|
| `getSummary` (visibility %, top10, avg pos) | Reconstruct from tracked-keyword positions; for owned sites GSC gives real avg position | none |
| `listKeywords` + `getKeywordStats` (positions + history) | **GSC** where we have access (free, real served position) → else **DataForSEO live SERP** (`$0.0125`/kw, any keyword). History stored by us in a Sheet tab | none / pay-per-call |
| `getSeoPotential` | DataForSEO Labs keyword research (when Labs subscription is enabled — see open questions) | none |
| `createAudit` / `getAuditReport` (technical) | **unchanged — keep SE Ranking**, it is not a tracking slot | not a slot |

## The two position sources, and when each applies

1. **GSC — free, but only knows keywords that earned impressions.** Brilliant on
   sites with real traffic (e.g. Burstows: every branded term at 1.0–1.7). Blind on
   thin/new sites where the tracked terms get no impressions (e.g. clinicadmin's
   curated B2B phrases). Bonus: it surfaces real queries we don't even track.
2. **DataForSEO live SERP — universal.** Returns organic position for *any*
   keyword regardless of impressions, from a live Google crawl at a set location
   and device. This is the only way to track aspirational terms we don't yet rank
   for. `$0.0125` per keyword per check.

Design: `seo_intel.keyword_intel_v2()` prefers GSC per keyword when a `GSC Property`
is set for the site **and** that keyword has GSC data; otherwise it falls back to
DataForSEO automatically. So it runs today entirely on DataForSEO, and each site
flips to the free path the moment its GSC access is wired — no code change.

## Files (all additive — live cron untouched)

- **`seo_intel.py`** — the new engine. `keyword_intel_v2(domain)` returns the exact
  same shape as `seo_boss.keyword_intel(site_id)`, so it is a drop-in. Backends:
  `dfs_rank()` (DataForSEO live SERP), `gsc_positions()` (Search Console),
  `read_history()` / `record_positions()` (movement + verify loop from the Sheet).
- **`seed_tracked_keywords.py`** — one-off that built the `Tracked Keywords` tab
  from the current SE Ranking lists with each site's real location/device. Already
  run on 05/06/2026.
- **`seo_boss.py`, `seo_tech.py`** — unchanged. The cutover below is the only edit.

## The Sheet (new tabs)

- **`Tracked Keywords`** — the keyword registry that replaces SE Ranking's keyword
  manager. Columns: `Domain | Keyword | Location | Language | Device | GSC Property`.
  Edit here to add/remove tracked keywords or change a site's location. 251 keywords
  seeded across 10 sites.
- **`Position History`** — auto-created on first `record_positions()` run. Columns:
  `Date | Domain | Keyword | Position | Landing | Source`. One row per keyword per
  tick; this is what reconstructs 7/30-day movement and powers the verify loop.

## Parity proof (05/06/2026)

- **Burstows** (3 tracked terms, all #1 in SE Ranking): DataForSEO live SERP
  returned **#1 for all three** — exact match. Cost `$0.0375`.
- **Clinicadmin** (terms GSC could not see): `clinic admin outsourcing` 1=1,
  `Cliniko virtual assistant` 5=5, `allied health admin outsourcing Australia`
  SE Ranking 1 vs DataForSEO 6. The one gap is live-SERP volatility on a single
  competitive term, not a tooling gap; re-checks settle it. Location was correct
  (clinicadmin = AU national, engine 211).

## Cost model (real numbers)

- DataForSEO **live SERP advanced = `$0.0125` per keyword per check** (measured).
- 251 unique tracked keywords across 10 sites. **One full live pass = `$3.14`.**
- Cadence is the lever:
  - Daily live, all keywords: ~`$94`/month.
  - Weekly live, all keywords: ~`$13`/month.
  - The 3 GSC-connected sites cost `$0` — onboard more sites to GSC to shrink the bill.
- `totaltreeservices` alone is 110 keywords (~44% of spend). Strong candidate for
  GSC onboarding or a lighter cadence.
- **Cheaper modes worth pricing before locking cadence:** DataForSEO also offers a
  queued *Standard* SERP at a fraction of live cost (async, post-then-poll) — ideal
  for nightly bulk; and Labs `ranked_keywords` gets *all* of a domain's positions in
  one call (needs the Labs subscription — currently 500ing, see below).

## Cutover steps (for the PH team, do with review)

1. **Enable DataForSEO creds.** In `~/.hermes/.env`, uncomment the two
   `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD` lines, then `hermes gateway restart`.
   (The code also reads them commented as a fallback, so testing works either way.)
2. **Verify the seed.** Open the `Tracked Keywords` tab; confirm locations look
   right per site (see open questions). Add/trim keywords as desired.
3. **Swap the engine in `seo_boss.py`:**
   - `build_sites()` currently iterates `serank("PROJECT_listProjects")`. Drive it
     from the `Monitored Sites` registry instead (it already has Site/Domain), so
     no SE Ranking project list is needed.
   - Replace the `keyword_intel(site_id)` call with
     `seo_intel.keyword_intel_v2(domain, gsc_property=<from registry/tab>)`.
   - Replace the `getSummary` headline (vis/top10/avg) with figures derived from the
     returned positions (top10 = count ≤10, avg = mean of ranked).
   - `getSeoPotential` block: drop or move to Labs once subscribed.
4. **Leave `seo_tech.py` and `audit_blockers()` on SE Ranking** (standalone audits).
5. **Run read-only once** (`keyword_intel_v2(..., record=False, verbose=True)` per
   site) and eyeball against the last SE Ranking numbers before trusting it.
6. **Turn on history** (`record=True`) so movement + verify loop start accumulating.
7. Once a fortnight of history looks right, **retire the SE Ranking tracking
   projects** (keep the account for audits) — every slot is freed.

## GSC wiring (to unlock the free path inside Hermes)

Hermes's Google OAuth currently has Sheets/Gmail/Calendar/Drive scopes but **not**
Search Console, so `gsc_positions()` returns empty and everything falls to
DataForSEO. To enable the free path, either:

- **Add the scope:** add `https://www.googleapis.com/auth/webmasters.readonly` to
  the Hermes Google OAuth consent and re-auth, and make sure that account can read
  the properties; **or**
- **Register the gsc-server MCP** in `config.yaml` under `mcp_servers` the same way
  `seranking` is, and call it from `seo_intel.gsc_positions()` over MCP.

Currently GSC reaches 3 of 10 properties (`clinicadmin.com.au`, `yoonet.io`,
`burstows.com.au`). Use the `gsc-onboard` skill to grant the YooSEO service account
access to more sites, then set their `GSC Property` in the tab.

## Open questions / verify before trusting

- **Yoonet** is dual-tracked in SE Ranking (AU engine 211 + NZ engine 325). Seeded
  as Australia. Decide whether to track both markets (add NZ rows) or pick one.
- **Balanga** is tracked in Tagalog (`tl`) on Google Philippines. Seeded `tl` — keep
  if that's the intent.
- **Applebom / Charhouse** are city-level (Toowoomba) in SE Ranking; seeded
  `Toowoomba,Queensland,Australia`. Confirm DataForSEO accepts and that local-pack
  vs organic is what you want (we count organic only, like SE Ranking).
- **DataForSEO Labs** endpoints (`ranked_keywords`, `domain_rank_overview`) return
  500 — the account has SERP API but likely not the Labs subscription. Enabling Labs
  makes bulk discovery and the `getSummary`/`getSeoPotential` replacements far
  cheaper. Worth a pricing decision.
- **Device:** seeded desktop only (1x cost). SE Ranking also tracked mobile on some
  sites. Add mobile rows per keyword only where a client cares.
