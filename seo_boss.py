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

Actions: VERIFY and CHASE are fully deterministic and handled inline every tick
(no LLM): done tasks get their real before/after written from position history,
overdue tasks get stamped and noted. The agent is only invoked for the work that
needs judgement — AUDIT (weekly per site) > EMAIL (monthly, only with something
to report) > NONE.

v3 (the SE Ranking off-ramp, per SE_RANKING_OFFRAMP.md): rank tracking no longer
uses SE Ranking projects/slots. Sites come from the Monitored Sites registry,
headline stats from the Position History tab, and live positions for the one
action site from seo_intel.keyword_intel_v2 (GSC where wired, else DataForSEO).
SE Ranking remains only as the technical site-audit crawler (audit_blockers).
"""
import json
import os
import re
import sys
import datetime
import subprocess
import urllib.request
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
HERMES_BIN = HERMES_HOME / "hermes-agent/venv/bin/hermes"
# Where a balance run-down alert is pushed, and the band we last told Ben about
# (so the ping fires once per crossing, not every 30-minute tick).
DFS_ALERT_TARGET = os.environ.get("DFS_ALERT_TARGET", "telegram")
DFS_ALERT_STATE = HERMES_HOME / "state" / "dfs_balance_alert.json"
SHEET_ID = "1arbNijYAj3iRbLT_FVGKcm7VKzeIclc9iG-b4-1_EGo"
REGISTRY_TAB = "Monitored Sites"
EMAILS_TAB = "Client Emails"
DIGEST_TAB = "Weekly Digest"
AUDIT_CADENCE_DAYS = 7
CLIENT_UPDATE_CADENCE_DAYS = 30
# A live on-page change gets this long to be reflected by Google before the
# verifier is allowed to judge it a failure. Under it, a not-yet-improved change
# sits at "Verifying", not "Need Revision" — done correctly, just not confirmed.
DIGEST_DAYS = 14
# Sub-this-many-place moves are rank jitter, not a regression worth flagging.
NOISE_POSITIONS = 3
MCP_URL = "https://api.seranking.com/mcp"

REGISTRY_HEADER = ["Site", "Domain", "Type", "Brand", "Status", "Repo / Access",
                   "SE Ranking ID", "Keywords", "Visibility %", "In Top 10", "Avg Pos",
                   "Movement", "Last Audited", "Last Client Update", "Open Tasks",
                   "Next Review", "Date Added", "Notes", "Slack Channel"]
# Human-maintained Status values that take a site out of the audit/email rota
# (kept in the registry, but the Boss does no LLM work on them).
SKIP_AUDIT_STATUSES = {"pre-launch", "prelaunch", "parked", "setup needed",
                       "setup-needed", "paused", "inactive", "archived"}
TASK_HEADER = ["Date Raised", "Priority", "Target page", "Finding (evidence)",
               "Recommended action", "Claude Code prompt (paste into Claude Code)",
               "Owner", "Due", "Status", "Result"]
EMAIL_HEADER = ["Date Drafted", "Site", "Subject", "Body (review and send)", "Status"]
OPEN_STATUSES = {"", "to do", "todo", "in progress", "overdue", "escalated",
                 "need revision", "needs revision"}
DONE_STATUSES = {"done", "complete", "completed"}
# "Closed" (team or Boss: recommendation not worth applying, duplicate, disproven)
# and "Verified" sit outside both sets on purpose — the Boss never re-processes
# them. A Closed row keeps its text; the Result column says why it was dropped.
# "Verifying" is a HOLDING state (re-checked every tick like Done): the change is
# made and correct as far as we can tell, but not yet confirmed — Google needs
# time, or the rank feed is down. "Need Revision" is OPEN and stronger: only set
# once the digestion window has passed AND the change measurably went backwards,
# i.e. the work itself looks wrong or incomplete and the owner must revisit it.
# Everything do_verifications writes into Result starts with one of these — a
# Done row whose Result starts differently has NOT been verified yet. The token
# "since YYYY-MM-DD" inside a Verifying note is the measurement clock.
VERIFIED_PREFIXES = ("Worked:", "Held:", "Measuring:", "Feed down:",
                     "Needs rework:", "Gone backwards:", "No movement yet:",
                     "Verified at face value")

sys.path.insert(0, str(HERMES_HOME / "skills/productivity/google-workspace/scripts"))
from googleapiclient.discovery import build  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402
import google_api as gapi  # noqa: E402
import time as _time
import random as _random


def _execute(req, tries=6):
    """Run a Google API request with exponential backoff on transient errors.

    Sheets enforces 60 reads and 60 writes per minute per user; a burst mid-audit
    returns HTTP 429 and, without this, the write is simply lost and the site's
    audit half-written. Retries 429/500/503 with jitter; re-raises anything else
    (and the final attempt) so genuine failures still surface."""
    for i in range(tries):
        try:
            return req.execute()
        except HttpError as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            try:
                status = int(status)
            except (TypeError, ValueError):
                status = None
            if status in (429, 500, 503) and i < tries - 1:
                _time.sleep(min(2 ** i, 30) + _random.uniform(0, 0.75))
                continue
            raise

SHEETS = build("sheets", "v4", credentials=gapi.sa_credentials(
    ["https://www.googleapis.com/auth/spreadsheets"])).spreadsheets()


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
    with urllib.request.urlopen(req, timeout=15) as r:
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
_TAB_TITLES_CACHE = None


def tab_titles(force=False):
    """Tab titles for the workbook, cached for the life of the process. Every
    read_tab()/ensure_tab() used to re-fetch the full spreadsheet metadata,
    which on an 11-site board meant 50+ Sheets reads per build_sites() and blew
    Google's 60-reads/min/user quota (HTTP 429). The set of tabs only changes
    when WE add one, so we fetch once and keep the cache in sync on ensure_tab.
    Pass force=True to invalidate (not normally needed)."""
    global _TAB_TITLES_CACHE
    if force or _TAB_TITLES_CACHE is None:
        meta = _execute(SHEETS.get(spreadsheetId=SHEET_ID))
        _TAB_TITLES_CACHE = [s["properties"]["title"] for s in meta.get("sheets", [])]
    return _TAB_TITLES_CACHE


STATUS_OPTIONS = ["To Do", "In Progress", "Done", "Verifying", "Verified",
                  "Overdue", "Need Revision", "Closed"]


def set_status_validation(sheet_id):
    """Strict dropdown on the Status column (I) so the team can only pick real
    statuses. Unbounded below row 1 so it survives the tab growing."""
    _execute(SHEETS.batchUpdate(spreadsheetId=SHEET_ID, body={"requests": [{
        "setDataValidation": {
            "range": {"sheetId": sheet_id, "startRowIndex": 1,
                      "startColumnIndex": 8, "endColumnIndex": 9},
            "rule": {"condition": {"type": "ONE_OF_LIST",
                                   "values": [{"userEnteredValue": v}
                                              for v in STATUS_OPTIONS]},
                     "strict": True, "showCustomUi": True}}}]}))


def ensure_tab(title, header, write_header=True):
    """Create the tab if missing and put `header` on row 1.

    write_header=False writes the header ONLY when the tab is actually created.
    build_sites() calls this for every site on every 30-minute tick, and
    unconditionally rewriting 26 header rows spent 26 of the 60 writes/min/user
    quota re-doing work that was already correct — the single biggest source of
    the HTTP 429s. Callers that pass write_header=False are responsible for
    noticing a header that has drifted (build_sites does, from its batched read).
    Returns True if the tab was created."""
    created = title not in tab_titles()
    if created:
        resp = _execute(SHEETS.batchUpdate(spreadsheetId=SHEET_ID, body={
            "requests": [{"addSheet": {"properties": {"title": title}}}]}))
        if _TAB_TITLES_CACHE is not None:   # keep cache in sync, no re-fetch
            _TAB_TITLES_CACHE.append(title)
        if header == TASK_HEADER:           # site task boards get the dropdown
            set_status_validation(
                resp["replies"][0]["addSheet"]["properties"]["sheetId"])
    if created or write_header:
        _execute(SHEETS.values().update(spreadsheetId=SHEET_ID, range=f"'{title}'!A1",
                                        valueInputOption="RAW", body={"values": [header]}))
    return created


def read_tab(title, rng="A1:Z100000"):
    if title not in tab_titles():
        return []
    return _execute(SHEETS.values().get(
        spreadsheetId=SHEET_ID, range=f"'{title}'!{rng}")).get("values", [])


def read_tabs(titles, rng="A1:Z100000"):
    """Read many tabs in ONE Sheets call. Returns {title: rows}.

    build_sites() reads one tab per site. At 26 sites that was 26 of the 60
    reads/min/user quota on every 30-minute tick, leaving too little headroom: any
    second reader in the same minute — a manual run, slack-backlog, the tech
    sweep, the digest — tipped the tick into HTTP 429 and lost it silently. Worse,
    the cost grew linearly, so a big enough registry would have failed on its own.
    batchGet collapses the per-site reads into a single request, so a tick now
    costs a handful of reads regardless of how many sites we monitor."""
    known = [t for t in titles if t in tab_titles()]
    out = {t: [] for t in titles}
    if not known:
        return out
    res = _execute(SHEETS.values().batchGet(
        spreadsheetId=SHEET_ID, ranges=[f"'{t}'!{rng}" for t in known]))
    for title, vr in zip(known, res.get("valueRanges", [])):
        out[title] = vr.get("values", [])
    return out


def update_range(rng, values):
    _execute(SHEETS.values().update(spreadsheetId=SHEET_ID, range=rng,
                                    valueInputOption="RAW", body={"values": values}))


def append_rows(tab, rows):
    _execute(SHEETS.values().append(
        spreadsheetId=SHEET_ID, range=f"'{tab}'!A1", valueInputOption="RAW",
        insertDataOption="INSERT_ROWS", body={"values": rows}))


def safe_tab_name(title):
    return "".join(c for c in title if c not in ':\\/?*[]').strip()[:90] or "Site"


def domain_of(p):
    """Domain of an SE Ranking project dict (kept for seed_tracked_keywords.py)."""
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


# --- keyword intelligence (slot-free: GSC / DataForSEO via seo_intel) ---
def keyword_intel(site):
    """Live positions for one site dict — the off-ramp drop-in. Records a
    Position History row per keyword so movement and the verify loop accumulate."""
    import seo_intel
    return seo_intel.keyword_intel_v2(site["domain"], record=True)


def fmt_pos(p):
    return "100+" if not p or p >= 999 else str(p)


def move_label(prev, now):
    if now >= 999:
        return "not in top 100" if prev >= 999 else "dropped out of top 100"
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
def site_task_state(tab, rows=None):
    """Task state for one site tab. Pass `rows` to reuse a batched read (see
    read_tabs) instead of spending another Sheets read here."""
    rows = read_tab(tab) if rows is None else rows
    recs, _ = rows_as_dicts(rows)
    open_tasks, overdue, done_unver, completed_recent = [], [], [], []
    for d in recs:
        # A site tab holds a SECOND table to the right (the Technical Health block
        # the daily sweep writes from column L). Those rows have empty task columns,
        # and since "" is an OPEN status a blank Status made every one of them count
        # as an open task — 55 phantom tasks across the board, inflating the Open
        # Tasks column and, once Slack was wired up, ready to post empty bullets.
        # A row with no date and nothing recommended is not a task.
        if not (d.get("Date Raised", "").strip()
                or d.get("Recommended action", "").strip()):
            continue
        status = d.get("Status", "").strip().lower()
        result = d.get("Result", "").strip()
        if status in OPEN_STATUSES:
            open_tasks.append(d)
            due = d.get("Due", "").strip()
            if due and due < tstr():
                overdue.append(d)
        elif status in DONE_STATUSES or status == "verifying":
            # Status is the only signal: verification flips a row to Verified,
            # so anything still Done is unverified regardless of what's in
            # Result (a chase stamp, or a team note saying what they did —
            # notes are preserved when the result lands). Ben's rule, 16/07.
            # "Verifying" rides here too: it is a Done change still inside its
            # digestion window (or waiting on the feed), re-measured each tick.
            done_unver.append(d)
        elif status in (DONE_STATUSES | {"verified"}):
            dr = d.get("Date Raised", "").strip()
            if dr and dr >= days_ago(CLIENT_UPDATE_CADENCE_DAYS):
                completed_recent.append(d)
    return open_tasks, overdue, done_unver, completed_recent


def verify_result(task, kintel, anchor=None):
    """Match a tracked keyword in the task text and compute real before/after.

    `anchor` is the date the change went live (the measurement start). We take
    the "before" position from the first reading on/after it, so a change is
    judged on what happened AFTER it shipped — not on a slide that was already
    under way when the task was first raised. Falls back to Date Raised until a
    change has an anchor (its first Verifying tick stamps one)."""
    text = " ".join([task.get("Finding (evidence)", ""),
                     task.get("Recommended action", "") + " " + task.get("Target page", "")]).lower()
    anchor_date = anchor or task.get("Date Raised", "").strip()
    best = None
    for k in kintel:
        if k["kw"] and k["kw"].lower() in text:
            if best is None or len(k["kw"]) > len(best["kw"]):
                best = k
    if not best:
        return None
    if str(best.get("source", "")).startswith("error"):
        # The rank feed errored for this keyword (e.g. DataForSEO 402) — pos_now
        # is a placeholder 999, not a measurement. Verifying now would write a
        # false "Gone backwards". Leave the row for a tick with real data.
        return {"skip": True, "kw": best["kw"]}
    pos_then = None
    for d, p in best["series"]:
        if anchor_date and d >= anchor_date:
            pos_then = p
            break
    if pos_then is None:
        pos_then = best["pos_prev"]
    pos_now = best["pos_now"]
    delta = pos_then - pos_now
    return {"kw": best["kw"], "then": pos_then, "now": pos_now, "delta": delta}


# --- main report ---
def _history_by_domain(days=35):
    """{bare_domain: {kw_lower: sorted [(date, pos)]}} from the Position History
    tab, capped to `days`. One Sheet read serves every site's headline stats."""
    import seo_intel
    rows = read_tab(seo_intel.HISTORY_TAB, "A1:F100000")
    recs, _ = rows_as_dicts(rows)
    cutoff = days_ago(days)
    by = {}
    for d in recs:
        dom = _bare_domain(d.get("Domain"))
        kw = (d.get("Keyword") or "").strip().lower()
        date = (d.get("Date") or "").strip()
        if not (dom and kw and date) or date < cutoff:
            continue
        if (d.get("Source") or "").startswith("error"):
            # Failed lookups are recorded as pos 999 for telemetry; counting
            # them here turns a feed outage into a fake mass ranking drop.
            continue
        try:
            pos = int(float(d.get("Position") or 999))
        except ValueError:
            continue
        by.setdefault(dom, {}).setdefault(kw, []).append((date, pos))
    for dom in by.values():
        for series in dom.values():
            series.sort()
    return by


