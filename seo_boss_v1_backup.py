#!/usr/bin/env python3
"""
SEO Boss — deterministic sync + state engine for the Hermes SEO department.

Runs each tick (no LLM). It:
  1. Pulls the monitored sites from SE Ranking (PROJECT_listProjects) — the source of truth.
  2. Syncs them into the registry tab of the Google Sheet (adaptive: sites appear/leave,
     per-site task tabs are auto-created).
  3. Reads real health data per site (visibility, top10, avg position, movement).
  4. Works out the ONE next action: AUDIT a site whose audit is due, CHASE overdue team
     tasks, VERIFY tasks the team marked Done, or NONE.
  5. Prints a situation report to stdout for the agent to act on.

The agent (Sonnet) reads this output and does the judgement + writing. This script never
calls an LLM, so idle ticks are nearly free.
"""
import json
import sys
import datetime
import urllib.request
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
SHEET_ID = "1arbNijYAj3iRbLT_FVGKcm7VKzeIclc9iG-b4-1_EGo"
REGISTRY_TAB = "Monitored Sites"
AUDIT_CADENCE_DAYS = 7
MCP_URL = "https://api.seranking.com/mcp"

REGISTRY_HEADER = ["Site", "Domain", "SE Ranking ID", "Keywords",
                   "Visibility %", "In Top 10", "Avg Pos", "Movement",
                   "Last Audited", "Open Tasks", "Next Review"]
TASK_HEADER = ["Date Raised", "Priority", "Finding (evidence)",
               "Action for the team", "Owner", "Due", "Status",
               "Last Chased", "Result"]
OPEN_STATUSES = {"", "to do", "todo", "in progress", "overdue", "escalated"}

# --- google sheets auth (reuse the google-workspace skill's credentials) ---
sys.path.insert(0, str(HERMES_HOME / "skills/productivity/google-workspace/scripts"))
from googleapiclient.discovery import build  # noqa: E402
import google_api as gapi  # noqa: E402

_creds = gapi.get_credentials()
SHEETS = build("sheets", "v4", credentials=_creds).spreadsheets()


def today():
    return datetime.date.today().isoformat()


def serank_key():
    import yaml
    cfg = yaml.safe_load(open(HERMES_HOME / "config.yaml"))
    return cfg["mcp_servers"]["seranking"]["headers"]["X-Api-Key"]


_KEY = serank_key()


def serank(tool, args=None):
    """Call a SE Ranking MCP tool via JSON-RPC over the streamable-HTTP endpoint."""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args or {}},
    }).encode()
    req = urllib.request.Request(MCP_URL, data=body, method="POST", headers={
        "X-Api-Key": _KEY,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    with urllib.request.urlopen(req, timeout=45) as r:
        raw = r.read().decode()
    for line in raw.splitlines():
        line = line[6:].strip() if line.startswith("data:") else line.strip()
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
            "requests": [{"addSheet": {"properties": {"title": title}}}]
        }).execute()
    # always make sure the header row is set
    SHEETS.values().update(
        spreadsheetId=SHEET_ID, range=f"'{title}'!A1",
        valueInputOption="RAW", body={"values": [header]}).execute()


def read_tab(title, rng="A1:Z1000"):
    res = SHEETS.values().get(
        spreadsheetId=SHEET_ID, range=f"'{title}'!{rng}").execute()
    return res.get("values", [])


def write_registry(rows):
    SHEETS.values().update(
        spreadsheetId=SHEET_ID, range=f"'{REGISTRY_TAB}'!A1",
        valueInputOption="RAW",
        body={"values": [REGISTRY_HEADER] + rows}).execute()


def safe_tab_name(title):
    # Google Sheets tab names cannot contain : \ / ? * [ ]
    bad = ':\\/?*[]'
    return "".join(c for c in title if c not in bad).strip()[:90] or "Site"


# --- core ---
def domain_of(project):
    name = (project.get("name") or "").strip()
    return name.replace("https://", "").replace("http://", "").rstrip("/")


def load_prev_registry():
    """Map site_id -> existing registry row dict, to preserve Last Audited."""
    prev = {}
    rows = read_tab(REGISTRY_TAB) if REGISTRY_TAB in tab_titles() else []
    if not rows:
        return prev
    header = rows[0]
    idx = {h: i for i, h in enumerate(header)}
    for r in rows[1:]:
        def g(col):
            i = idx.get(col)
            return r[i] if i is not None and i < len(r) else ""
        sid = g("SE Ranking ID")
        if sid:
            prev[str(sid)] = {"last_audited": g("Last Audited")}
    return prev


def site_task_state(tab):
    """Return (open_count, overdue, done_unverified) for a site's task tab."""
    if tab not in tab_titles():
        return 0, [], []
    rows = read_tab(tab)
    if not rows:
        return 0, [], []
    header = rows[0]
    idx = {h: i for i, h in enumerate(header)}
    open_count, overdue, done_unverified = 0, [], []
    for n, r in enumerate(rows[1:], start=2):
        def g(col):
            i = idx.get(col)
            return (r[i] if i is not None and i < len(r) else "").strip()
        status = g("Status").lower()
        if status in OPEN_STATUSES:
            open_count += 1
            due = g("Due")
            if due and due < today():
                overdue.append({"row": n, "action": g("Action for the team"),
                                "due": due, "owner": g("Owner")})
        elif status == "done" and not g("Result"):
            done_unverified.append({"row": n, "action": g("Action for the team"),
                                    "owner": g("Owner")})
    return open_count, overdue, done_unverified


