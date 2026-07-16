#!/usr/bin/env python3
"""
seo_intel.py — slot-free keyword intelligence for the Hermes SEO department.

A drop-in replacement for `seo_boss.keyword_intel(site_id)` that does NOT need a
SE Ranking tracking project (and therefore no slot) per site. It sources live
positions from:

  1. GSC  — for any site whose Search Console we can read (real served positions,
            free, unlimited). Preferred when available.
  2. DataForSEO live SERP — for every other site (organic rank from a live Google
            crawl, pay-per-call ~$0.0125/keyword, no slots, no per-site cap).

History (what SE Ranking stored for free) is kept by us in a "Position History"
tab and appended every tick, so 7/30-day movement and the closed verify loop
reconstruct exactly as before.

Return shape matches seo_boss.keyword_intel() so it is a true drop-in:
  [{id, kw, pos_now, pos_prev, change, landing, striking, series}]

Keyword lists live in a "Tracked Keywords" tab (Domain | Keyword | Location |
Language | Device | GSC Property) — this replaces SE Ranking's keyword manager.
With no GSC Property set, the site uses DataForSEO. Set one to switch it to the
free path the moment access is granted; the code falls back automatically if the
GSC read fails (e.g. scope not yet added), so nothing breaks in the meantime.

Credentials:
  - DataForSEO: DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD (HTTP basic). Already present
    in ~/.hermes/.env — uncomment the two lines so the gateway loads them.
  - GSC: google_api credentials need the webmasters.readonly scope, OR register the
    gsc-server MCP in config.yaml the way `seranking` is. Until then GSC degrades
    to DataForSEO automatically.
"""
import os
import re
import json
import base64
import urllib.request
import datetime
from pathlib import Path

import seo_boss as s  # reuse sheet helpers, dates, domain normaliser

TRACKED_TAB = "Tracked Keywords"
HISTORY_TAB = "Position History"
TRACKED_HEADER = ["Domain", "Keyword", "Location", "Language", "Device", "GSC Property"]
HISTORY_HEADER = ["Date", "Domain", "Keyword", "Position", "Landing", "Source"]

DFS_ENDPOINT = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"
DEFAULT_LOCATION = "Australia"   # most of the book is AU; override per keyword row
DEFAULT_LANGUAGE = "en"
SERP_DEPTH = 100                 # how deep we look for the domain before calling it 100+
NOT_FOUND = 999                  # mirrors seo_boss: 999 == "100+ / not ranking"


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #
def _dfs_auth():
    """Basic-auth header value. Reads env first; falls back to the (possibly
    commented) ~/.hermes/.env so it runs before the gateway env is reloaded."""
    login = os.environ.get("DATAFORSEO_LOGIN")
    pw = os.environ.get("DATAFORSEO_PASSWORD")
    if not (login and pw):
        try:
            env = (Path.home() / ".hermes/.env").read_text()
            def grab(k):
                m = re.search(rf"^#?\s*{k}=(.+)$", env, re.M)
                return m.group(1).strip().strip('"').strip("'") if m else None
            login = login or grab("DATAFORSEO_LOGIN")
            pw = pw or grab("DATAFORSEO_PASSWORD")
        except Exception:
            pass
    if not (login and pw):
        raise RuntimeError("DataForSEO creds missing (DATAFORSEO_LOGIN/PASSWORD)")
    return "Basic " + base64.b64encode(f"{login}:{pw}".encode()).decode()