def build_sites():
    """Sites come from the Monitored Sites registry (a spreadsheet row, not an
    SE Ranking slot). Headline stats are reconstructed from Position History —
    no live position calls here; those happen only for the action site."""
    import seo_intel
    ensure_tab(REGISTRY_TAB, REGISTRY_HEADER)
    prev, _ = rows_as_dicts(read_tab(REGISTRY_TAB))
    tracked, _ = rows_as_dicts(read_tab(seo_intel.TRACKED_TAB))
    hist = _history_by_domain()
    sites = []
    seen_domains = set()

    # Resolve the roster first, so every site tab can be read in a single
    # batched call below rather than one Sheets read apiece (see read_tabs).
    roster = []
    for pv in prev:
        domain = (pv.get("Domain") or "").strip()
        title = safe_tab_name((pv.get("Site") or "").strip() or domain)
        if not domain or _bare_domain(domain) in seen_domains:
            continue
        seen_domains.add(_bare_domain(domain))
        ensure_tab(title, TASK_HEADER, write_header=False)
        roster.append((pv, domain, title))
    tab_rows = read_tabs([title for _, _, title in roster])

    # Self-heal a drifted header. ensure_tab no longer rewrites it every tick, so
    # repair it here instead — from rows we have already read, and only when it is
    # genuinely wrong (i.e. after TASK_HEADER changes, not 26 times an hour).
    for title in list(tab_rows):
        rows = tab_rows[title]
        if rows and rows[0][:len(TASK_HEADER)] != TASK_HEADER:
            update_range(f"'{title}'!A1", [TASK_HEADER])
            print(f"# repaired header on '{title}'", file=sys.stderr)

    for pv, domain, title in roster:
        bd = _bare_domain(domain)
        kws = [(t.get("Keyword") or "").strip().lower()
               for t in tracked if _bare_domain(t.get("Domain")) == bd]
        kws = [k for k in kws if k]
        series_map = hist.get(bd, {})
        now_pos, week_pos = [], []
        for k in kws:
            series = series_map.get(k) or []
            if not series:
                continue
            now_pos.append(series[-1][1])
            week = [p for dt, p in series if dt <= days_ago(7)]
            week_pos.append(week[-1] if week else series[0][1])
        ranked = [p for p in now_pos if p < 999]
        top10 = sum(1 for p in now_pos if p <= 10)
        avg = round(sum(ranked) / len(ranked)) if ranked else ""
        move = ""
        deltas = [w - n for n, w in zip(now_pos, week_pos) if n < 999 or w < 999]
        if deltas:
            md = round(sum(deltas) / len(deltas))
            move = f"{'+' if md > 0 else ''}{md}"
        open_tasks, overdue, done_unver, completed = site_task_state(
            title, rows=tab_rows.get(title))
        sites.append({
            "sid": str(pv.get("SE Ranking ID") or "").strip() or bd,
            "title": title, "domain": domain,
            "keywords": len(kws),
            "vis": "", "top10": top10 if now_pos else "",
            "avg": avg, "move": move,
            "last_audited": pv.get("Last Audited", ""),
            "last_email": pv.get("Last Client Update", ""),
            "repo": pv.get("Repo / Access", ""),
            "type": pv.get("Type", ""), "brand": pv.get("Brand", ""),
            "status": pv.get("Status", ""), "notes": pv.get("Notes", ""),
            "slack": pv.get("Slack Channel", ""),
            "added": (pv.get("Date Added") or "").strip() or tstr(),
            "open": len(open_tasks), "open_tasks": open_tasks,
            "overdue": overdue,
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
    ordered = sorted(sites, key=lambda s: (s["title"] or "").strip().lower())
    update_range(f"'{REGISTRY_TAB}'!A1", [REGISTRY_HEADER] + [[
        s["title"], s["domain"], s.get("type", ""), s.get("brand", ""),
        s.get("status", ""), s.get("repo", ""), s["sid"], s["keywords"],
        s["vis"], s["top10"], s["avg"], s["move"], s["last_audited"],
        s["last_email"], s["open"], nxt(s["last_audited"]), s.get("added", ""),
        s.get("notes", ""), s.get("slack", ""),
    ] for s in ordered])
    _execute(SHEETS.values().clear(spreadsheetId=SHEET_ID,
                                   range=f"'{REGISTRY_TAB}'!A{len(sites) + 2}:S1000"))


def _team_note(result):
    """The team-authored portion of a Result cell, stripped of any Boss note, so
    a re-measured row keeps the human's note without it accumulating each tick."""
    r = (result or "").strip()
    if not r:
        return ""
    for pref in ("Overdue since",) + VERIFIED_PREFIXES:
        idx = r.find(f" — {pref}")
        if idx != -1:
            return r[:idx].strip()
    if r.startswith(("Overdue since",) + VERIFIED_PREFIXES):
        return ""
    return r


def _measuring_since(result):
    """The date a change first went live for measurement, parsed from an earlier
    Boss note ('… since YYYY-MM-DD'). None until the first Verifying tick. The
    lookbehind skips a leftover 'Overdue since …' chase stamp, which must NOT be
    mistaken for the measurement clock — that would pre-start the window and let
    the verifier judge a change before Google has had time to reflect it."""
    m = re.search(r"(?<!Overdue )since (\d{4}-\d{2}-\d{2})", result or "")
    return m.group(1) if m else None


def _verdict(t, ki, since):
    """(status, boss_note) for one Done/Verifying task. The heart of Ben's rule:
    Need Revision means the developer's work was wrong or incomplete — never just
    'not confirmed yet'. Anything unconfirmed is Verifying."""
    stamp = since or tstr()
    vr = verify_result(t, ki, anchor=since)
    if vr and vr.get("skip"):
        return ("Verifying",
                f"Feed down: the rank feed is unavailable for '{vr['kw']}', so this "
                f"change cannot be measured yet. It stays Verifying until the feed "
                f"is back — reload DataForSEO (measuring since {stamp}).")
    if not vr:
        return ("Verified",
                "Verified at face value — no single tracked keyword maps to this "
                "change, so there is nothing to measure directly.")
    kw, then, now, delta = vr["kw"], fmt_pos(vr["then"]), fmt_pos(vr["now"]), vr["delta"]
    if delta > 0:
        return ("Verified", f"Worked: '{kw}' moved {then} to {now}.")
    # Not improved yet. Hold at Verifying until the change has had time to land.
    if since is None:
        return ("Verifying",
                f"Measuring: the change is live and '{kw}' is at {now}. Google can "
                f"take up to {DIGEST_DAYS} days to reflect on-page changes — "
                f"measuring since {stamp}.")
    try:
        elapsed = (today() - datetime.date.fromisoformat(since)).days
    except Exception:
        elapsed = 0
    if elapsed < DIGEST_DAYS:
        return ("Verifying",
                f"Measuring: '{kw}' at {now}, {elapsed} of {DIGEST_DAYS} days into "
                f"the window — done and live, too early to judge (since {since}).")
    if delta <= -NOISE_POSITIONS:
        return ("Need Revision",
                f"Needs rework: {elapsed} days on, '{kw}' has gone backwards "
                f"({then} to {now}). The change looks incorrect or incomplete — "
                f"revisit it and set the row back to Done to re-measure (since {since}).")
    return ("Verified",
            f"Held: '{kw}' at {now} after {elapsed} days (was {then} at the start) — "
            f"no material regression, the change did not go backwards.")


def _needs_measure(t, since):
    """Does this row need a live position lookup THIS tick? Freshly-Done rows and
    rows whose digestion window is up do; a change still inside its window is left
    untouched — no API call — which keeps the verify loop off the rank feed for
    the 14 days a change is settling (and keeps DataForSEO spend down)."""
    st = (t.get("Status", "") or "").strip().lower()
    if st in DONE_STATUSES or since is None:
        return True
    try:
        return (today() - datetime.date.fromisoformat(since)).days >= DIGEST_DAYS
    except Exception:
        return True


def do_verifications(sites):
    """Deterministic VERIFY. A team-Done change is measured against Position
    History and moved to one of three states (see the status vocabulary above):
    Verified (it worked, or held past the window), Verifying (correct but not yet
    confirmable), or Need Revision (measurably backwards after the window). A row
    already Verifying and still inside its window is skipped — no lookup, no spend
    — until the window is up."""
    done = []
    for s in sites:
        if not s["done_unver"]:
            continue
        pending = [(t, _measuring_since(t.get("Result", ""))) for t in s["done_unver"]]
        pending = [(t, since) for t, since in pending if _needs_measure(t, since)]
        if not pending:
            continue                       # nothing due — no live lookup for this site
        ki = keyword_intel(s)
        for t, since in pending:
            new_status, boss = _verdict(t, ki, since)
            team = _team_note(t.get("Result", ""))
            result = f"{team} — {boss}" if team else boss
            update_range(f"'{s['title']}'!I{t['_row']}:J{t['_row']}",
                         [[new_status, result]])
            done.append({"site": s["title"], "row": t["_row"], "result": result,
                         "status": new_status,
                         "line": f"'{s['title']}' row {t['_row']}: {new_status} — {boss}"})
    return done


def do_chases(sites):
    """Deterministic CHASE: stamp overdue tasks with a firm note. Skips rows
    already stamped so the tick stays idempotent."""
    done = []
    for s in sites:
        for t in s["overdue"]:
            st = t.get("Status", "").strip().lower()
            res = t.get("Result", "").strip()
            if st == "overdue" and res:
                continue
            note = (f"Overdue since {t.get('Due', '')}. Owner "
                    f"{t.get('Owner', '').strip() or 'UNASSIGNED'}: action this "
                    "today or escalate")
            if st in ("need revision", "needs revision"):
                # Keep the verifier's feedback and the status — a revision
                # request must not be flattened into a generic overdue stamp.
                if "Overdue since" in res:
                    continue
                update_range(f"'{s['title']}'!I{t['_row']}:J{t['_row']}",
                             [["Need Revision", f"{res} — {note}" if res else note]])
                done.append({"site": s["title"], "row": t["_row"], "task": t,
                             "line": f"'{s['title']}' row {t['_row']} (due {t.get('Due', '')})"})
                continue
            update_range(f"'{s['title']}'!I{t['_row']}:J{t['_row']}",
                         [["Overdue", note]])
            done.append({"site": s["title"], "row": t["_row"], "task": t,
                         "line": f"'{s['title']}' row {t['_row']} (due {t.get('Due', '')})"})
    return done


def blocker_covered_by(blocker_name, open_tasks):
    """Row number of an open task that already covers this SE Ranking blocker, else None.

    The agent kept re-raising the same technical blocker every week — the duplicate-title
    error on Balanga was raised on 08/07 and again on 14/07 — because it had to infer the
    overlap from a truncated action string that never mentioned the blocker. Deciding this
    in Python and telling it outright is far more reliable than asking it to notice."""
    words = {w for w in re.findall(r"[a-z]{4,}", (blocker_name or "").lower())
             if w not in {"page", "pages", "http", "with", "have", "that", "this",
                          "code", "codes", "status", "tags", "tag"}}
    if not words:
        return None
    for t in open_tasks:
        hay = ((t.get("Finding (evidence)") or "") + " "
               + (t.get("Recommended action") or "")).lower()
        hits = sum(1 for w in words if w in hay)
        if hits and hits >= max(1, len(words) - 1):   # nearly all key words present
            return t.get("_row")
    return None


def audit_due(s):
    if (s.get("status", "") or "").strip().lower() in SKIP_AUDIT_STATUSES:
        return False
    if not s["last_audited"]:
        return True
    try:
        return (today() - datetime.date.fromisoformat(s["last_audited"])).days >= AUDIT_CADENCE_DAYS
    except Exception:
        return True


def email_due(s):
    if (s.get("status", "") or "").strip().lower() in SKIP_AUDIT_STATUSES:
        return False
    if not s["completed"]:
        return False  # nothing to report yet
    if not s["last_email"]:
        return True
    try:
        return (today() - datetime.date.fromisoformat(s["last_email"])).days >= CLIENT_UPDATE_CADENCE_DAYS
    except Exception:
        return True


def _elem_phrase(element):
    """Distinctive phrase for dedupe: H2 heading text, else the element's first word."""
    e = (element or "").strip()
    if e.lower().startswith("h2") and ":" in e:
        return e.split(":", 1)[1].strip().lower()
    return e.split()[0].lower() if e else ""


def hybrid_audit():
    """Deterministic AUDIT via the hybrid generator: local Gemma writes the on-page
    rewrite tasks, Claude (Opus 4.8) drafts the client email. Picks the same audit-due
    site main() would, writes to the Sheet, and stamps Last Audited. No LLM agent."""
    import seo_hybrid
    sites = build_sites()
    due = sorted([s for s in sites if audit_due(s)], key=lambda s: (s["last_audited"] or "0000"))
    if not due:
        print("NEXT_ACTION: NONE (hybrid) — no site due for audit.")
        return 0
    s = due[0]
    print(f"NEXT_ACTION: AUDIT (hybrid) — SITE: {s['title']} | DOMAIN: {s['domain']}")
    try:
        out = seo_hybrid.run(s["domain"])           # Gemma tasks + Claude email, grounded
    except Exception as e:
        print(f"HYBRID FAILED: generation error for {s['domain']}: {str(e)[:200]}")
        return 1

    # dedupe generated tasks against what's already open on the board
    open_blob = " ".join(
        ((t.get("Recommended action", "") or "") + " " + (t.get("Target page", "") or "")).lower()
        for t in s["open_tasks"])
    target = out["facts"]["url"]
    due_date = (today() + datetime.timedelta(days=5)).isoformat()
    rows, skipped = [], 0
    for t in out["tasks"]:
        phrase = _elem_phrase(t.get("element", ""))
        if phrase and phrase in open_blob:
            skipped += 1
            continue
        rows.append([tstr(), t.get("priority", "Medium"), target,
                     t.get("finding", ""), t.get("action", ""),
                     t.get("claude_code_prompt", ""), "", due_date, "To Do", ""])
    if rows:
        ensure_tab(s["title"], TASK_HEADER)
        append_rows(s["title"], rows)
    print(f"  wrote {len(rows)} task(s) to '{s['title']}'"
          + (f" (skipped {skipped} already-open)" if skipped else ""))

    email = out.get("email", {})
    if email.get("by") in (None, "none", "error") or not email.get("body"):
        print(f"  client email NOT written — {email.get('note', 'no email produced')}")
    else:
        ensure_tab(EMAILS_TAB, EMAIL_HEADER)
        append_rows(EMAILS_TAB, [[tstr(), s["title"], email.get("subject", ""),
                                  email.get("body", ""), "Draft"]])
        print(f"  wrote client email draft (by {email['by']}) to '{EMAILS_TAB}'")

    stamp("Last Audited", s.get("sid") or s["title"])
    return 0


def _dfs_band(bal):
    import seo_intel
    if bal is None:
        return None
    if bal <= seo_intel.DFS_MIN_BALANCE:
        return "out"
    if bal <= seo_intel.DFS_WARN_BALANCE:
        return "low"
    return "ok"


def _dfs_alert(bal):
    """Push Ben one message whenever the balance crosses a band — so a run-down is
    caught early and a reload is CONFIRMED back to us automatically, without anyone
    having to watch the sheet or tell us by hand. Fires once per crossing, never
    every tick, and never fails the tick if the send or state file misbehaves."""
    band = _dfs_band(bal)
    if band is None:
        return
    try:
        prev = json.loads(DFS_ALERT_STATE.read_text()).get("band")
    except Exception:
        prev = None
    if band == prev:
        return
    msg = {
        "out": f"🛑 DataForSEO OUT OF CREDIT (${bal:.2f}). Live rank checks are PAUSED "
               "— reload at https://app.dataforseo.com to resume.",
        "low": f"⚠️ DataForSEO balance is low (${bal:.2f}). Reload soon at "
               "https://app.dataforseo.com before rank checks stop.",
        "ok":  (f"✅ DataForSEO reloaded (${bal:.2f}). Rank checks are running normally "
                "again." if prev in ("low", "out") else None),
    }.get(band)
    # First-ever run with no prior state: only speak up if we start in trouble.
    if prev is None and band == "ok":
        msg = None
    if msg:
        try:
            subprocess.run([str(HERMES_BIN), "send", "-t", DFS_ALERT_TARGET, "-q", msg],
                           timeout=25, check=False)
        except Exception as e:
            print(f"# dfs balance alert send failed ({e})", file=sys.stderr)
    try:
        DFS_ALERT_STATE.parent.mkdir(parents=True, exist_ok=True)
        DFS_ALERT_STATE.write_text(json.dumps({"band": band, "bal": bal, "at": tstr()}))
    except Exception:
        pass


def feed_banner():
    """Loud, self-clearing flag when the rank feed is out of credit. Mirrors the
    state to a single Ops Health row (visible off the tick log) and pushes Ben a
    one-time alert on any balance-band change — the automatic reload monitor."""
    import seo_intel
    bal = seo_intel.dfs_balance()
    down = bal is not None and bal <= seo_intel.DFS_MIN_BALANCE
    if down:
        line = (f"⚠️  DATAFORSEO OUT OF CREDIT (balance ${bal:.2f}) — position checks "
                "are PAUSED to avoid false drops. RELOAD at https://app.dataforseo.com. "
                "Done tasks are held as 'Verifying', no ranking data is being recorded.")
        print(line + "\n")
    _dfs_alert(bal)
    try:
        status = (f"⚠️ OUT OF CREDIT (${bal:.2f}) — reload" if down
                  else (f"OK (${bal:.2f})" if bal is not None else "balance unreadable"))
        _upsert_ops_row("DataForSEO feed", "Rank feed credit", status)
    except Exception as e:
        print(f"# ops-health flag skipped ({e})", file=sys.stderr)
    return down


def _upsert_ops_row(job, name, status):
    """Find-or-append one Ops Health row keyed on Job, so the feed flag updates in
    place and self-clears rather than piling up a line per tick."""
    OPS = "Ops Health"
    ensure_tab(OPS, ["Checked", "Job", "Name", "Schedule", "Last Run", "Last Status"])
    rows = read_tab(OPS)
    target = None
    for i, r in enumerate(rows[1:], start=2):
        if len(r) > 1 and (r[1] or "").strip() == job:
            target = i
            break
    row = [tstr(), job, name, "every tick", tstr(), status]
    if target:
        update_range(f"'{OPS}'!A{target}:F{target}", [row])
    else:
        append_rows(OPS, [row])


def main():
    sites = build_sites()
    write_registry(sites)
    print(f"# SEO Boss situation report — {tstr()}")
    feed_banner()
    print(f"Monitored sites: {len(sites)} (from the Monitored Sites registry).\n")
    print("| Site | Domain | Top10 | AvgPos | 7d Move | Last Audited | Open |")
    print("|---|---|---|---|---|---|---|")
    for s in sites:
        print(f"| {s['title']} | {s['domain']} | {s['top10']} | {s['avg']} "
              f"| {s['move']} | {s['last_audited'] or 'never'} | {s['open']} |")
    print()

    # deterministic housekeeping — every tick, no LLM
    verified = do_verifications(sites)
    chased = do_chases(sites)
    if verified:
        print(f"HOUSEKEEPING — verified {len(verified)} done task(s) from position history:")
        for v in verified:
            print(f"  - {v['line']}")
        print()
    if chased:
        print(f"HOUSEKEEPING — stamped {len(chased)} overdue task(s):")
        for c in chased:
            print(f"  - {c['line']}")
        print()

    # Slack delivery. Sites with an empty "Slack Channel" cell are skipped, so
    # this is a no-op until a channel is filled in. Never fatal: the tick's real
    # job is the sheet, and Slack being down must not fail the cron.
    try:
        import slack_notify
        posted = slack_notify.deliver(sites, verified, chased)
        if posted:
            print(f"SLACK — {len(posted)} delivery action(s):")
            for line in posted:
                print(line)
            print()
    except Exception as e:
        print(f"SLACK — delivery skipped ({e})\n")

    due = sorted([s for s in sites if audit_due(s)], key=lambda s: (s["last_audited"] or "0000"))
    emails = [s for s in sites if email_due(s)]

    if due:
        s = due[0]
        ki = keyword_intel(s)
        print("NEXT_ACTION: AUDIT")
        print(f"SITE: {s['title']} | DOMAIN: {s['domain']} | SITE_ID: {s['sid']} | TASK_TAB: {s['title']}")
        print(f"REPO / ACCESS: {s.get('repo') or 'not recorded — write the Claude Code prompt to be run in the site repo, and detect the platform from the live page if you can'}")
        print(f"HEALTH: top10 {s['top10']}, avg pos {s['avg']}, "
              f"7d move {s['move'] or 'n/a'}. Last audited {s['last_audited'] or 'never'}.")
        if s["open_tasks"]:
            print(f"\nALREADY OPEN ({len(s['open_tasks'])} task(s) on the board) — do NOT raise a "
                  "task that duplicates any of these; only add genuinely new findings:")
            for t in s["open_tasks"][:15]:
                # The FINDING must be shown, not just the action. Row 2 on Balanga read
                # "Rewrite the /business-directory page title…" (an action that sounds
                # like one page) while its finding was "62 pages share the same title"
                # — so the agent could not see it already covered the duplicate-title
                # blocker, and raised it again. Showing only the action hid the overlap.
                print(f"  - ROW {t['_row']} [{t.get('Status', '').strip() or 'To Do'}] "
                      f"{(t.get('Target page', '') or '-')[:60]}")
                print(f"      FINDING: {(t.get('Finding (evidence)', '') or '')[:150]}")
                print(f"      ACTION:  {(t.get('Recommended action', '') or '')[:110]}")
        def feed_error(k):
            return str(k.get("source", "")).startswith("error")
        striking = [k for k in ki if k["striking"] and not feed_error(k)]
        drops = [k for k in ki if k["pos_prev"] < 900 and k["change"] <= -10
                 and not feed_error(k)]
        errored = [k for k in ki if feed_error(k)]
        print("\nKEYWORDS (tracked — position now, 35d movement, landing page):")
        for k in ki[:25]:
            if feed_error(k):
                print(f"  - \"{k['kw']}\": position data UNAVAILABLE this tick "
                      "(rank feed error) — NOT a drop, draw no conclusion from it")
                continue
            flag = "  <-- STRIKING DISTANCE (page 2, quick win)" if k["striking"] else ""
            print(f"  - \"{k['kw']}\": pos {fmt_pos(k['pos_now'])} "
                  f"({move_label(k['pos_prev'], k['pos_now'])})  page: {k['landing'] or '-'}{flag}")
        if errored:
            print(f"\nNOTE: the rank feed errored for {len(errored)} of {len(ki)} keywords "
                  "this tick. Do NOT describe rankings as dropped, collapsed or lost, and "
                  "do NOT raise ranking-drop tasks or mention visibility in the client "
                  "email — there is no position data to support it.")
        if drops:
            print(f"\nURGENT — {len(drops)} keyword(s) dropped 10+ places, investigate why: "
                  + ", ".join(f"'{k['kw']}' ({move_label(k['pos_prev'], k['pos_now'])})" for k in drops))
        if striking:
            print(f"\nPRIORITISE the {len(striking)} striking-distance keywords above — "
                  "small on-page work moves these to page 1 fastest.")
        tech = audit_blockers(s["domain"])
        if tech and tech.get("blockers"):
            print(f"\nTECHNICAL BLOCKERS (SE Ranking site audit — score {tech['score']}/100, "
                  f"{tech['errors']} errors / {tech['warnings']} warnings) — FIX THESE TOO:")
            for sev, name, val, secname in tech["blockers"][:12]:
                # Say outright which blockers an open row already covers, rather than
                # hoping the agent spots it. The same blocker was re-raised week after
                # week because the agent had to infer the overlap for itself.
                # Flag the overlap, but do not forbid outright — the matcher is keyword
                # based and cannot tell "fix the homepage meta description" (1 page) from
                # "build a CMS template for the other 913". Blocking the second would kill
                # a real fix. So: force the agent to justify a second bite, or drop it.
                covered = blocker_covered_by(name, s["open_tasks"])
                flag = (f"\n      <-- ROW {covered} ALREADY COVERS THIS BLOCKER. Do not raise it "
                        f"again unless your task covers a genuinely DIFFERENT scope (different "
                        f"pages). If it does, your finding MUST say how it differs from ROW "
                        f"{covered}. If it does not, raise nothing." if covered else "")
                print(f"  - [{sev.upper()}] {name} — {val} page(s)  ({secname}){flag}")
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
        # The agent used to be handed a "PAGES TO FETCH" list and told to fetch them
        # itself. It didn't — it wrote from a stale memory of the site, and about one
        # task in five was fabricated (see page_facts.py). So we fetch the pages here
        # and hand it the observed values. It cannot invent a title it was never given.
        targets = [home] + [p for p in pages[:4] if p and p != home]
        try:
            import page_facts
            print(page_facts.brief(targets))
        except Exception as e:
            print(f"\nVERIFIED PAGE FACTS: unavailable this tick ({e}).")
            print("Do NOT state any on-page fact (title, meta, H1, canonical, alt text) "
                  "in a finding or a prompt. Raise only tasks that rest on the keyword "
                  "and technical data above, or raise nothing.")
    elif emails:
        s = emails[0]
        ki = keyword_intel(s)
        wins = [k for k in ki if k["change"] > 0][:6]
        print("NEXT_ACTION: EMAIL")
        print(f"SITE: {s['title']} | DOMAIN: {s['domain']}")
        print(f"HEALTH: {s['top10']} keywords in top 10, avg pos {s['avg']}.")
        print("\nWORK COMPLETED THIS PERIOD (team):")
        for t in s["completed"]:
            print(f"  - {t.get('Recommended action','')[:100]}")
        print("\nRANKING WINS (keywords that moved up):")
        for k in wins:
            print(f"  - \"{k['kw']}\": now pos {fmt_pos(k['pos_now'])} (up {k['change']})")
        print("\nDraft a warm, plain client update email to the Client Emails tab.")
    else:
        print("NEXT_ACTION: NONE")
        print("All sites within audit cadence, no client update due. "
              "Verifications and overdue stamps are handled above, deterministically.")


def stamp(field, site_id):
    """Stamp a registry date field (Last Audited / Last Client Update) = today.
    Matches by SE Ranking ID, domain, or site/tab name — new sites have no ID."""
    rows = read_tab(REGISTRY_TAB)
    recs, idx = rows_as_dicts(rows)
    col = idx.get(field)
    if col is None:
        print(f"unknown field {field}")
        return
    key = str(site_id).strip()
    for d in recs:
        if (str(d.get("SE Ranking ID", "")).strip() == key
                or _bare_domain(d.get("Domain")) == _bare_domain(key)
                or (d.get("Site", "") or "").strip().lower() == key.lower()):
            a1 = chr(ord("A") + col)
            update_range(f"'{REGISTRY_TAB}'!{a1}{d['_row']}", [[tstr()]])
            print(f"stamped {field}={tstr()} for {site_id}")
            return
    print(f"site_id {site_id} not found")


def digest_narrative(sites, tot_overdue, tot_done):
    """One-shot Claude summary of the week for Ben and Honey. Returns prose, or ''
    if no API key / the call fails — the caller writes the table regardless, so the
    digest never depends on the LLM being up (this is what the old agent job got
    wrong: it flailed and produced nothing)."""
    try:
        import seo_hybrid
        key = seo_hybrid.anthropic_key()
        if not key:
            return ""
        movers = sorted(sites, key=lambda s: -(int(s["top10"]) if str(s["top10"]).isdigit() else 0))
        lines = "\n".join(
            f"- {s['title']}: avg pos {s['avg']}, {s['top10']} in top 10, 7d move {s['move']}, "
            f"{s['open']} open tasks, {len(s['overdue'])} overdue, {len(s['completed'])} done in 30d"
            for s in movers)
        prompt = (
            "You are the SEO Boss writing this week's oversight note for Ben and Honey (Yoonet). "
            "UK English, plain and direct, no jargon, no bolded headings, no bullet points. "
            f"Portfolio: {len(sites)} monitored sites, {tot_overdue} overdue tasks, "
            f"{tot_done} tasks completed in the last 30 days.\n\nPer site:\n{lines}\n\n"
            "Write 4 to 6 sentences: what moved, where the risk is (overdue/stalled), and the one "
            "thing worth attention next week. Return the prose only, nothing else."
        )
        body = json.dumps({
            "model": seo_hybrid.CLAUDE_MODEL, "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }).encode()
        req = urllib.request.Request(seo_hybrid.CLAUDE_URL, data=body, method="POST", headers={
            "x-api-key": key, "anthropic-version": seo_hybrid.CLAUDE_VERSION,
            "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.load(r)
        return next((b["text"] for b in d.get("content", []) if b.get("type") == "text"), "").strip()
    except Exception as e:
        print(f"  narrative skipped: {str(e)[:160]}")
        return ""


def digest():
    """Write a deterministic weekly digest for owner oversight, with an optional
    Claude narrative. Fully self-contained — no LLM agent, so it cannot flail."""
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

    narrative = digest_narrative(sites, tot_overdue, tot_done)
    if narrative:
        # Stable side panel in column J so it never collides with the table.
        update_range(f"'{DIGEST_TAB}'!J1", [["Weekly Summary"], [narrative]])
        print("  narrative written to J1:J2")

    print(f"Digest written: {len(sites)} sites, {tot_overdue} overdue tasks, "
          f"{tot_done} completed in last 30 days.")


def slack_backlog(only=None):
    """One-off catch-up post per channel: where the site stands today.

    The tick itself is deliberately silent about pre-existing tasks (it seeds
    state on first sight), so this is how a channel gets introduced to its
    standing backlog — once, as a summary, rather than 248 individual posts.
    Also seeds the seen-state, so the next tick announces only genuinely new work.

    `only` limits it to one site (by tab name). Safe to re-run: it re-posts the
    summary, it does not duplicate task announcements."""
    import slack_notify
    sites = build_sites()
    sent = 0
    for s in sites:
        channel = (s.get("slack") or "").strip()
        if not channel or (only and s["title"] != only):
            continue
        msg = slack_notify.backlog_msg(s["title"], s["open_tasks"], s["overdue"])
        if slack_notify.post(channel, msg):
            sent += 1
            print(f"posted backlog for '{s['title']}' -> {channel} "
                  f"({len(s['open_tasks'])} open, {len(s['overdue'])} overdue)")
        state = slack_notify.load_state()
        state[s["title"]] = sorted(slack_notify.task_key(t) for t in s["open_tasks"])
        slack_notify.save_state(state)
    if not sent:
        print("no sites posted — is the 'Slack Channel' column filled in? "
              "(set SLACK_DRY_RUN=1 to preview without a token)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "slack-test":
        # slack-test <channel>  — prove the token and the channel invite work
        import slack_notify
        if not slack_notify.enabled() and os.environ.get("SLACK_DRY_RUN") != "1":
            sys.exit("no SLACK_BOT_TOKEN in env or ~/.hermes/.env")
        ch = sys.argv[2] if len(sys.argv) >= 3 else ""
        ok = slack_notify.post(ch, ":satellite: Hermes SEO Boss can post here. "
                                   "Site updates will follow.")
        print("posted" if ok else "FAILED — see the error above")
        sys.exit(0 if ok else 1)
    elif len(sys.argv) >= 2 and sys.argv[1] == "slack-backlog":
        sys.exit(slack_backlog(sys.argv[2] if len(sys.argv) >= 3 else None))
    elif len(sys.argv) >= 3 and sys.argv[1] == "stamp":
        stamp("Last Audited", sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "stamp-email":
        stamp("Last Client Update", sys.argv[2])
    elif len(sys.argv) >= 2 and sys.argv[1] == "digest":
        digest()
    elif len(sys.argv) >= 2 and sys.argv[1] == "hybrid":
        sys.exit(hybrid_audit())
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