def main():
    projects = serank("PROJECT_listProjects")
    if not isinstance(projects, list):
        print("NEXT_ACTION: NONE")
        print("Could not read SE Ranking projects.")
        return
    projects = [p for p in projects if p.get("is_active", 1)]

    prev = load_prev_registry()
    ensure_tab(REGISTRY_TAB, REGISTRY_HEADER)

    sites = []
    for p in projects:
        sid = p["id"]
        raw_title = (p.get("title") or "").strip()
        if not raw_title or "://" in raw_title or raw_title.lower().startswith("http"):
            raw_title = domain_of(p) or str(sid)
        title = safe_tab_name(raw_title)
        ensure_tab(title, TASK_HEADER)
        try:
            s = serank("PROJECT_getSummary", {"site_id": sid}) or {}
        except Exception:
            s = {}
        open_count, overdue, done_unver = site_task_state(title)
        last_audited = prev.get(str(sid), {}).get("last_audited", "")
        move = ""
        if s.get("today_avg") is not None and s.get("yesterday_avg") is not None:
            delta = s["yesterday_avg"] - s["today_avg"]  # positive = improved (lower pos)
            move = f"{'+' if delta > 0 else ''}{delta}" if delta else "0"
        sites.append({
            "sid": sid, "title": title, "domain": domain_of(p),
            "keywords": p.get("keyword_count", ""),
            "visibility": s.get("visibility_percent", ""),
            "top10": s.get("top10", ""), "avg": s.get("today_avg", ""),
            "move": move, "last_audited": last_audited,
            "open": open_count, "overdue": overdue, "done_unver": done_unver,
        })

    # write registry
    def next_review(la):
        if not la:
            return today()
        try:
            d = datetime.date.fromisoformat(la) + datetime.timedelta(days=AUDIT_CADENCE_DAYS)
            return d.isoformat()
        except Exception:
            return today()
    write_registry([[
        s["title"], s["domain"], s["sid"], s["keywords"],
        s["visibility"], s["top10"], s["avg"], s["move"],
        s["last_audited"], s["open"], next_review(s["last_audited"]),
    ] for s in sites])

    # decide the one next action
    def audit_due(s):
        if not s["last_audited"]:
            return True
        try:
            d = datetime.date.fromisoformat(s["last_audited"])
            return (datetime.date.today() - d).days >= AUDIT_CADENCE_DAYS
        except Exception:
            return True

    due = sorted([s for s in sites if audit_due(s)],
                 key=lambda s: (s["last_audited"] or "0000"))
    overdue = [(s, t) for s in sites for t in s["overdue"]]
    done_unver = [(s, t) for s in sites for t in s["done_unver"]]

    print(f"# SEO Boss situation report — {today()}")
    print(f"Monitored sites: {len(sites)} (synced from SE Ranking).\n")
    print("| Site | Domain | Vis% | Top10 | AvgPos | Move | Last Audited | Open |")
    print("|---|---|---|---|---|---|---|---|")
    for s in sites:
        print(f"| {s['title']} | {s['domain']} | {s['visibility']} | {s['top10']} "
              f"| {s['avg']} | {s['move']} | {s['last_audited'] or 'never'} | {s['open']} |")
    print()

    if due:
        s = due[0]
        print(f"NEXT_ACTION: AUDIT")
        print(f"SITE: {s['title']}  |  DOMAIN: {s['domain']}  |  SITE_ID: {s['sid']}  "
              f"|  TASK_TAB: {s['title']}")
        print(f"WHY: last audited {s['last_audited'] or 'never'} "
              f"(cadence {AUDIT_CADENCE_DAYS}d).")
        # data bundle for the agent
        try:
            kw = serank("PROJECT_listKeywords", {"site_id": s["sid"]})
            print("\nKEYWORDS (tracked, with target pages):")
            print(json.dumps(kw, indent=1)[:3000])
        except Exception as e:
            print(f"(keyword pull failed: {e})")
        try:
            pot = serank("PROJECT_getSeoPotential", {"site_id": s["sid"]})
            print("\nSEO_POTENTIAL:")
            print(json.dumps(pot, indent=1)[:1200])
        except Exception:
            pass
    elif overdue:
        print("NEXT_ACTION: CHASE")
        print("OVERDUE TASKS (Status still open, Due date passed):")
        for s, t in overdue[:10]:
            print(f"- TAB '{s['title']}' row {t['row']}: \"{t['action']}\" "
                  f"(due {t['due']}, owner {t['owner'] or 'UNASSIGNED'})")
    elif done_unver:
        print("NEXT_ACTION: VERIFY")
        print("TASKS MARKED DONE, AWAITING VERIFICATION:")
        for s, t in done_unver[:10]:
            print(f"- TAB '{s['title']}' row {t['row']}: \"{t['action']}\" "
                  f"(owner {t['owner'] or 'UNASSIGNED'}, site_id {s['sid']})")
    else:
        print("NEXT_ACTION: NONE")
        print("All sites within audit cadence, no overdue tasks, nothing to verify.")


def stamp_audited(site_id):
    """Mark a site's Last Audited = today in the registry (called by the agent
    after it has written the audit tasks)."""
    rows = read_tab(REGISTRY_TAB)
    if not rows:
        return
    idx = {h: i for i, h in enumerate(rows[0])}
    col = idx.get("Last Audited", 8)
    for n, r in enumerate(rows[1:], start=2):
        sid_i = idx.get("SE Ranking ID")
        if sid_i is not None and sid_i < len(r) and str(r[sid_i]) == str(site_id):
            a1 = chr(ord("A") + col)
            SHEETS.values().update(
                spreadsheetId=SHEET_ID, range=f"'{REGISTRY_TAB}'!{a1}{n}",
                valueInputOption="RAW", body={"values": [[today()]]}).execute()
            print(f"stamped {site_id} Last Audited = {today()}")
            return
    print(f"site_id {site_id} not found in registry")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "stamp":
        stamp_audited(sys.argv[2])
    else:
        main()