# --------------------------------------------------------------------------- #
# DataForSEO backend — live organic position for one keyword
# --------------------------------------------------------------------------- #
def dfs_rank(domain, keyword, location=DEFAULT_LOCATION,
             language=DEFAULT_LANGUAGE, device="desktop"):
    """(position, landing_url, cost). position = best organic rank_group for the
    domain, or NOT_FOUND if it isn't in the first SERP_DEPTH organic results.
    We count organic only (ignoring local_pack etc.) to match SE Ranking."""
    bare = s._bare_domain(domain)
    body = json.dumps([{
        "keyword": keyword, "location_name": location, "language_code": language,
        "device": device, "depth": SERP_DEPTH,
    }]).encode()
    req = urllib.request.Request(DFS_ENDPOINT, data=body, method="POST",
                                 headers={"Authorization": _dfs_auth(),
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        d = json.loads(r.read().decode())
    cost = d.get("cost", 0)
    task = (d.get("tasks") or [{}])[0]
    result = (task.get("result") or [{}])[0]
    items = result.get("items") or []
    best, landing = NOT_FOUND, ""
    for it in items:
        if it.get("type") != "organic":
            continue
        if s._bare_domain(it.get("domain")) == bare:
            rg = it.get("rank_group") or NOT_FOUND
            if rg < best:
                best, landing = rg, it.get("url", "")
    return best, landing, cost


# --------------------------------------------------------------------------- #
# GSC backend — real served position, free (when we can read the property)
# --------------------------------------------------------------------------- #
def gsc_positions(gsc_property, days=30):
    """{keyword_lower: (position, landing)} from Search Console, or {} if we
    can't read it (missing scope / access). Position is GSC avg over `days`."""
    try:
        from googleapiclient.discovery import build
        import google_api as gapi
        svc = build("searchconsole", "v1", credentials=gapi.sa_credentials(
            ["https://www.googleapis.com/auth/webmasters.readonly"]))
    except Exception:
        return {}
    end = s.today()
    start = end - datetime.timedelta(days=days)
    try:
        resp = svc.searchanalytics().query(siteUrl=gsc_property, body={
            "startDate": start.isoformat(), "endDate": end.isoformat(),
            "dimensions": ["query", "page"], "rowLimit": 25000,
        }).execute()
    except Exception:
        return {}
    out = {}
    for row in resp.get("rows", []):
        q = (row["keys"][0] or "").lower()
        page = row["keys"][1] if len(row["keys"]) > 1 else ""
        pos = row.get("position")
        if pos is None:
            continue
        if q not in out or pos < out[q][0]:
            out[q] = (pos, page)
    return out


# --------------------------------------------------------------------------- #
# sheet: tracked keywords + position history
# --------------------------------------------------------------------------- #
def tracked_keywords(domain):
    """Rows from the Tracked Keywords tab for one domain."""
    rows = s.read_tab(TRACKED_TAB)
    recs, _ = s.rows_as_dicts(rows)
    bd = s._bare_domain(domain)
    return [d for d in recs if s._bare_domain(d.get("Domain")) == bd]


def read_history(domain, kw, days=35):
    """Sorted [(date, pos)] for one domain+keyword within `days`."""
    rows = s.read_tab(HISTORY_TAB)
    recs, _ = s.rows_as_dicts(rows)
    bd, k = s._bare_domain(domain), kw.strip().lower()
    cutoff = s.days_ago(days)
    series = []
    for d in recs:
        if s._bare_domain(d.get("Domain")) != bd:
            continue
        if (d.get("Keyword", "") or "").strip().lower() != k:
            continue
        if (d.get("Source") or "").startswith("error"):
            # Failed lookups are recorded as pos 999 for telemetry — treating
            # them as measurements turns a feed outage into fake movement.
            continue
        date = (d.get("Date") or "").strip()
        if date and date >= cutoff:
            try:
                series.append((date, int(float(d.get("Position") or NOT_FOUND))))
            except ValueError:
                pass
    series.sort()
    return series


def record_positions(rows):
    """Append [Date, Domain, Keyword, Position, Landing, Source] history rows."""
    if not rows:
        return
    s.ensure_tab(HISTORY_TAB, HISTORY_HEADER)
    s.append_rows(HISTORY_TAB, rows)


# --------------------------------------------------------------------------- #
# the drop-in
# --------------------------------------------------------------------------- #
def keyword_intel_v2(domain, keywords=None, location=None, language=DEFAULT_LANGUAGE,
                     device="desktop", gsc_property=None, record=True, verbose=False):
    """Slot-free equivalent of seo_boss.keyword_intel(site_id).

    domain      : site domain (with or without scheme/www)
    keywords    : explicit list; if None, read from the Tracked Keywords tab
    gsc_property: GSC siteUrl to prefer (free path); falls back to DataForSEO
    Returns the same dict shape seo_boss expects.
    """
    # resolve keyword list + per-row config
    rowcfg = {}
    if keywords is None:
        rows = tracked_keywords(domain)
        keywords = []
        for d in rows:
            kw = (d.get("Keyword") or "").strip()
            if not kw:
                continue
            keywords.append(kw)
            rowcfg[kw.lower()] = {
                "location": (d.get("Location") or location or DEFAULT_LOCATION),
                "language": (d.get("Language") or language),
                "device": (d.get("Device") or device),
                "gsc": (d.get("GSC Property") or gsc_property or ""),
            }
    # de-dupe, preserve order
    seen, uniq = set(), []
    for kw in keywords:
        k = kw.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(kw)

    # one GSC pull per distinct property, reused across its keywords
    gsc_cache = {}

    def gsc_for(prop):
        if not prop:
            return {}
        if prop not in gsc_cache:
            gsc_cache[prop] = gsc_positions(prop)
        return gsc_cache[prop]

    # Resolve each keyword: GSC first (already batched into one call per
    # property), else a live DataForSEO SERP lookup. The DataForSEO endpoint is
    # synchronous (~13s/keyword as it crawls Google live), so doing these
    # sequentially is the dominant cost of a tick. They are independent and
    # purely I/O-bound, so we fan them out across a thread pool: wall time drops
    # from sum(calls) to ~max(call). GSC keywords resolve inline (no network).
    from concurrent.futures import ThreadPoolExecutor

    def resolve(kw):
        """-> (kw, pos_now, landing, source, cost). Never raises."""
        cfg = rowcfg.get(kw.lower(), {
            "location": location or DEFAULT_LOCATION, "language": language,
            "device": device, "gsc": gsc_property or ""})
        gmap = gsc_for(cfg["gsc"])
        if kw.lower() in gmap:
            raw, landing = gmap[kw.lower()]
            return kw, int(round(raw)), landing, "gsc", 0.0
        try:
            pos_now, landing, cost = dfs_rank(
                domain, kw, cfg["location"], cfg["language"], cfg["device"])
            return kw, pos_now, landing, "dataforseo", float(cost or 0)
        except Exception as e:
            return kw, NOT_FOUND, "", f"error:{str(e)[:30]}", 0.0

    # Pre-warm the GSC cache once (single-threaded) so concurrent workers don't
    # race to issue duplicate Search Console pulls for the same property.
    for kw in uniq:
        gsc_for(rowcfg.get(kw.lower(), {}).get("gsc", gsc_property or ""))

    max_workers = min(10, len(uniq)) or 1
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        resolved = list(pool.map(resolve, uniq))   # preserves input order

    out, hist_rows, total_cost = [], [], 0.0
    for kw, pos_now, landing, source, cost in resolved:
        total_cost += cost
        if verbose:
            print(f"  {kw!r:42} pos {pos_now:<4} via {source}")
        series = read_history(domain, kw)
        series_with_now = series + [(s.tstr(), pos_now)]
        pos_prev = series[0][1] if series else pos_now
        out.append({
            "id": "", "kw": kw, "pos_now": pos_now, "pos_prev": pos_prev,
            "change": pos_prev - pos_now,
            "landing": landing,
            "striking": 11 <= pos_now <= 20,
            "series": series_with_now,
            "source": source,
        })
        hist_rows.append([s.tstr(), s._bare_domain(domain), kw, pos_now, landing, source])

    if record:
        record_positions(hist_rows)
    out.sort(key=lambda k: (k["pos_now"] if k["pos_now"] else NOT_FOUND))
    if verbose:
        print(f"  (DataForSEO spend this run: ${round(total_cost, 4)})")
    return out


if __name__ == "__main__":
    import sys
    dom = sys.argv[1] if len(sys.argv) > 1 else None
    if not dom:
        print("usage: seo_intel.py <domain> [\"kw1\" \"kw2\" ...]")
        raise SystemExit(1)
    kws = sys.argv[2:] or None
    for k in keyword_intel_v2(dom, keywords=kws, verbose=True):
        print(f"{k['pos_now']:>4}  {k['kw']}  ({k['source']})  {k['landing']}")
