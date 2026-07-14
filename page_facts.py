#!/usr/bin/env python3
"""page_facts.py — fetch the real, current on-page facts for the audit brief.

WHY THIS EXISTS
The audit agent was inventing on-page findings. The 14/07/2026 audit of all 191
open tasks found roughly one in five was fabricated: titles, H1s, meta
descriptions and canonicals quoted in tasks that had not been on the live site for
weeks. The Outer Edge case is the proof — the agent quoted an H1, title and meta
description that were deleted from the repo between 04/05 and 24/06, then raised a
task about them on 07/07. It described a stale snapshot it had never re-read.

boss_prompt.txt ALREADY told it to fetch the pages first ("FETCH the pages in the
PAGES TO FETCH list … Your prompts MUST be grounded in what is actually on the page
now, not assumptions"). It ignored that instruction, and three of the tasks it
produced would have damaged working sites if actioned.

So we stop asking. Python fetches; the agent is handed the facts and forbidden from
asserting any on-page claim that is not in them. It cannot invent a title it was
never given. Same division of labour as seo_local_gen.py: Python does the I/O, the
model only reasons.

WHAT IT DELIBERATELY GETS RIGHT
  - Decorative images. SE Ranking reports `alt=""` + `aria-hidden="true"` as
    "missing alt text". That is WRONG — an empty alt is the correct treatment for a
    decorative image, and "fixing" it is an accessibility regression. We exclude
    them and count them separately so the agent can see the distinction.
  - Dead vs bot-blocked links. A 403/406/999 from an external host is almost always
    bot protection (the ATO, NDIS and Trustpilot all do it), not a dead link. Only
    404/410 are genuinely dead. An audit task told the team to strip valid ATO and
    Trustpilot citations because it could not tell the difference.
  - Every H1, not the first. Lifemere ships four H1s on one page, two of them
    literal "Heading 1" placeholder text. Reporting only the first hides that.
"""
import html
import re
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
TIMEOUT = 20
DEAD_CODES = {404, 410}


def _get(url, method="GET"):
    """Fetch a URL following redirects. Returns (status, final_url, body)."""
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read().decode("utf-8", "replace") if method == "GET" else ""
            return r.status, r.geturl(), body
    except urllib.error.HTTPError as e:
        return e.code, url, ""
    except Exception as e:
        return None, url, f"__ERROR__ {e}"


def _txt(s):
    """Strip tags/entities from an HTML fragment and collapse whitespace."""
    s = re.sub(r"<[^>]+>", " ", s or "")
    return " ".join(html.unescape(s).split())


def _attr(tag, name):
    m = re.search(rf'{name}\s*=\s*"([^"]*)"', tag, re.I) or \
        re.search(rf"{name}\s*=\s*'([^']*)'", tag, re.I)
    return m.group(1) if m else None


def _has_attr(tag, name):
    return re.search(rf"\b{name}\b", tag, re.I) is not None


def facts(url):
    """Ground truth for one page. Every value here was observed, not inferred."""
    status, final, body = _get(url)
    f = {"url": url, "status": status, "final_url": final}
    if status != 200 or body.startswith("__ERROR__"):
        f["error"] = body if body.startswith("__ERROR__") else f"HTTP {status}"
        return f

    head = body[:body.lower().find("</head>") + 7] if "</head>" in body.lower() else body

    m = re.search(r"<title[^>]*>(.*?)</title>", head, re.I | re.S)
    f["title"] = _txt(m.group(1)) if m else None

    m = re.search(r'<meta[^>]+name\s*=\s*["\']description["\'][^>]*>', head, re.I)
    f["meta_description"] = html.unescape(_attr(m.group(0), "content") or "") if m else None

    m = re.search(r'<link[^>]+rel\s*=\s*["\']canonical["\'][^>]*>', head, re.I) or \
        re.search(r'<link[^>]+rel=canonical[^>]*>', head, re.I)
    f["canonical"] = _attr(m.group(0), "href") if m else None

    # ALL H1s — a page with four of them (or one reading "Heading 1") is a real
    # defect that reporting only the first would hide.
    f["h1"] = [_txt(x) for x in re.findall(r"<h1[^>]*>(.*?)</h1>", body, re.I | re.S)]
    f["h2"] = [_txt(x) for x in re.findall(r"<h2[^>]*>(.*?)</h2>", body, re.I | re.S)][:12]

    # Images: separate genuinely-missing alt from correctly-decorative empty alt.
    missing, decorative = [], 0
    for tag in re.findall(r"<img[^>]*>", body, re.I):
        alt = _attr(tag, "alt")
        empty = (alt is None) or (alt.strip() == "") or not _has_attr(tag, "alt")
        if not empty:
            continue
        if (_attr(tag, "aria-hidden") or "").lower() == "true":
            decorative += 1          # correct as-is — do NOT "fix" these
            continue
        src = (_attr(tag, "src") or _attr(tag, "data-src") or "?").split("/")[-1][:60]
        missing.append(src)
    f["img_missing_alt"] = missing[:10]
    f["img_missing_alt_count"] = len(missing)
    f["img_decorative_ok"] = decorative

    # Crawlable text — settles "the page has no content / is JS-gated" claims.
    stripped = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.I | re.S)
    f["word_count"] = len(_txt(stripped).split())

    return f


