#!/usr/bin/env python3
"""
SEO Boss v2 — deterministic sync + state + intelligence engine for the Hermes
SEO department. Runs each tick (no LLM). It decides the ONE next action and hands
the agent a rich, specific brief; the agent only supplies judgement and writing.

v2 adds, over v1:
  - Keyword intelligence: real per-keyword positions + 7/30-day movement, with
    "striking distance" (page-2) flags, so audits cite specifics not a summary.
  - A closed verification loop: when the team marks a task Done, the script matches
    the tracked keyword in the row, computes the actual before/after from position
    history, and the agent writes the real result.
  - Monthly client-update emails: when a site is due an update AND has something to
    report, the script gathers the month's wins and the agent drafts the email.
  - A weekly digest (`seo_boss_next.py digest`) for owner oversight.

Actions, in priority order: AUDIT (weekly per site) > VERIFY (team-done tasks) >
EMAIL (monthly, only with something to report) > CHASE (overdue) > NONE.
"""
import json
import sys
import datetime
import urllib.request
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
SHEET_ID = "1arbNijYAj3iRbLT_FVGKcm7VKzeIclc9iG-b4-1_EGo"
REGISTRY_TAB = "Monitored Sites"
EMAILS_TAB = "Client Emails"
DIGEST_TAB = "Weekly Digest"
AUDIT_CADENCE_DAYS = 7
CLIENT_UPDATE_CADENCE_DAYS = 30
MCP_URL = "https://api.seranking.com/mcp"

REGISTRY_HEADER = ["Site", "Domain", "Repo / Access", "SE Ranking ID", "Keywords",
                   "Visibility %", "In Top 10", "Avg Pos", "Movement",
                   "Last Audited", "Last Client Update", "Open Tasks", "Next Review"]
TASK_HEADER = ["Date Raised", "Priority", "Target page", "Finding (evidence)",
               "Recommended action", "Claude Code prompt (paste into Claude Code)",
               "Owner", "Due", "Status", "Result"]
EMAIL_HEADER = ["Date Drafted", "Site", "Subject", "Body (review and send)", "Status"]
OPEN_STATUSES = {"", "to do", "todo", "in progress", "overdue", "escalated"}
DONE_STATUSES = {"done", "complete", "completed"}

sys.path.insert(0, str(HERMES_HOME / "skills/productivity/google-workspace/scripts"))
from googleapiclient.discovery import build  # noqa: E402
import google_api as gapi  # noqa: E402

SHEETS = build("sheets", "v4", credentials=gapi.get_credentials()).spreadsheets()


def today():
    return datetime.date.today()


def tstr():
    return today().isoformat()


def days_ago(n):
    return (today() - datetime.timedelta(days=n)).isoformat()


def serank_key():
    import yaml
    cfg = yaml.safe_load(open(HERMES_HOME / "config.yaml"))
    return cfg["mcp_servers"]["seranking"]["headers"]["X-Api-Key"]


_KEY = serank_key()


def serank(tool, args=None):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": tool, "arguments": args or {}}}).encode()
    req = urllib.request.Request(MCP_URL, data=body, method="POST", headers={
        "X-Api-Key": _KEY, "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"})
    with urllib.request.urlopen(req, timeout=50) as r:
        raw = r.read().decode()
    for line in raw.splitlines():
        line = line[5:].strip() if line.startswith("data:") else line.strip()
        if not line.startswith("{"):
            continue
        d = json.loads(line)
        if "error" in d:
            raise RuntimeError(f"{tool}: {d['error']}")
        for c in d.get("result", {}).get("content", []):
            if c.get("type") == "text":
                try:
                    return json.loads(c["text"])
                except Exception:
                    return c["text"]
    return None


# --- sheet helpers ---
def tab_titles():
    meta = SHEETS.get(spreadsheetId=SHEET_ID).execute()
    return [s["properties"]["title"] for s in meta.get("sheets", [])]


