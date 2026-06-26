#!/usr/bin/env python3
"""
seo_hybrid.py — hybrid SEO generation: local Gemma + Claude API key.

Division of labour, matched to each model's strengths:
  * Gemma (local Ollama, free)  -> the bulk bounded-rewrite tasks (title/meta/H1/H2),
    grounded + schema-validated via seo_local_gen.
  * Claude (direct Anthropic API key) -> the client-facing email, where quality and
    tone matter and a 12B local model is weakest.

Claude is reached over the raw Messages API (no SDK — matches the rest of the
Hermes scripts, which are all urllib glue). The key is read from ANTHROPIC_API_KEY
(env first, then ~/.hermes/.env). If it's absent, the Gemma tasks still print and
the email step is skipped with a clear note — so the local half works today and the
Claude half lights up the moment the key is added.

Usage:
    python seo_hybrid.py hitl.ph
    python seo_hybrid.py hitl.ph --json
"""
import json, os, re, sys, time, urllib.request, urllib.error
from pathlib import Path

import seo_local_gen as g  # reuse fetch_facts + Gemma task generation

CLAUDE_MODEL = "claude-opus-4-8"            # most capable Opus tier for client-facing email
CLAUDE_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_VERSION = "2023-06-01"


def anthropic_key():
    """ANTHROPIC_API_KEY from env, else from ~/.hermes/.env (commented or not)."""
    k = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_TOKEN")
    if k:
        return k.strip()
    try:
        env = (Path.home() / ".hermes/.env").read_text()
        m = re.search(r"^#?\s*ANTHROPIC_API_KEY=(.+)$", env, re.M)
        if m:
            return m.group(1).strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def claude_email(facts, tasks, key, comps=None, model=CLAUDE_MODEL):
    """Draft a warm, plain client kickoff email with Claude. Returns (subject, body, usage)."""
    task_lines = "\n".join(f"- [{t['priority']}] {t['element']}: {t['action']}" for t in tasks)
    comp_block = ""
    if comps:
        names = ", ".join(c.get("Competitor Domain", "") for c in comps if c.get("Competitor Domain"))
        pos = "; ".join(c.get("Competitor Positioning", "") for c in comps if c.get("Competitor Positioning"))
        comp_block = (
            f"\n\nCompetitive context (do NOT name the competitor to the client): a similarly-named "
            f"competitor ({names}) is positioned as: {pos}. Frame our work around what makes "
            f"{facts['domain']} distinct, without mentioning them.")
    prompt = (
        f"You are the SEO Boss for Yoonet writing a short, warm client kickoff email for the site "
        f"{facts['domain']}.\n\n"
        f"What the site is (from its live homepage):\n- title: {facts['title']}\n"
        f"- meta: {facts['meta']}\n- H1: {facts['h1']}\n\n"
        f"On-page changes the team is starting this week:\n{task_lines}{comp_block}\n\n"
        "Write a 4-sentence email: warm, plain English, no jargon, no [bracketed] placeholders. "
        "Greet with 'Hi team' and sign off as 'The Yoonet SEO team'. Be specific to what this site "
        "actually sells. Return STRICT JSON: {\"subject\": \"...\", \"body\": \"...\"} and nothing else."
    )
    body = json.dumps({
        "model": model, "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(CLAUDE_URL, data=body, method="POST", headers={
        "x-api-key": key, "anthropic-version": CLAUDE_VERSION, "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    if d.get("stop_reason") == "refusal":
        raise RuntimeError("Claude refused the request")
    text = next((b["text"] for b in d.get("content", []) if b.get("type") == "text"), "")
    # Opus 4.8 with thinking off may prepend prose — extract the JSON object robustly.
    obj = None
    try:
        obj = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
            except Exception:
                obj = None
    if obj:
        return obj.get("subject", ""), obj.get("body", ""), d.get("usage", {})
    return "SEO kickoff", text.strip(), d.get("usage", {})


def run(domain):
    facts = g.fetch_facts(domain)
    gem = g.generate(facts)                  # Gemma: bounded-rewrite tasks (+ its own email, unused)
    tasks = gem["tasks"]
    comps = gem.get("competitors", [])
    key = anthropic_key()
    email = {"by": "none", "subject": "", "body": "",
             "note": "ANTHROPIC_API_KEY not set — add it to ~/.hermes/.env to enable the Claude email step."}
    if key:
        try:
            subj, bdy, usage = claude_email(facts, tasks, key, comps=comps)
            email = {"by": CLAUDE_MODEL, "subject": subj, "body": bdy, "usage": usage}
        except (urllib.error.HTTPError, urllib.error.URLError, RuntimeError) as e:
            detail = e.read().decode()[:200] if isinstance(e, urllib.error.HTTPError) else str(e)
            email = {"by": "error", "subject": "", "body": "", "note": f"Claude call failed: {detail}"}
    return {"domain": facts["domain"], "facts": facts, "tasks": tasks,
            "competitors": comps, "email": email, "gemma_meta": gem.get("_meta", {})}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: seo_hybrid.py <domain> [--json]"); raise SystemExit(1)
    out = run(args[0])
    if "--json" in sys.argv:
        print(json.dumps(out, indent=2)); return
    gm = out["gemma_meta"]
    print(f"== {out['domain']} | Gemma tasks: {len(out['tasks'])} ({gm.get('secs','?')}s) | "
          f"email by: {out['email']['by']} ==")
    print("\n-- TASKS (local Gemma, bounded rewrites) --")
    for t in out["tasks"]:
        print(f"\n[{t['priority']}] {t['element']}\n  action: {t['action']}\n  CC: {t['claude_code_prompt'][:180]}")
    e = out["email"]
    print(f"\n-- CLIENT EMAIL (by {e['by']}) --")
    if e.get("note"):
        print(f"  ({e['note']})")
    else:
        print(f"Subject: {e['subject']}\n{e['body']}")


if __name__ == "__main__":
    main()
