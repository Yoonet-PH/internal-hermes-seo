#!/usr/bin/env python3
"""
SEO Tech Sweep — deterministic daily technical-health pass for the Hermes SEO
department. Runs once a day BEFORE the SEO Boss working ticks, so every site's
blockers are fresh on the board when the boss audits and the VAs start.

It does NOT use an LLM. For each monitored site it:
  1. Finds the site's SE Ranking website audit (creates one if missing).
  2. Reads the audit report and extracts every check with affected pages,
     keeping errors + warnings as line-item blockers.
  3. Writes a "TECHNICAL HEALTH" panel onto that site's own tab (columns L+),
     refreshed in place each run.
  4. Writes a sortable "Tech Health" roll-up tab — one row per site, worst
     score first — so you can see at a glance what is stopping each site.
  5. Triggers a re-crawl of any audit older than today so tomorrow is fresh.

The SEO Boss reads these panels and turns blockers into team tasks: a Claude
Code prompt where we own the repo, a client recommendation where we do not.

Usage:  python seo_tech.py            # full sweep (used by the daily cron)
        python seo_tech.py --no-recheck   # skip re-crawls (read only)
"""
import sys
import seo_boss as s

TECH_TAB = "Tech Health"
TECH_HEADER = ["Updated", "Site", "Domain", "Score", "Errors", "Warnings",
               "Notices", "Top blocker", "Crawled", "Last crawl", "Audit status"]
PANEL_COL = "L"            # site-tab panel starts here, to the right of tasks (A-J)
PANEL_ROWS = 34            # fixed block height so each run overwrites the last
SEV_RANK = {"error": 0, "warning": 1, "notice": 2, "passed": 3}


def bare(d):
    d = (d or "").replace("https://", "").replace("http://", "").rstrip("/").lower()
    return d[4:] if d.startswith("www.") else d


def rank_audit(a):
    """Higher is better: finished beats in-progress, then newer id."""
    return (1 if a.get("status") == "finished" else 0, int(a.get("id") or 0))


def list_audits():
    """bare-domain -> best audit dict. Keyed by domain because audits created
    via createAudit(domain) come back unlinked (site_id None), so matching on
    site_id misses them; domain matches both linked and standalone audits."""
    res = s.serank("PROJECT_listAudits") or {}
    items = res.get("items", res if isinstance(res, list) else [])
    by_dom = {}
    for a in items:
        if not isinstance(a, dict):
            continue
        dom = bare(a.get("url"))
        if not dom:
            continue
        cur = by_dom.get(dom)
        if cur is None or rank_audit(a) > rank_audit(cur):
            by_dom[dom] = a
    return by_dom


def extract_blockers(report):
    """Return (errors, warnings, notices, pages, score, blockers[], cwv)."""
    errors = report.get("total_errors", 0)
    warnings = report.get("total_warnings", 0)
    notices = report.get("total_notices", 0)
    pages = report.get("total_pages", 0)
    score = report.get("score_percent", report.get("weighted_score_percent", ""))
    blockers = []
    for sec in report.get("sections", []) or []:
        secname = sec.get("name", sec.get("uid", ""))
        for code, prop in (sec.get("props", {}) or {}).items():
            if not isinstance(prop, dict):
                continue
            status = (prop.get("status") or "").lower()
            val = prop.get("value") or 0
            if status in ("error", "warning") and isinstance(val, (int, float)) and val > 0:
                blockers.append({"sev": status, "name": prop.get("name", code),
                                 "pages": int(val), "section": secname})
    blockers.sort(key=lambda b: (SEV_RANK.get(b["sev"], 9), -b["pages"]))
    cwv = ""
    cux = report.get("chromeux") or {}
    if isinstance(cux, dict):
        lcp = cux.get("largest_contentful_paint") or cux.get("lcp")
        cls = cux.get("cumulative_layout_shift") or cux.get("cls")
        inp = cux.get("interaction_to_next_paint") or cux.get("inp")
        bits = [f"LCP {lcp}" if lcp else "", f"INP {inp}" if inp else "", f"CLS {cls}" if cls else ""]
        cwv = " · ".join(b for b in bits if b)
    return errors, warnings, notices, pages, score, blockers, cwv