def ensure_tab(title, header):
    if title not in tab_titles():
        SHEETS.batchUpdate(spreadsheetId=SHEET_ID, body={
            "requests": [{"addSheet": {"properties": {"title": title}}}]}).execute()
    SHEETS.values().update(spreadsheetId=SHEET_ID, range=f"'{title}'!A1",
                           valueInputOption="RAW", body={"values": [header]}).execute()


def read_tab(title, rng="A1:Z1000"):
    if title not in tab_titles():
        return []
    return SHEETS.values().get(
        spreadsheetId=SHEET_ID, range=f"'{title}'!{rng}").execute().get("values", [])


def update_range(rng, values):
    SHEETS.values().update(spreadsheetId=SHEET_ID, range=rng,
                           valueInputOption="RAW", body={"values": values}).execute()


def append_rows(tab, rows):
    SHEETS.values().append(
        spreadsheetId=SHEET_ID, range=f"'{tab}'!A1", valueInputOption="RAW",
        insertDataOption="INSERT_ROWS", body={"values": rows}).execute()


def safe_tab_name(title):
    return "".join(c for c in title if c not in ':\\/?*[]').strip()[:90] or "Site"


def domain_of(p):
    return (p.get("name") or "").strip().replace("https://", "").replace("http://", "").rstrip("/")


def rows_as_dicts(rows):
    if not rows:
        return [], {}
    header = rows[0]
    idx = {h: i for i, h in enumerate(header)}
    out = []
    for n, r in enumerate(rows[1:], start=2):
        d = {h: (r[idx[h]] if idx[h] < len(r) else "") for h in header}
        d["_row"] = n
        out.append(d)
    return out, idx


# --- keyword intelligence ---
def keyword_intel(site_id):
    """Return [{kw, pos_now, pos_prev, change, landing, striking, raised_pos_fn}]
    using listKeywords (text) joined to getKeywordStats (positions over 35 days)."""
    names = {}
    try:
        for kw in (serank("PROJECT_listKeywords", {"site_id": site_id}) or []):
            names[str(kw.get("id"))] = {"name": kw.get("name", ""), "link": kw.get("link")}
    except Exception:
        pass
    out = []
    try:
        stats = serank("PROJECT_getKeywordStats", {
            "site_id": site_id, "date_from": days_ago(35), "date_to": tstr(),
            "with_landing_pages": "1"}) or []
    except Exception:
        stats = []
    for engine in stats if isinstance(stats, list) else []:
        for kw in engine.get("keywords", []):
            kid = str(kw.get("id"))
            series = [(p.get("date"), p.get("pos", 0)) for p in kw.get("positions", [])]
            series = [(d, (p if p else 999)) for d, p in series if d]
            series.sort()
            pos_now = series[-1][1] if series else 999
            pos_prev = series[0][1] if len(series) > 1 else pos_now
            landing = ""
            lp = kw.get("landing_pages") or kw.get("landing_page")
            if isinstance(lp, list) and lp:
                landing = lp[-1].get("url", "") if isinstance(lp[-1], dict) else str(lp[-1])
            elif isinstance(lp, str):
                landing = lp
            out.append({
                "id": kid, "kw": names.get(kid, {}).get("name", ""),
                "pos_now": pos_now, "pos_prev": pos_prev,
                "change": pos_prev - pos_now,  # +ve = improved (moved up)
                "landing": landing or (names.get(kid, {}).get("link") or ""),
                "striking": 11 <= pos_now <= 20,
                "series": series,
            })
    out = [k for k in out if k["kw"]]
    out.sort(key=lambda k: (k["pos_now"] if k["pos_now"] else 999))
    return out


def fmt_pos(p):
    return "100+" if not p or p >= 999 else str(p)


def move_label(prev, now):
    if now >= 999:
        return "dropped out of top 100"
    if prev >= 999:
        return f"new entry at {now}"
    diff = prev - now
    if diff > 0:
        return f"up {diff}"
    if diff < 0:
        return f"down {-diff}"
    return "no change"


