# Google Service Account Runbook (Hermes SEO)

How Hermes authenticates to Google, how to onboard the remaining client sites to
free Search Console, and how to finish domain wide delegation. Pick up here later.

## Reference

| Thing | Value |
|---|---|
| Service account email | `hermes@gen-lang-client-0764569702.iam.gserviceaccount.com` |
| Client ID (numeric, for delegation) | `115936906848567200080` |
| GCP project | `gen-lang-client-0764569702` (Yoonet) |
| Key file on this Mac | `~/.hermes/google_service_account.json` (perms 600) |
| Auth helper in code | `google_api.sa_credentials(scopes, subject=None)` |
| Used by | `seo_boss.py` (Sheets) and `seo_intel.py` (Search Console) |

The management Sheet ("Hermes SEO") is shared with the service account directly, so
Sheets works with no delegation. Search Console works on each property where the
service account has been added as a user. The boss does NOT need Gmail or Calendar,
so domain wide delegation (Part B) is optional and only matters if you later want
Hermes to act as ai@ for mail, calendar or drive.

## Status (as of 17 Jun 2026)

- DONE: **Clinic Admin** live on the service account. Reads positions free from
  `sc-domain:clinicadmin.com.au`.
- PENDING: the 9 sites below — add the service account to each one's Search Console
  property (Part A).
- PENDING: domain wide delegation in the Workspace admin console (Part B) — returns
  `unauthorized_client` until done.

---

## Part A — Onboard a site to free Search Console

Each site you grant flips from paid DataForSEO lookups to free Search Console
positions. Two halves: a person grants access in Search Console, then Hermes points
the sheet at it and verifies.

### A1. Grant the service account (person with Owner on the property)

1. Open Google Search Console (search.google.com/search-console), signed in as an
   account that is an **Owner** of that site's property. For client owned sites, the
   client (or whoever holds Owner) does this step, or you ask them to.
2. Choose the property. **Prefer a Domain property** (`sc-domain:thedomain.com`) if one
   is verified — it covers www, non www, http and https in one entry. Otherwise use the
   URL prefix property (e.g. `https://www.thedomain.com/`).
3. Gear / Settings → **Users and permissions** → **Add user**.
4. Email: `hermes@gen-lang-client-0764569702.iam.gserviceaccount.com`
5. Permission: **Full** (Restricted also reads, Full is safest). Save.

### A2. Flip the site over (Hermes / Claude does this)

Tell Claude "site X is granted on `<property id>`" and it runs the snippet below,
which updates the site's **GSC Property** in the Tracked Keywords tab and verifies a
live pull. To run it by hand, set `DOMAIN` (bare) and `PROP` (exact property id):

```bash
cd ~/.hermes/scripts
DOMAIN="applebom.com.au"
PROP="sc-domain:applebom.com.au"      # or "https://www.applebom.com.au/"
~/.hermes/hermes-agent/venv/bin/python - <<PY
import seo_boss as b, seo_intel
dom, prop = "$DOMAIN", "$PROP"
recs, idx = b.rows_as_dicts(b.read_tab(seo_intel.TRACKED_TAB))
col = chr(ord('A') + idx["GSC Property"]); n = 0
for d in recs:
    if b._bare_domain(d.get("Domain")) == dom and (d.get("GSC Property") or "").strip() != prop:
        b.update_range(f"'{seo_intel.TRACKED_TAB}'!{col}{d['_row']}", [[prop]]); n += 1
print("updated", n, "rows ->", prop)
ki = seo_intel.keyword_intel_v2(dom, record=False)
src = {}
for k in ki: src[k.get('source','?')] = src.get(k.get('source','?'), 0) + 1
print("keywords:", len(ki), "source split:", src)   # want most/all 'gsc'
PY
```

A healthy result shows most keywords with `src=gsc`. Any left on `dataforseo` are
keywords the site does not rank for yet, so Search Console has no data and the live
SERP is checked instead — that is correct, not a failure.

### The 9 sites still to onboard

| Site | Domain | Sheet's GSC Property today | Action |
|---|---|---|---|
| Yoonet | yoonet.io | `sc-domain:yoonet.io` | add SA to that domain property |
| Burstows Funerals | burstows.com.au | `https://www.burstows.com.au/` | add SA (prefer `sc-domain:burstows.com.au` if verified) |
| Applebom | applebom.com.au | none | verify a property, add SA, then set it |
| A1 Electrical | a1electrical.co.nz | none | verify a property, add SA, then set it |
| Outer Edge | outeredge.nz | none | verify a property, add SA, then set it |
| The Hive | hivepractice.com | none | verify a property, add SA, then set it |
| Char House | charhouse.com.au | none | verify a property, add SA, then set it |
| JS Podiatry | jspodiatry.com | none | verify a property, add SA, then set it |
| HITL | hitl.ph | none | verify a property, add SA, then set it |

---

## Part B — Finish domain wide delegation (Gmail / Calendar / Drive as ai@)

Lets the service account impersonate **ai@yoonet.io** for user data APIs. Needed only
if you want Hermes sending mail, reading the calendar, or acting on Drive as ai@. The
SEO boss does not need this.

Prerequisites: you must be a Google Workspace **super admin** for yoonet.io, and
ai@yoonet.io must be a real user in that Workspace.

1. Have the service account Client ID ready: **`115936906848567200080`** (the numeric
   Unique ID — not the email).
2. Go to **admin.google.com** and sign in as a super admin.
3. **Security → Access and data control → API controls**.
4. At the bottom, click **Manage Domain Wide Delegation**.
5. Click **Add new**.
6. **Client ID**: paste `115936906848567200080`.
7. **OAuth scopes** (paste as one comma separated line — these match what the helper
   requests, so any of them work once authorised):

   ```
   https://www.googleapis.com/auth/gmail.readonly,https://www.googleapis.com/auth/gmail.send,https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/calendar,https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/documents,https://www.googleapis.com/auth/contacts.readonly
   ```

8. Click **Authorize**.
9. Wait for propagation — usually a few minutes, occasionally up to 24 hours.
10. Tell Claude to re-test, or run the check below. When it prints `ai@yoonet.io`
    instead of an `unauthorized_client` error, delegation is live.

```bash
~/.hermes/hermes-agent/venv/bin/python - <<PY
from google.oauth2 import service_account
from googleapiclient.discovery import build
KEY = "/Users/yoonetmarketplace/.hermes/google_service_account.json"
c = service_account.Credentials.from_service_account_file(
    KEY, scopes=["https://www.googleapis.com/auth/gmail.readonly"], subject="ai@yoonet.io")
print(build("gmail", "v1", credentials=c).users().getProfile(userId="me").execute().get("emailAddress"))
PY
```

### Gotchas

- Use the **numeric Client ID**, not the service account email.
- Scopes must match exactly. Extra scopes are fine; a missing one means
  `unauthorized_client` for that scope.
- The impersonated user (ai@yoonet.io) must exist in the Workspace and have the
  relevant service (Gmail, Calendar) switched on.
- Only a super admin can add a delegation entry.
