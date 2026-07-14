#!/usr/bin/env python3
"""slack_notify.py — push-only Slack delivery for the SEO Boss.

WHY A BOT TOKEN AND NOT CLAUDE TAG
Claude Tag is reactive: a human tags @Claude in a channel and it answers. Nothing
on this machine can make it post — there is no external trigger, no webhook, no
API. The Boss is a cron that decides, unprompted, that a site needs attention, so
the *announcing* half has to be a Slack bot token we hold. Claude Tag remains the
plan for the *answering* half inside these same channels (per-channel Access
bundles, Drive access to this very sheet), once the channels exist and carry
traffic.

WHAT THIS IS
No LLM, no agent loop, no cost. Three deterministic triggers, all detected inside
the Boss tick:

  1. NEW TASKS   — task rows that appeared on a site tab since the last tick.
                   Diffed against a local state file rather than reported by the
                   audit agent, because the agent writes its rows *after*
                   seo_boss.py has already exited. Diffing also catches rows added
                   by hybrid mode or typed straight into the sheet by a human.
  2. OVERDUE     — rows do_chases() newly stamped. It skips already-stamped rows,
                   so what it returns is exactly "newly overdue", already
                   idempotent.
  3. VERIFIED    — rows do_verifications() closed off with a real before/after.

SAFETY VALVES (the rollout depends on these)
  - A site whose "Slack Channel" registry cell is empty gets NO Slack delivery.
    That is byte-for-byte the pre-Slack behaviour, so the column can be filled in
    one site at a time and an unhappy site can be silenced by clearing one cell.
  - No token configured => every post is a silent no-op. The Boss tick is
    unaffected and cannot fail because of Slack.
  - The FIRST tick that sees a given site seeds its state WITHOUT posting. Going
    live must not fire 248 backlog tasks into the channels. Use the deliberate
    `slack-backlog` command for a one-off catch-up summary instead.
  - SLACK_DRY_RUN=1 prints what would be posted and sends nothing.
"""
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

HERMES_HOME = Path.home() / ".hermes"
STATE_PATH = HERMES_HOME / "state" / "slack_seen.json"
SHEET_URL = ("https://docs.google.com/spreadsheets/d/"
             "1arbNijYAj3iRbLT_FVGKcm7VKzeIclc9iG-b4-1_EGo/edit")
POST_URL = "https://slack.com/api/chat.postMessage"


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #
TOKEN_VARS = ("SEO_SLACK_BOT_TOKEN", "SLACK_BOT_TOKEN")


def token():
    """Bot token (xoxb-) from the environment, falling back to ~/.hermes/.env.

    SEO_SLACK_BOT_TOKEN is preferred and is what the Boss should be given. The
    gateway watches SLACK_BOT_TOKEN and, seeing it, will try to bring up the
    interactive Slack adapter on every restart — which needs SLACK_APP_TOKEN for
    Socket Mode, fails without it, and would quietly go live workspace-wide the
    day somebody added one. Under its own name the push layer is inert to the
    gateway: it can speak, and it cannot listen. SLACK_BOT_TOKEN is still read as
    a fallback for the day we deliberately run both.

    A COMMENTED line does not count (.env ships a commented placeholder); picking
    that up would mean posting against a dead token instead of cleanly no-opping.
    """
    for var in TOKEN_VARS:
        tok = os.environ.get(var)
        if tok:
            return tok
    try:
        env = (HERMES_HOME / ".env").read_text()
    except Exception:
        return None
    for var in TOKEN_VARS:
        m = re.search(rf"^\s*{var}=(.+)$", env, re.M)
        if m:
            return m.group(1).strip().strip('"').strip("'") or None
    return None


def enabled():
    return bool(token())