# --- technical audit blockers (SE Ranking site audit -> AUDIT brief) ---
AUDIT_SEV = {"error": 0, "warning": 1, "notice": 2, "passed": 3}


def _bare_domain(d):
    d = (d or "").replace("https://", "").replace("http://", "").rstrip("/").lower()
    return d[4:] if d.startswith("www.") else d


def audit_blockers(domain):
    """Best SE Ranking site audit for a domain -> errors+warnings with page counts.
    Matches by domain (createAudit audits come back unlinked, site_id None).
    Mirrors seo_tech.py's extraction so the boss audits on the same data the
    daily Tech Health sweep shows on each tab."""
    bd = _bare_domain(domain)
    if not bd:
        return None
    res = serank("PROJECT_listAudits") or {}
    items = res.get("items", res if isinstance(res, list) else [])
    best = None
    for a in items:
        if isinstance(a, dict) and _bare_domain(a.get("url")) == bd:
            rank = (1 if a.get("status") == "finished" else 0, int(a.get("id") or 0))
            if best is None or rank > best[0]:
                best = (rank, a)
    if best is None:
        return None
    try:
        rep = serank("PROJECT_getAuditReport", {"audit_id": int(best[1].get("id"))}) or {}
    except Exception:
        return {"score": "", "errors": "", "warnings": "", "blockers": []}
    blockers = []
    for sec in rep.get("sections", []) or []:
        secname = sec.get("name", sec.get("uid", ""))
        for code, prop in (sec.get("props", {}) or {}).items():
            if isinstance(prop, dict):
                st = (prop.get("status") or "").lower()
                val = prop.get("value") or 0
                if st in ("error", "warning") and isinstance(val, (int, float)) and val > 0:
                    blockers.append((st, prop.get("name", code), int(val), secname))
    blockers.sort(key=lambda b: (AUDIT_SEV.get(b[0], 9), -b[2]))
    return {"score": rep.get("score_percent", ""), "errors": rep.get("total_errors", 0),
            "warnings": rep.get("total_warnings", 0), "blockers": blockers}


# --- per-site task state ---
def site_task_state(tab):
    rows = read_tab(tab)
    recs, _ = rows_as_dicts(rows)
    open_count, overdue, done_unver, completed_recent = 0, [], [], []
    for d in recs:
        status = d.get("Status", "").strip().lower()
        if status in OPEN_STATUSES:
            open_count += 1
            due = d.get("Due", "").strip()
            if due and due < tstr():
                overdue.append(d)
        elif status in DONE_STATUSES and not d.get("Result", "").strip():
            done_unver.append(d)
        elif status in (DONE_STATUSES | {"verified"}):
            dr = d.get("Date Raised", "").strip()
            if dr and dr >= days_ago(CLIENT_UPDATE_CADENCE_DAYS):
                completed_recent.append(d)
    return open_count, overdue, done_unver, completed_recent


def verify_result(task, kintel):
    """Match a tracked keyword in the task text and compute real before/after."""
    text = " ".join([task.get("Finding (evidence)", ""),
                     task.get("Recommended action", "") + " " + task.get("Target page", "")]).lower()
    raised = task.get("Date Raised", "").strip()
    best = None
    for k in kintel:
        if k["kw"] and k["kw"].lower() in text:
            if best is None or len(k["kw"]) > len(best["kw"]):
                best = k
    if not best:
        return None
    pos_then = None
    for d, p in best["series"]:
        if raised and d >= raised:
            pos_then = p
            break
    if pos_then is None:
        pos_then = best["pos_prev"]
    pos_now = best["pos_now"]
    delta = pos_then - pos_now
    return {"kw": best["kw"], "then": pos_then, "now": pos_now, "delta": delta}