def build_panel(audit, report):
    """Fixed-height L-column block for one site tab."""
    if report is None:
        body = [["TECHNICAL HEALTH  (auto · daily)", "", "", ""],
                ["Status", "Audit crawl queued — results after next sweep", "", ""],
                ["", "Crawls take a few minutes; check back tomorrow.", "", ""]]
        return pad(body)
    errors, warnings, notices, pages, score, blockers, cwv = extract_blockers(report)
    last = audit.get("last_update", "") if audit else ""
    body = [
        ["TECHNICAL HEALTH  (auto · daily)", "", "", ""],
        ["Score", f"{score}/100", "Last crawl", last],
        ["Errors", errors, "Warnings", warnings],
        ["Notices", notices, "Pages crawled", pages],
    ]
    if cwv:
        body.append(["Core Web Vitals", cwv, "", ""])
    body.append(["", "", "", ""])
    body.append(["Severity", "Blocker", "Pages", "Section"])
    if not blockers:
        body.append(["clean", "No blocking errors or warnings", "", ""])
    else:
        for b in blockers[:PANEL_ROWS - len(body) - 1]:
            body.append([b["sev"].upper(), b["name"], b["pages"], b["section"]])
    return pad(body)


def pad(body):
    block = [(row + ["", "", "", ""])[:4] for row in body]
    while len(block) < PANEL_ROWS:
        block.append(["", "", "", ""])
    return block[:PANEL_ROWS]


def top_blocker(report):
    if report is None:
        return "crawl queued"
    _, _, _, _, _, blockers, _ = extract_blockers(report)
    if not blockers:
        return "clean"
    b = blockers[0]
    return f"{b['sev'].upper()}: {b['name']} ({b['pages']}p)"


def main(recheck=True):
    recs, _ = s.rows_as_dicts(s.read_tab(s.REGISTRY_TAB))
    audits = list_audits()
    roll = []
    for d in recs:
        site = d.get("Site", "")
        sid = str(d.get("SE Ranking ID") or "")
        domain = (d.get("Domain") or "").replace("https://", "").replace("http://", "").lstrip("/")
        bare_dom = bare(domain)
        audit = audits.get(bare_dom)
        report = None
        status = "no audit"

        if audit is None and bare_dom:
            try:
                created = s.serank("PROJECT_createAudit", {"domain": bare_dom, "title": site or bare_dom})
                aid = (created or {}).get("audit_id") or (created or {}).get("id")
                status = "crawl queued (created)"
                audit = {"id": aid, "site_id": sid, "last_update": "", "status": "in_progress"}
            except Exception as e:
                status = f"create failed: {str(e)[:40]}"
        elif audit is not None:
            aid = audit.get("id") or audit.get("audit_id")
            try:
                report = s.serank("PROJECT_getAuditReport", {"audit_id": int(aid)})
                status = audit.get("status", "finished")
            except Exception as e:
                status = f"report err: {str(e)[:40]}"
            if recheck and audit.get("last_update", "") < s.tstr():
                try:
                    s.serank("PROJECT_recheckAudit", {"audit_id": int(aid)})
                except Exception:
                    pass

        # write the per-tab panel
        if site:
            try:
                s.update_range(f"'{site}'!{PANEL_COL}1", build_panel(audit, report))
            except Exception as e:
                print(f"  ! panel write failed for '{site}': {str(e)[:60]}")

        if report is not None:
            errors, warnings, notices, pages, score, _, _ = extract_blockers(report)
            last = audit.get("last_update", "") if audit else ""
        else:
            errors = warnings = notices = pages = ""
            score = ""
            last = audit.get("last_update", "") if audit else ""
        roll.append([s.tstr(), site, domain, score, errors, warnings, notices,
                     top_blocker(report), pages, last, status])

    # sortable roll-up: worst score first, blank scores (queued) at the bottom
    def sortkey(r):
        sc = r[3]
        return (0, int(sc)) if str(sc).isdigit() else (1, 0)
    roll.sort(key=sortkey)
    s.ensure_tab(TECH_TAB, TECH_HEADER)
    s.update_range(f"'{TECH_TAB}'!A1", [TECH_HEADER] + roll)

    done = sum(1 for r in roll if str(r[3]).isdigit())
    queued = len(roll) - done
    tot_err = sum(int(r[4]) for r in roll if str(r[4]).isdigit())
    tot_warn = sum(int(r[5]) for r in roll if str(r[5]).isdigit())
    print(f"# SEO Tech Sweep — {s.tstr()}")
    print(f"{len(roll)} sites: {done} with audit data, {queued} crawl queued. "
          f"Totals: {tot_err} errors, {tot_warn} warnings across audited sites.")
    print("Worst first:")
    for r in roll:
        print(f"  {str(r[3]) or '—':>4}  {r[1]:<28} {r[7]}")


if __name__ == "__main__":
    main(recheck="--no-recheck" not in sys.argv)