# --------------------------------------------------------------------------- #
# posting
# --------------------------------------------------------------------------- #
def post(channel, text):
    """Post to a channel. Returns True on success.

    Never raises: a Slack outage, a bad channel id or a revoked token must not
    take down the Boss tick, whose real job is the sheet."""
    channel = (channel or "").strip()
    if not channel:
        return False
    if os.environ.get("SLACK_DRY_RUN") == "1":
        print(f"\n--- SLACK DRY RUN -> {channel} ---\n{text}\n--- end ---")
        return True
    tok = token()
    if not tok:
        return False
    body = json.dumps({
        "channel": channel,
        "text": text,
        "unfurl_links": False,
        "unfurl_media": False,
    }).encode()
    req = urllib.request.Request(POST_URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json; charset=utf-8",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.loads(r.read().decode())
    except Exception as e:
        print(f"[slack] post failed ({channel}): {e}")
        return False
    if not res.get("ok"):
        # not_in_channel is the one everybody hits: the bot must be invited to a
        # private channel before it can speak in it.
        print(f"[slack] refused ({channel}): {res.get('error')}")
        return False
    return True


# --------------------------------------------------------------------------- #
# state — what we have already announced
# --------------------------------------------------------------------------- #
def load_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def task_key(t):
    """Stable identity for a task row: its row number plus a hash of the finding.

    Row number alone would re-announce a task if a row above it were deleted; the
    finding hash alone would miss two genuinely different tasks sharing wording.
    Together they are stable under the only edit that actually happens (appends)."""
    finding = (t.get("Finding (evidence)") or "") + (t.get("Recommended action") or "")
    h = hashlib.sha1(finding.encode("utf-8", "replace")).hexdigest()[:10]
    return f"{t.get('_row')}:{h}"


# --------------------------------------------------------------------------- #
# formatting
# --------------------------------------------------------------------------- #
def _dm(d):
    """ISO date -> DD/MM (house style). Passes anything unparseable straight through."""
    s = (d or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    return f"{m.group(3)}/{m.group(2)}" if m else s


def _clip(s, n):
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _prio(t):
    return (t.get("Priority") or "").strip().upper() or "MEDIUM"


DETAIL_LINE = ("_Evidence, the affected page and a ready-to-run Claude Code prompt "
               "for each are on the tracker._")


SUMMARY_MAX = 70
# A floor as well as a ceiling. "Brief and publish two blog posts" breaks at " and "
# after five characters — but that " and " joins two VERBS, not two clauses, and
# "Brief" is not a summary. Anything under this is a fragment, so we keep looking.
SUMMARY_MIN = 25

# Real clause boundaries — cutting here leaves a complete thought behind.
_STRONG_BREAKS = (". ", "; ", ", ", " — ", " and ", " or ", " so ", " that ")
# Weaker joins, where a qualifier is bolted onto an already-finished phrase
# ("Shorten the homepage title tag" | "to under 60 characters"). Tried only after
# the strong breaks, because a preposition can also sit INSIDE a clause (or inside
# a quoted keyword — "cash for cars"), where cutting at it strands what came before.
_WEAK_BREAKS = (" to ", " with ", " from ", " into ", " for ", " at ", " of ",
                " across ", " targeting ", " (")
# A cut that ends on one of these — or on a bare number — is mid-thought, however
# tidy its length. "…60 characters and lead" and "…to be between 150" both fit the
# budget and both read as broken English.
_DANGLING = {"and", "or", "to", "with", "that", "so", "the", "a", "an", "of", "in",
             "for", "on", "at", "by", "from", "as", "into", "is", "be", "it", "its"}


def _complete(s):
    """Does this prefix read as a finished thought rather than a severed one?"""
    words = s.rstrip(" ,;.").split()
    if not words:
        return False
    last = words[-1].strip("'\"").lower()
    return last not in _DANGLING and not last.isdigit()


def _summary(t):
    """A headline for a task — "Shorten the homepage title tag" — not the full
    three-clause instruction.

    Slack is a notice board, not the brief. The bullet says what the job IS so a
    developer can tell at a glance whether it is theirs; the tracker row carries
    the how. Keeping the two apart stops the channel turning into the wall of text
    nobody reads, which is the failure mode this whole layer exists to fix.

    Never cuts mid-thought. It takes the LONGEST cut that both fits SUMMARY_MAX and
    still reads as finished English, preferring real clause boundaries and only
    falling back to prepositional joins when no clause boundary fits. So the budget
    gets spent where there is something worth saying ("Add descriptive alt text to
    all images across the 8 affected pages", 66) and not padded where there isn't
    ("Add a new use-case page", 23). An ellipsis appears only if a single clause is
    itself over budget — a badly written task, not a bug here."""
    s = " ".join((t.get("Recommended action") or "").split())
    if not s:
        return "See the tracker"

    def pick(breaks):
        # Cuts must clear the floor; the whole action never has to, since a short
        # instruction ("Identify all 5 broken URLs") is already its own summary.
        cand = [c for c in (s[:i] for i in (s.find(b) for b in breaks) if i > 0)
                if len(c) >= SUMMARY_MIN] + [s]
        good = [c for c in cand if len(c) <= SUMMARY_MAX and _complete(c)]
        return max(good, key=len).rstrip(" ,;:—-") if good else None

    return (pick(_STRONG_BREAKS)
            or pick(_STRONG_BREAKS + _WEAK_BREAKS)
            # Nothing readable fits: trim at a word boundary, never mid-word.
            or (s[:SUMMARY_MAX].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"))


def _bullet(t, show_owner=False):
    owner = (t.get("Owner") or "").strip()
    who = f", {owner}" if (show_owner and owner) else (", *unassigned*" if show_owner else "")
    return (f"\n• *{_prio(t)}* · {_summary(t)}"
            f"  _(row {t.get('_row')}, due {_dm(t.get('Due'))}{who})_")


def new_tasks_msg(site, tasks):
    n = len(tasks)
    head = f":mag: *{site}* — {n} new SEO task{'s' if n != 1 else ''} raised\n"
    lines = [_bullet(t) for t in tasks]
    return (head + "".join(lines) + f"\n\n{DETAIL_LINE}"
            f"\n<{SHEET_URL}|Open the tracker>")


def overdue_msg(site, tasks):
    n = len(tasks)
    head = f":rotating_light: *{site}* — {n} task{'s' if n != 1 else ''} now overdue\n"
    lines = [_bullet(t, show_owner=True) for t in tasks]
    return (head + "".join(lines) + f"\n\n{DETAIL_LINE}"
            f"\n<{SHEET_URL}|Open the tracker>")


def verified_msg(site, results):
    n = len(results)
    head = f":white_check_mark: *{site}* — {n} completed task{'s' if n != 1 else ''} measured\n"
    lines = [f"\n• {_clip(r['result'], 160)}  _(row {r['row']})_" for r in results]
    return head + "".join(lines)


def backlog_msg(site, open_tasks, overdue):
    """One-off catch-up posted by `slack-backlog` when a channel goes live."""
    n, o = len(open_tasks), len(overdue)
    head = (f":wave: *{site}* — this channel is now live for SEO updates.\n\n"
            f"From here on you'll get a post when new tasks are raised, when a task "
            f"goes overdue, and when a completed task's ranking result comes in.\n\n"
            f"*Where things stand today: {n} open task{'s' if n != 1 else ''}"
            + (f", {o} of them overdue" if o else "") + ".*\n")
    top = sorted(open_tasks,
                 key=lambda t: (0 if _prio(t) == "HIGH" else 1, t.get("_row") or 0))[:5]
    lines = [_bullet(t) for t in top]
    more = f"\n• …and {n - len(top)} more" if n > len(top) else ""
    return (head + "".join(lines) + more + f"\n\n{DETAIL_LINE}"
            f"\n<{SHEET_URL}|Open the tracker>")


# --------------------------------------------------------------------------- #
# delivery — called from the Boss tick
# --------------------------------------------------------------------------- #
def deliver(sites, verified, chased):
    """Post the three triggers for every site that has a Slack Channel set.

    `verified` and `chased` are the structured returns from do_verifications() /
    do_chases(): dicts carrying at least {"site", "row"} (+ "result" for verified,
    + "task" for chased).

    Returns log lines for the tick's stdout. Sites without a channel are skipped
    entirely; sites seen for the first time are seeded silently."""
    state = load_state()
    log = []
    ver_by_site, chase_by_site = {}, {}
    for v in verified:
        ver_by_site.setdefault(v["site"], []).append(v)
    for c in chased:
        chase_by_site.setdefault(c["site"], []).append(c["task"])

    for s in sites:
        channel = (s.get("slack") or "").strip()
        if not channel:
            continue
        title = s["title"]
        known = state.get(title)
        current = {task_key(t) for t in s["open_tasks"]}

        if known is None:
            # First sight of this site. Seed and stay quiet — going live must not
            # dump the whole standing backlog into the channel.
            state[title] = sorted(current)
            log.append(f"  - {title}: seeded {len(current)} existing task(s), posted nothing")
            continue

        seen = set(known)
        fresh = [t for t in s["open_tasks"] if task_key(t) not in seen]
        if fresh and post(channel, new_tasks_msg(title, fresh)):
            log.append(f"  - {title}: posted {len(fresh)} new task(s) to {channel}")
        # Mark as seen either way: a Slack failure must not queue up a duplicate
        # blast on the next tick. The sheet stays the source of truth.
        state[title] = sorted(current)

        od = chase_by_site.get(title) or []
        if od and post(channel, overdue_msg(title, od)):
            log.append(f"  - {title}: posted {len(od)} overdue chase(s) to {channel}")

        vr = ver_by_site.get(title) or []
        if vr and post(channel, verified_msg(title, vr)):
            log.append(f"  - {title}: posted {len(vr)} verification(s) to {channel}")

    save_state(state)
    return log