# --- main report ---
def build_sites():
    projects = [p for p in (serank("PROJECT_listProjects") or []) if p.get("is_active", 1)]
    prev, _ = rows_as_dicts(read_tab(REGISTRY_TAB))
    prevmap = {str(d.get("SE Ranking ID")): d for d in prev}
    ensure_tab(REGISTRY_TAB, REGISTRY_HEADER)
    sites = []
    for p in projects:
        sid = p["id"]
        raw = (p.get("title") or "").strip()
        if not raw or "://" in raw or raw.lower().startswith("http"):
            raw = domain_of(p) or str(sid)
        title = safe_tab_name(raw)
        ensure_tab(title, TASK_HEADER)
        try:
            s = serank("PROJECT_getSummary", {"site_id": sid}) or {}
        except Exception:
            s = {}
        open_count, overdue, done_unver, completed = site_task_state(title)
        pv = prevmap.get(str(sid), {})
        move = ""
        if s.get("today_avg") is not None and s.get("yesterday_avg") is not None:
            delta = s["yesterday_avg"] - s["today_avg"]
            move = f"{'+' if delta > 0 else ''}{delta}"
        sites.append({
            "sid": sid, "title": title, "domain": domain_of(p),
            "keywords": p.get("keyword_count", ""),
            "vis": s.get("visibility_percent", ""), "top10": s.get("top10", ""),
            "avg": s.get("today_avg", ""), "move": move,
            "last_audited": pv.get("Last Audited", ""),
            "last_email": pv.get("Last Client Update", ""),
            "repo": pv.get("Repo / Access", ""),
            "open": open_count, "overdue": overdue,
            "done_unver": done_unver, "completed": completed,
        })
    return sites


def write_registry(sites):
    def nxt(la):
        try:
            return (datetime.date.fromisoformat(la) +
                    datetime.timedelta(days=AUDIT_CADENCE_DAYS)).isoformat() if la else tstr()
        except Exception:
            return tstr()
    update_range(f"'{REGISTRY_TAB}'!A1", [REGISTRY_HEADER] + [[
        s["title"], s["domain"], s.get("repo", ""), s["sid"], s["keywords"],
        s["vis"], s["top10"], s["avg"], s["move"], s["last_audited"],
        s["last_email"], s["open"], nxt(s["last_audited"]),
    ] for s in sites])


def audit_due(s):
    if not s["last_audited"]:
        return True
    try:
        return (today() - datetime.date.fromisoformat(s["last_audited"])).days >= AUDIT_CADENCE_DAYS
    except Exception:
        return True


def email_due(s):
    if not s["completed"]:
        return False  # nothing to report yet
    if not s["last_email"]:
        return True
    try:
        return (today() - datetime.date.fromisoformat(s["last_email"])).days >= CLIENT_UPDATE_CADENCE_DAYS
    except Exception:
        return True


