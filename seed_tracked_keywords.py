#!/usr/bin/env python3
"""
One-off: seed the "Tracked Keywords" tab in the Hermes SEO sheet from the current
SE Ranking keyword lists, with each site's REAL tracked location/device/language
(resolved from PROJECT_getSearchEngines) so seo_intel.keyword_intel_v2 reproduces
SE Ranking positions. Run once during the off-ramp; after this the tab is the
source of truth and SE Ranking tracking projects can be retired.

    python seed_tracked_keywords.py            # write the tab
    python seed_tracked_keywords.py --dry-run  # print, write nothing
"""
import sys
sys.path.insert(0, "/Users/home/.hermes/scripts")
import seo_boss as s
import seo_intel as si

# bare-domain -> (DataForSEO location_name, language, device, GSC property)
# location/device resolved from SE Ranking PROJECT_getSearchEngines on 05/06/2026:
#   211=Google AU, 1551=AU mobile, 325=Google NZ, 329/1669=Google PH (tl).
# GSC property filled only where the YooSEO service account already has access
# (3 sites today); the rest use DataForSEO until onboarded to GSC.
SITE_CONFIG = {
    "yoonet.io":              ("Australia",                          "en", "desktop", "sc-domain:yoonet.io"),
    "applebom.com.au":        ("Toowoomba,Queensland,Australia",     "en", "desktop", ""),
    "a1electrical.co.nz":     ("New Zealand",                        "en", "desktop", ""),
    "outeredge.nz":           ("Dunedin,Otago,New Zealand",          "en", "desktop", ""),
    "hivepractice.com":       ("Australia",                          "en", "desktop", ""),
    "clinicadmin.com.au":     ("Australia",                          "en", "desktop", "sc-domain:clinicadmin.com.au"),
    "burstows.com.au":        ("Australia",                          "en", "desktop", "https://www.burstows.com.au/"),
    "totaltreeservices.com":  ("Australia",                          "en", "desktop", ""),
    "balanga.com.ph":         ("Philippines",                        "tl", "desktop", ""),
    "charhouse.com.au":       ("Toowoomba,Queensland,Australia",     "en", "desktop", ""),
}
DEFAULT = ("Australia", "en", "desktop", "")

# yoonet is dual-tracked AU+NZ in SE Ranking; balanga tracked in Tagalog. Both
# flagged in SE_RANKING_OFFRAMP.md for the team to confirm/extend.


def main(dry=False):
    projs = [p for p in (s.serank("PROJECT_listProjects") or []) if p.get("is_active", 1)]
    rows = []
    summary = []
    for p in projs:
        dom = s.domain_of(p)
        bd = s._bare_domain(dom)
        loc, lang, dev, gsc = SITE_CONFIG.get(bd, DEFAULT)
        # pull + de-dupe the tracked keyword names
        seen, kws = set(), []
        for kw in (s.serank("PROJECT_listKeywords", {"site_id": p["id"]}) or []):
            name = (kw.get("name") or "").strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                kws.append(name)
        for name in kws:
            rows.append([bd, name, loc, lang, dev, gsc])
        summary.append((bd, len(kws), loc, dev, "GSC" if gsc else "DataForSEO"))

    print(f"Seeding '{si.TRACKED_TAB}' — {len(rows)} keywords across {len(projs)} sites\n")
    print(f"{'Domain':26} {'KW':>3}  {'Location':34} {'Path'}")
    print("-" * 78)
    for bd, n, loc, dev, path in summary:
        print(f"{bd:26} {n:>3}  {loc:34} {path}")
    print("-" * 78)
    print(f"GSC path now: 3 sites (free). DataForSEO path: 7 sites "
          f"(~${len(rows)*0.0125:.2f} for one full live pass of all {len(rows)} keywords).")

    if dry:
        print("\n--dry-run: nothing written.")
        return
    s.ensure_tab(si.TRACKED_TAB, si.TRACKED_HEADER)
    s.update_range(f"'{si.TRACKED_TAB}'!A1", [si.TRACKED_HEADER] + rows)
    print(f"\nWrote {len(rows)} rows to '{si.TRACKED_TAB}'.")


if __name__ == "__main__":
    main(dry="--dry-run" in sys.argv)