def link_check(url, body_urls):
    """Status-check outbound links. Distinguishes DEAD from BOT-BLOCKED, because an
    audit task once told the team to delete live ATO and Trustpilot citations."""
    out = []
    for u in body_urls:
        status, _, _ = _get(u, method="HEAD")
        if status is None or status >= 400:
            status2, _, _ = _get(u)          # some hosts refuse HEAD
            status = status2 if status2 else status
        if status in DEAD_CODES:
            out.append((u, status, "DEAD"))
        elif status is None or status >= 400:
            out.append((u, status, "BOT-BLOCKED (live for humans — do NOT remove)"))
    return out


def brief(pages):
    """The VERIFIED PAGE FACTS block that goes into the audit situation report."""
    lines = ["", "=" * 72,
             "VERIFIED PAGE FACTS — fetched live this tick. THIS IS GROUND TRUTH.",
             "You MUST NOT state any on-page fact that is not in this block.",
             "=" * 72]
    for url in pages:
        f = facts(url)
        lines.append(f"\n--- {url}")
        if f.get("error"):
            lines.append(f"    COULD NOT FETCH ({f['error']}) — do not make claims about this page.")
            continue
        if f["final_url"].rstrip("/") != url.rstrip("/"):
            lines.append(f"    redirects to: {f['final_url']}")
        t = f["title"]
        lines.append(f"    TITLE ({len(t) if t else 0} chars): {t!r}" if t
                     else "    TITLE: ABSENT")
        d = f["meta_description"]
        lines.append(f"    META DESC ({len(d)} chars): {d!r}" if d
                     else "    META DESC: ABSENT")
        lines.append(f"    CANONICAL: {f['canonical'] or 'ABSENT'}")
        if len(f["h1"]) == 1:
            lines.append(f"    H1: {f['h1'][0]!r}")
        elif not f["h1"]:
            lines.append("    H1: ABSENT")
        else:
            lines.append(f"    H1: {len(f['h1'])} H1 TAGS ON ONE PAGE (a defect): "
                         + ", ".join(repr(h) for h in f["h1"]))
        if f["h2"]:
            lines.append("    H2s: " + ", ".join(repr(h) for h in f["h2"][:8]))
        lines.append(f"    CRAWLABLE WORDS: {f['word_count']}")
        if f["img_missing_alt_count"]:
            names = f["img_missing_alt"]
            lines.append(f"    IMAGES MISSING ALT: {f['img_missing_alt_count']} "
                         f"({', '.join(names[:5])})")
            # A divider/icon/logo SVG repeated across the page is decorative but has
            # no aria-hidden, so it is genuinely flagged — yet writing alt text for a
            # divider is the wrong fix. Say so, or the agent captions a squiggle.
            repeated = len(names) != len(set(names))
            looks_decorative = sum(
                1 for n in names
                if n.lower().endswith(".svg")
                or re.search(r"divider|icon|spacer|arc|crest|logo|pattern|shape", n, re.I))
            if repeated or looks_decorative:
                lines.append("    NOTE: some of those look DECORATIVE (repeated file, or an "
                             "svg/divider/icon/logo). For a decorative image the correct fix "
                             "is aria-hidden=\"true\" plus an empty alt — NOT invented alt "
                             "text. Only content images that carry meaning get real alt text.")
        else:
            lines.append("    IMAGES MISSING ALT: 0")
        if f["img_decorative_ok"]:
            lines.append(f"    (+ {f['img_decorative_ok']} decorative images with alt=\"\" and "
                         "aria-hidden=true — these are CORRECT, never 'fix' them)")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    print(brief(sys.argv[1:]))