def main():
    sites = build_sites()
    write_registry(sites)
    print(f"# SEO Boss situation report — {tstr()}")
    print(f"Monitored sites: {len(sites)} (synced from SE Ranking).\n")
    print("| Site | Domain | Vis% | Top10 | AvgPos | Move | Last Audited | Open |")
    print("|---|---|---|---|---|---|---|---|")
    for s in sites:
        print(f"| {s['title']} | {s['domain']} | {s['vis']} | {s['top10']} | {s['avg']} "
              f"| {s['move']} | {s['last_audited'] or 'never'} | {s['open']} |")
    print()

    due = sorted([s for s in sites if audit_due(s)], key=lambda s: (s["last_audited"] or "0000"))
    verify = [s for s in sites if s["done_unver"]]
    emails = [s for s in sites if email_due(s)]
    chase = [s for s in sites if s["overdue"]]

    if due:
        s = due[0]
        ki = keyword_intel(s["sid"])
        print("NEXT_ACTION: AUDIT")
        print(f"SITE: {s['title']} | DOMAIN: {s['domain']} | SITE_ID: {s['sid']} | TASK_TAB: {s['title']}")
        print(f"REPO / ACCESS: {s.get('repo') or 'not recorded — write the Claude Code prompt to be run in the site repo, and detect the platform from the live page if you can'}")
        print(f"HEALTH: visibility {s['vis']}%, top10 {s['top10']}, avg pos {s['avg']}, "
              f"7d move {s['move']}. Last audited {s['last_audited'] or 'never'}.")
        striking = [k for k in ki if k["striking"]]
        drops = [k for k in ki if k["pos_prev"] < 900 and k["change"] <= -10]
        print("\nKEYWORDS (tracked — position now, 35d movement, landing page):")
        for k in ki[:25]:
            flag = "  <-- STRIKING DISTANCE (page 2, quick win)" if k["striking"] else ""
            print(f"  - \"{k['kw']}\": pos {fmt_pos(k['pos_now'])} "
                  f"({move_label(k['pos_prev'], k['pos_now'])})  page: {k['landing'] or '-'}{flag}")
        if drops:
            print(f"\nURGENT — {len(drops)} keyword(s) dropped 10+ places, investigate why: "
                  + ", ".join(f"'{k['kw']}' ({move_label(k['pos_prev'], k['pos_now'])})" for k in drops))
        if striking:
            print(f"\nPRIORITISE the {len(striking)} striking-distance keywords above — "
                  "small on-page work moves these to page 1 fastest.")
        try:
            pot = serank("PROJECT_getSeoPotential", {"site_id": s["sid"]})
            print("\nSEO_POTENTIAL:", json.dumps(pot)[:500])
        except Exception:
            pass
        tech = audit_blockers(s["domain"])
        if tech and tech.get("blockers"):
            print(f"\nTECHNICAL BLOCKERS (SE Ranking site audit — score {tech['score']}/100, "
                  f"{tech['errors']} errors / {tech['warnings']} warnings) — FIX THESE TOO:")
            for sev, name, val, secname in tech["blockers"][:12]:
                print(f"  - [{sev.upper()}] {name} — {val} page(s)  ({secname})")
            print("Each blocker is a candidate task: where REPO / ACCESS names a repo, write a "
                  "ready-to-run Claude Code prompt to fix it there; where it says 'No repo / SEO "
                  "only', write 'Not a code change — ' + a client recommendation. Errors before warnings.")
        elif tech is not None:
            print(f"\nTECHNICAL: site audit score {tech.get('score', '')}/100 — no blocking errors or warnings.")
        pages, seen = [], set()
        for k in ki:
            lp = k["landing"]
            if lp and lp not in seen:
                seen.add(lp)
                pages.append(lp)
        home = s["domain"] if s["domain"].startswith("http") else f"https://{s['domain']}"
        print("\nPAGES TO FETCH before writing (read the live HTML so prompts cite the real "
              "current <title>, meta description and H1):")
        print(f"  - {home}")
        for pg in pages[:4]:
            print(f"  - {pg}")
    elif verify:
        s = verify[0]
        ki = keyword_intel(s["sid"])
        print("NEXT_ACTION: VERIFY")
        print(f"SITE: {s['title']} | TASK_TAB: {s['title']}")
        print("Tasks the team marked Done — with the REAL before/after computed from SE Ranking:")
        for t in s["done_unver"]:
            vr = verify_result(t, ki)
            if vr:
                print(f"- ROW {t['_row']}: \"{t['Recommended action'][:80]}\" | "
                      f"keyword '{vr['kw']}' was {fmt_pos(vr['then'])}, now {fmt_pos(vr['now'])} "
                      f"({move_label(vr['then'], vr['now'])})")
            else:
                print(f"- ROW {t['_row']}: \"{t['Recommended action'][:80]}\" | "
                      f"no single tracked keyword to measure — confirm at face value, "
                      f"re-check at next audit")
    elif emails:
        s = emails[0]
        ki = keyword_intel(s["sid"])
        wins = [k for k in ki if k["change"] > 0][:6]
        print("NEXT_ACTION: EMAIL")
        print(f"SITE: {s['title']} | DOMAIN: {s['domain']}")
        print(f"HEALTH: visibility {s['vis']}%, {s['top10']} keywords in top 10, avg pos {s['avg']}.")
        print("\nWORK COMPLETED THIS PERIOD (team):")
        for t in s["completed"]:
            print(f"  - {t.get('Recommended action','')[:100]}")
        print("\nRANKING WINS (keywords that moved up):")
        for k in wins:
            print(f"  - \"{k['kw']}\": now pos {fmt_pos(k['pos_now'])} (up {k['change']})")
        print("\nDraft a warm, plain client update email to the Client Emails tab.")
    elif chase:
        print("NEXT_ACTION: CHASE")
        for s in chase:
            for t in s["overdue"]:
                print(f"- TAB '{s['title']}' ROW {t['_row']}: \"{t['Recommended action'][:80]}\" "
                      f"(due {t['Due']}, owner {t.get('Owner') or 'UNASSIGNED'})")
    else:
        print("NEXT_ACTION: NONE")
        print("All sites within audit cadence, nothing to verify, no client update due, no overdue tasks.")


