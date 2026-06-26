#!/usr/bin/env python3
"""
seo_local_gen.py — slot-free, LLM-on-laptop SEO task generation for the Hermes
SEO Boss, optimised for a LOCAL model (gemma3:12b via Ollama).

Why this exists: gemma3:12b cannot call tools through Ollama and collapses on the
full agentic boss prompt. So we invert the division of labour:

  * Python (deterministic) does ALL the I/O — fetch the live page, extract the
    real title/meta/H1/H2, and (when wired) write rows to the Sheet.
  * The model does ONE constrained thing — rewrite EXISTING on-page elements,
    grounded in the fetched facts, returned as schema-validated JSON.

This plays to the small model's strength (bounded rewriting from given facts) and
avoids its weaknesses (tool-calling, complex routing, inventing capabilities on
open-ended generation). Output is guaranteed-valid JSON via Ollama structured
outputs, and context is right-sized (8k) so it runs comfortably on 16 GB.

Usage:
    python seo_local_gen.py hitl.ph
    python seo_local_gen.py hitl.ph --json   # machine-readable (for the boss)
"""
import json, re, sys, time, urllib.request, urllib.parse, urllib.error

OLLAMA = "http://localhost:11434/api/chat"
MODEL = "gemma3:12b"

COMPETITORS_TAB = "Competitors"
COMPETITORS_HEADER = ["Site Domain", "Competitor Domain", "Competitor Positioning", "Notes"]

SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
                    "element": {"type": "string"},          # which existing element
                    "finding": {"type": "string"},
                    "action": {"type": "string"},
                    "claude_code_prompt": {"type": "string"},
                },
                "required": ["priority", "element", "finding", "action", "claude_code_prompt"],
            },
        },
        "email_subject": {"type": "string"},
        "email_body": {"type": "string"},
    },
    "required": ["tasks", "email_subject", "email_body"],
}

SYS = (
    "You are the SEO Boss for Yoonet. Audit a homepage and output concrete on-page SEO "
    "tasks plus a warm client email.\n"
    "RULES for claude_code_prompt: PLAIN ENGLISH a VA pastes into Claude Code. No code, "
    "no code fences, no invented file paths (say 'on the homepage' — Claude Code finds "
    "the file). State the goal, give the EXACT new copy in quotes and the current copy it "
    "replaces. One paragraph.\n"
    "RULES for tasks: ONLY propose edits to elements that ALREADY EXIST (title, meta "
    "description, H1, the listed H2s/their copy). Do NOT invent new sections, pages, "
    "use-case lists, or capabilities. Combine title+meta into ONE task; each other task "
    "targets a different existing element.\n"
    "ACCURACY: only reference what the site actually offers (read it from the facts). Never "
    "fabricate services it does not sell.\n"
    "DIFFERENTIATION: if a COMPETITIVE CONTEXT block is given, make the rewrites sharpen what "
    "THIS site uniquely is (per the facts) and target the category the competitor is NOT "
    "optimised for. Never borrow the competitor's wording, claims, or category.\n"
    "The email is signed 'the Yoonet SEO team'; greet with 'Hi team'; no [bracketed] "
    "placeholders.")


def _get(url, hops=5):
    """GET that follows redirects including 308 (urllib skips 308 by default)."""
    for _ in range(hops):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            return urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urllib.parse.urljoin(url, e.headers["Location"])
                continue
            raise
    raise RuntimeError(f"too many redirects for {url}")


def fetch_facts(domain):
    url = domain if domain.startswith("http") else f"https://{domain}"
    h = _get(url)

    def one(p):
        m = re.search(p, h, re.I | re.S)
        return re.sub(r"\s+", " ", re.sub("<[^>]+>", "", m.group(1))).strip()[:300] if m else ""

    def many(p, n):
        return [re.sub(r"\s+", " ", re.sub("<[^>]+>", "", x)).strip()[:160]
                for x in re.findall(p, h, re.I | re.S)[:n]]

    return {
        "domain": domain.replace("https://", "").replace("http://", "").strip("/"),
        "url": url,
        "title": one(r"<title>(.*?)</title>"),
        "meta": one(r'<meta name="description" content="(.*?)"'),
        "h1": (many(r"<h1[^>]*>(.*?)</h1>", 1) or [""])[0],
        "h2s": [x for x in many(r"<h2[^>]*>(.*?)</h2>", 8) if x],
    }


def competitors(domain):
    """Rows from the Competitors tab for one site domain (empty if the tab is absent)."""
    try:
        rows = s.read_tab(COMPETITORS_TAB)
    except Exception:
        return []
    recs, _ = s.rows_as_dicts(rows)
    bd = s._bare_domain(domain)
    return [d for d in recs
            if s._bare_domain(d.get("Site Domain")) == bd
            and (d.get("Competitor Domain") or "").strip()]


def build_user(f, comps=None):
    msg = (f"AUDIT TARGET: {f['domain']} (homepage {f['url']}).\n\n"
           f"REAL current homepage (fetched live — these are the only facts; do not assume more):\n"
           f"- <title>: {f['title']}\n- meta description: {f['meta']}\n"
           f"- H1: {f['h1']}\n- H2s: " + " | ".join(f["h2s"]) + "\n\n"
           "Produce 3-4 high-value on-page tasks (rewrites of the existing elements above) "
           "and one 4-sentence client kickoff email. Quote the real text in each finding. "
           "Target how this site's buyers actually search.")
    if comps:
        msg += "\n\nCOMPETITIVE CONTEXT (differentiate against these — do NOT mimic them):"
        for c in comps:
            msg += f"\n- {c.get('Competitor Domain')}: {c.get('Competitor Positioning')}"
            if (c.get('Notes') or '').strip():
                msg += f" [{c['Notes'].strip()}]"
        msg += ("\nThe rewrites must make THIS site's distinct category unmistakable and avoid "
                "the competitor's framing entirely.")
    return msg


def generate(facts, temp=0.2, num_ctx=8192, comps=None):
    if comps is None:
        comps = competitors(facts["domain"])
    body = json.dumps({
        "model": MODEL, "stream": False, "format": SCHEMA,
        "options": {"temperature": temp, "num_ctx": num_ctx, "num_predict": 1400},
        "messages": [{"role": "system", "content": SYS},
                     {"role": "user", "content": build_user(facts, comps)}],
    }).encode()
    t = time.time()
    d = json.load(urllib.request.urlopen(urllib.request.Request(
        OLLAMA, data=body, headers={"Content-Type": "application/json"}), timeout=600))
    out = json.loads(d["message"]["content"])
    out["competitors"] = comps
    out["_meta"] = {"secs": round(time.time() - t, 1), "out_tokens": d.get("eval_count", 0)}
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: seo_local_gen.py <domain> [--json]"); raise SystemExit(1)
    domain = args[0]
    facts = fetch_facts(domain)
    out = generate(facts)
    if "--json" in sys.argv:
        print(json.dumps(out, indent=2)); return
    m = out["_meta"]
    print(f"== {domain}: {len(out['tasks'])} tasks | {m['secs']}s | {m['out_tokens']} tok | JSON valid ✓ ==")
    print(f"   grounded on: title={facts['title'][:60]!r} h1={facts['h1'][:50]!r}")
    for t in out["tasks"]:
        print(f"\n[{t['priority']}] {t['element']}\n  finding: {t['finding']}\n"
              f"  action : {t['action']}\n  CC: {t['claude_code_prompt'][:200]}")
    print(f"\nEMAIL — {out['email_subject']}\n{out['email_body']}")


if __name__ == "__main__":
    main()
