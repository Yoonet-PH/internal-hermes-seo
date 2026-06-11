#!/usr/bin/env python3
"""
cron_watchdog.py — daily health check over the Hermes cron jobs (runs as a
--no-agent cron). With no messaging platform on this install, a failing job
would otherwise fail silently forever; this surfaces problems where the team
already looks — an "Ops Health" tab on the Hermes SEO sheet.

Checks per job: paused, last run errored, or stale (no run within ~2x its
schedule interval). Also checks that the config-backup encryption key exists
when a config-backup job is present.

Stdout is empty when everything is healthy (no-agent cron: silent), and one
line per problem otherwise. The Ops Health tab is always rewritten so the
sheet shows a current health board either way.
"""
import datetime
import json
import re
from pathlib import Path

import seo_boss as s

HERMES_HOME = Path.home() / ".hermes"
JOBS = HERMES_HOME / "cron" / "jobs.json"
OPS_TAB = "Ops Health"
OPS_HEADER = ["Checked", "Job", "Name", "Schedule", "Last Run", "Last Status", "Health"]


def max_age_hours(expr):
    """Allowed hours between runs before a job counts as stale: ~2x interval."""
    f = (expr or "").split()
    if len(f) != 5:
        return None
    minute, hour, dom, mon, dow = f
    m = re.fullmatch(r"\*/(\d+)", minute)
    if m and hour == "*":
        return max(2 * int(m.group(1)) / 60, 2)
    if dow != "*":
        return 2 * 24 * 7
    if dom != "*" or mon != "*":
        return 2 * 24 * 31
    if hour != "*":
        return 2 * 24
    return 2


def hours_since(iso):
    try:
        dt = datetime.datetime.fromisoformat(iso)
        now = datetime.datetime.now(dt.tzinfo or datetime.timezone.utc)
        return (now - dt).total_seconds() / 3600
    except Exception:
        return None


def check(job):
    """Return (health, problem) for one job dict."""
    name = job.get("name") or job.get("id", "?")
    if not job.get("enabled", True) or job.get("paused_at"):
        return "PAUSED", f"{name}: paused — resume it or remove it"
    status = (job.get("last_status") or "").lower()
    if status and status not in ("ok", "success"):
        err = (job.get("last_error") or "")[:120]
        return "FAILING", f"{name}: last run {status}{' — ' + err if err else ''}"
    limit = max_age_hours(job.get("schedule", {}).get("expr"))
    ref = job.get("last_run_at") or job.get("created_at")
    age = hours_since(ref) if ref else None
    if limit and age and age > limit:
        return "STALE", (f"{name}: no run for {age / 24:.1f} day(s) "
                         f"(expected roughly every {limit / 2:.0f}h)")
    if (job.get("script") or "") == "config-backup.sh":
        if not (HERMES_HOME / ".config-backup-key").exists():
            return "MISCONFIGURED", (f"{name}: ~/.hermes/.config-backup-key is missing — "
                                     "the weekly backup will fail until it is restored")
    return "OK", None


def main():
    jobs = json.load(open(JOBS))
    jobs = jobs if isinstance(jobs, list) else jobs.get("jobs", list(jobs.values()))
    rows, problems = [], []
    for j in jobs:
        health, problem = check(j)
        if problem:
            problems.append(problem)
        rows.append([
            s.tstr(), j.get("id", ""), j.get("name", ""),
            j.get("schedule", {}).get("expr", ""),
            (j.get("last_run_at") or "never")[:19],
            j.get("last_status") or "", health,
        ])
    s.ensure_tab(OPS_TAB, OPS_HEADER)
    s.update_range(f"'{OPS_TAB}'!A1", [OPS_HEADER] + rows)
    s.SHEETS.values().clear(spreadsheetId=s.SHEET_ID,
                            range=f"'{OPS_TAB}'!A{len(rows) + 2}:G1000").execute()
    for p in problems:
        print(f"CRON HEALTH: {p}")


if __name__ == "__main__":
    main()