def stamp(field, site_id):
    """Stamp a registry date field (Last Audited / Last Client Update) = today."""
    rows = read_tab(REGISTRY_TAB)
    recs, idx = rows_as_dicts(rows)
    col = idx.get(field)
    if col is None:
        print(f"unknown field {field}")
        return
    for d in recs:
        if str(d.get("SE Ranking ID")) == str(site_id):
            a1 = chr(ord("A") + col)
            update_range(f"'{REGISTRY_TAB}'!{a1}{d['_row']}", [[tstr()]])
            print(f"stamped {field}={tstr()} for {site_id}")
            return
    print(f"site_id {site_id} not found")


def digest():
    """Write a deterministic weekly digest for owner oversight."""
    sites = build_sites()
    ensure_tab(DIGEST_TAB, ["Generated", "Site", "Avg Pos", "Top10",
                            "7d Move", "Open Tasks", "Overdue", "Done (30d)"])
    rows = []
    movers = sorted(sites, key=lambda s: -(int(s["top10"]) if str(s["top10"]).isdigit() else 0))
    for s in movers:
        rows.append([tstr(), s["title"], s["avg"], s["top10"], s["move"],
                     s["open"], len(s["overdue"]), len(s["completed"])])
    update_range(f"'{DIGEST_TAB}'!A1",
                 [["Generated", "Site", "Avg Pos", "Top10", "7d Move",
                   "Open Tasks", "Overdue", "Done (30d)"]] + rows)
    tot_overdue = sum(len(s["overdue"]) for s in sites)
    tot_done = sum(len(s["completed"]) for s in sites)
    print(f"Digest written: {len(sites)} sites, {tot_overdue} overdue tasks, "
          f"{tot_done} completed in last 30 days.")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "stamp":
        stamp("Last Audited", sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "stamp-email":
        stamp("Last Client Update", sys.argv[2])
    elif len(sys.argv) >= 2 and sys.argv[1] == "digest":
        digest()
    elif len(sys.argv) >= 4 and sys.argv[1] == "addtasks":
        # addtasks "<tab>" <jsonfile>  — jsonfile = list of 10-col rows
        rows = json.load(open(sys.argv[3]))
        ensure_tab(sys.argv[2], TASK_HEADER)
        append_rows(sys.argv[2], rows)
        print(f"appended {len(rows)} task(s) to '{sys.argv[2]}'")
    elif len(sys.argv) >= 3 and sys.argv[1] == "addemail":
        # addemail <jsonfile>  — jsonfile = one 5-col row [date,site,subject,body,status]
        row = json.load(open(sys.argv[2]))
        ensure_tab(EMAILS_TAB, EMAIL_HEADER)
        append_rows(EMAILS_TAB, row if isinstance(row[0], list) else [row])
        print("appended client email draft")
    else:
        main()
