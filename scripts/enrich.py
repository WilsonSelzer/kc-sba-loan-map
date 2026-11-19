"""Add profile details to each business, from free public sources. Runs monthly in GitHub Actions after places.py.

  1. Website scan: possible PE/platform ownership, founded year, family-owned, hiring, last site update (copyright year)
  2. Domain age: the year the website's domain was registered (RDAP, free)
  3. Retirement-plan participants: DOL Form 5500 / 5500-SF filings, a real headcount signal

Results are cached in data/enrich_cache.json so each month only re-checks what's stale.
Writes data/profiles.json: { "<place pid or sba bkey>": {...profile fields...} }
"""
import concurrent.futures as cf, csv, io, json, os, re, sys, time, zipfile
from datetime import date, datetime
from urllib.parse import urljoin, urlparse

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CACHE_PATH = os.path.join(DATA, "enrich_cache.json")
THIS_YEAR = date.today().year
UA = {"User-Agent": "Mozilla/5.0 (compatible; kc-rollup-finder/1.0; +https://github.com/WilsonSelzer/kc-sba-loan-map)"}
RESCAN_DAYS = 25
TIME_BUDGET = 60 * 60 * 2          # stop scanning after 2 hours; the rest picks up next month

# Platforms and PE-backed groups active in these categories. A match is a strong ownership signal.
PLATFORMS = ["Wrench Group", "Apex Service Partners", "Sila Services", "Turnpoint Services", "Service Experts", "Authority Brands",
    "Neighborly", "Leap Partners", "Heartland Home Services", "Redwood Services", "Seacoast Service Partners", "Frontdoor",
    "Heartland Dental", "Pacific Dental", "Smile Doctors", "Dental Care Alliance", "MB2 Dental", "Mortenson Dental", "Specialized Dental",
    "Smile Brands", "Great Expressions", "Western Dental", "Dental365", "D4C Dental", "Aspen Dental", "Sonrava", "Benevis",
    "National Veterinary Associates", "NVA", "PetVet Care Centers", "Thrive Pet Healthcare", "Mission Pet Health", "Southern Veterinary Partners",
    "Alliance Animal Health", "Rarebreed", "Heartland Veterinary Partners", "VetCor", "Pathway Vet Alliance", "Veterinary Practice Partners",
    "Upstream Rehabilitation", "Confluent Health", "Ivy Rehab", "BenchMark Physical Therapy", "Athletico", "ATI Physical Therapy", "Lumina",
    "Rollins", "Rentokil", "Anticimex", "Senske", "BrightView", "Caliber", "Crash Champions", "Classic Collision", "Driven Brands",
    "Learning Care Group", "Spring Education", "BrightPath", "Kids R Kids", "Accelerate Care", "Help at Home", "Addus", "Amedisys",
    "Aveanna", "BrightSpring", "Acrisure", "Hub International", "Alera Group", "Patriot Growth", "Higginbotham", "World Insurance Associates",
    "Baldwin Group", "Inszone", "Keystone Agency Partners", "Smart Choice", "Choice Financial", "Citrin Cooperman", "Aprio", "Baker Tilly",
    "CBIZ", "Creative Planning", "Ascend Partners", "Elevate Tax", "Turnpoint"]
OWNERSHIP_PATTERNS = [
    r"\b(?:proud(?:ly)?\s+)?part of (?:the\s+)?([A-Z][\w&.'\- ]{2,40}?)\s+family(?: of (?:companies|brands|practices))?",
    r"\ba\s+([A-Z][\w&.'\-]+(?:\s+[A-Z][\w&.'\-]+){0,4})\s+(?:company|brand|portfolio company|affiliate|partner practice)\b",
    r"\b(?:acquired by|backed by|in partnership with|partnered with|joined)\s+([A-Z][\w&.'\-]+(?:\s+[A-Z][\w&.'\-]+){0,4})",
    r"\bportfolio company of\s+([A-Z][\w&.'\-]+(?:\s+[A-Z][\w&.'\-]+){0,4})",
]
OWNERSHIP_STOP = re.compile(r"\b(BBB|Better Business|Chamber|Association|Society|Google|Facebook|Yelp|Angi|HomeAdvisor|Nextdoor|Carrier|Trane|Lennox|"
                            r"Rheem|Goodman|Bryant|Daikin|American Standard|Ruud|Mitsubishi|Generac|Kohler|Moen|Delta|Invisalign|Medicare|Medicaid|"
                            r"Blue Cross|Cigna|Aetna|Humana|United ?Healthcare|AAHA|ADA|AVMA|APTA|NFIB|PHCC|ACCA|NATE|Energy Star|our|the community|you|your|us|we|"
                            r"local|family|small|veteran|woman|women|minority|licensed|trusted|full[- ]service|Kansas City|KC|Missouri|Kansas|new|great|leading|certified|insured|reputable|top)\b", re.I)
FOUNDED = re.compile(r"\b(?:since|established(?: in)?|est\.?|founded(?: in)?|serving[\w ,&]{0,40}?since|in business since|opened(?: in| our doors in)?)\s*:?\s*((?:18|19|20)\d{2})\b", re.I)
FAMILY = re.compile(r"\bfamily[- ](?:owned|operated|run)\b|\b(?:second|third|fourth|2nd|3rd|4th)[- ]generation\b|\bfather (?:and|&) son\b|\bhusband (?:and|&) wife\b|\bmother (?:and|&) daughter\b", re.I)
HIRING_TEXT = re.compile(r"\b(?:now hiring|we(?:'|’)re hiring|we are hiring|join our team|career opportunities|open positions)\b", re.I)
HIRING_HREF = re.compile(r"href=[\"'][^\"']*(?:career|jobs|join-our-team|now-hiring|employment|hiring)[^\"']*[\"']", re.I)
COPYRIGHT = re.compile(r"(?:©|&copy;|&#169;|copyright)\s*(?:\d{4}\s*[-–]\s*)?((?:19|20)\d{2})", re.I)
ABOUT_HREF = re.compile(r"href=[\"']([^\"'#]*(?:about|our-story|history|who-we-are)[^\"'#]*)[\"']", re.I)
TAGS = re.compile(r"<script.*?</script>|<style.*?</style>|<[^>]+>", re.S | re.I)


def log(m):
    print(f"[enrich] {m}", flush=True)


def domain_of(url):
    try:
        h = urlparse(url if "://" in url else "http://" + url).hostname or ""
    except ValueError:
        return ""
    h = h.lower().removeprefix("www.")
    skip = ("facebook.", "instagram.", "yelp.", "google.", "linkedin.", "squareup.", "wixsite.", "business.site", "godaddysites.",
            "yapiapp.", "localedge.", "mapquest.", "nextdoor.", "angi.", "homeadvisor.", "bbb.org", "linktr.ee")
    return "" if not h or any(s in h for s in skip) else h


def fetch(url):
    r = requests.get(url, headers=UA, timeout=12, allow_redirects=True, stream=True)
    if r.status_code >= 400 or "html" not in r.headers.get("content-type", "html"):
        return None, r.url
    body = r.raw.read(600_000, decode_content=True)
    return body.decode(r.encoding or "utf-8", "replace"), r.url


def text_of(html):
    return re.sub(r"\s+", " ", TAGS.sub(" ", html))


def scan_site(url):
    out = {"checked": date.today().isoformat()}
    try:
        html, final = fetch(url)
    except Exception as e:
        out["error"] = type(e).__name__
        return out
    if not html:
        out["error"] = "no page"
        return out
    pages = [html]
    m = ABOUT_HREF.search(html)
    if m:
        try:
            about, _ = fetch(urljoin(final, m.group(1)))
            if about:
                pages.append(about)
        except Exception:
            pass
    raw = " ".join(pages)
    txt = text_of(raw)

    years = [int(y) for y in FOUNDED.findall(txt) if 1850 <= int(y) <= THIS_YEAR]
    if years:
        out["founded"] = min(years)
    if FAMILY.search(txt):
        out["family"] = FAMILY.search(txt).group(0).lower()
    if HIRING_TEXT.search(txt) or HIRING_HREF.search(raw):
        out["hiring"] = True
    cy = [int(y) for y in COPYRIGHT.findall(raw) if 1995 <= int(y) <= THIS_YEAR]
    if cy:
        out["site_year"] = max(cy)

    owner = None
    for p in PLATFORMS:
        if re.search(r"\b" + re.escape(p) + r"\b", txt):
            owner = p
            break
    if not owner:
        for pat in OWNERSHIP_PATTERNS:
            for g in re.findall(pat, txt):
                g = g.strip(" .,'-")
                if len(g) >= 4 and not OWNERSHIP_STOP.search(g):
                    owner = g
                    break
            if owner:
                break
    if owner:
        i = txt.find(owner)
        out["group"] = owner
        out["group_quote"] = txt[max(0, i - 60): i + len(owner) + 40].strip()[:160]
    return out


def rdap_year(dom):
    try:
        r = requests.get(f"https://rdap.org/domain/{dom}", headers=UA, timeout=12)
        if r.status_code == 429:
            time.sleep(5)
            return "retry"
        if r.status_code != 200:
            return None
        for ev in r.json().get("events", []):
            if ev.get("eventAction") == "registration":
                return int(ev["eventDate"][:4])
    except Exception:
        return None
    return None


def norm(n):
    n = re.sub(r"[^A-Z0-9 ]", " ", str(n or "").upper())
    n = re.sub(r"\b(LLC|L L C|INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LLP|PLLC|PC|PA|DBA|THE|AND)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def load_5500(kc_zips):
    """Form 5500-SF (small plans) and 5500 sponsors in KC ZIP codes -> {(norm name, zip): participants}."""
    found = {}
    for year in (THIS_YEAR - 1, THIS_YEAR - 2, THIS_YEAR - 3):
        got = False
        for form in ("F_5500_SF", "F_5500"):
            url = f"https://askebsa.dol.gov/FOIA%20Files/{year}/Latest/{form}_{year}_Latest.zip"
            try:
                r = requests.get(url, headers=UA, timeout=300)
                if r.status_code != 200:
                    log(f"5500: {form} {year} not available ({r.status_code})")
                    continue
                z = zipfile.ZipFile(io.BytesIO(r.content))
                name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
                rows = csv.DictReader(io.TextIOWrapper(z.open(name), encoding="latin-1"))
                cols = rows.fieldnames
                ncol = next(c for c in cols if "SPONS" in c and "NAME" in c and "DBA" not in c)
                dcol = next((c for c in cols if "SPONS" in c and "DBA" in c and "NAME" in c), None)
                zcol = next(c for c in cols if "SPONS" in c and "US_ZIP" in c and "LOC" not in c)
                pcol = (next((c for c in cols if "TOT_ACT" in c and "PARTCP" in c), None)
                        or next((c for c in cols if "TOT_PARTCP" in c and "EOY" in c), None)
                        or next(c for c in cols if "PARTCP" in c and "TOT" in c))
                log(f"5500: {form} {year} columns name={ncol} zip={zcol} participants={pcol}")
                n = 0
                for row in rows:
                    z5 = (row.get(zcol) or "")[:5]
                    if z5 not in kc_zips:
                        continue
                    try:
                        cnt = int(float(row.get(pcol) or 0))
                    except ValueError:
                        continue
                    if cnt <= 0:
                        continue
                    for nm in (row.get(ncol), row.get(dcol) if dcol else None):
                        if nm:
                            k = (norm(nm), z5)
                            if cnt > found.get(k, (0, 0))[0]:
                                found[k] = (cnt, year)
                    n += 1
                log(f"5500: {n} KC filers in {form} {year}")
                got = True
            except Exception as e:
                log(f"5500: {form} {year} failed: {type(e).__name__}: {e}")
        if got:
            break
    return found


def main():
    places = json.load(open(os.path.join(DATA, "places.json")))
    loans = json.load(open(os.path.join(DATA, "loans.json")))
    cache = json.load(open(CACHE_PATH)) if os.path.exists(CACHE_PATH) else {"sites": {}, "domains": {}, "f5500": {}}
    start = time.time()

    # 1. website scan (independents only)
    targets = {}
    for p in places:
        if p.get("chain"):
            continue
        d = domain_of(p.get("website") or "")
        if d:
            targets.setdefault(d, p["website"])
    stale = [d for d in targets if (date.today() - date.fromisoformat(cache["sites"].get(d, {}).get("checked", "2000-01-01"))).days > RESCAN_DAYS]
    log(f"{len(targets)} business websites; {len(stale)} to scan this run")
    done = 0
    with cf.ThreadPoolExecutor(max_workers=24) as ex:
        futs = {ex.submit(scan_site, targets[d]): d for d in stale}
        for f in cf.as_completed(futs):
            cache["sites"][futs[f]] = f.result()
            done += 1
            if done % 500 == 0:
                log(f"scanned {done}/{len(stale)}")
            if time.time() - start > TIME_BUDGET:
                log("time budget reached; the rest will be scanned next run")
                ex.shutdown(cancel_futures=True)
                break

    # 2. domain registration year (permanent once found)
    todo = [d for d in targets if d not in cache["domains"]]
    log(f"{len(todo)} domains need a registration year")
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for d, y in zip(todo, ex.map(rdap_year, todo)):
            if y == "retry":
                continue
            cache["domains"][d] = y

    # 3. Form 5500 (refresh at most every 60 days)
    f = cache.get("f5500", {})
    if not f.get("fetched") or (date.today() - date.fromisoformat(f["fetched"])).days > 60:
        kc_zips = {p.get("zip") for p in places if p.get("zip")} | {l["zip"] for l in loans}
        found = load_5500(kc_zips)
        if found:
            cache["f5500"] = {"fetched": date.today().isoformat(), "rows": {f"{k[0]}|{k[1]}": v for k, v in found.items()}}
    f5500 = cache.get("f5500", {}).get("rows", {})

    # build profiles
    profiles = {}
    for p in places:
        if p.get("chain"):
            continue
        prof = {}
        d = domain_of(p.get("website") or "")
        if d:
            s = cache["sites"].get(d, {})
            for k in ("founded", "family", "hiring", "site_year", "group", "group_quote"):
                if s.get(k):
                    prof[k] = s[k]
            if cache["domains"].get(d):
                prof["domain_year"] = cache["domains"][d]
        hit = f5500.get(f"{norm(p['name'])}|{p.get('zip')}")
        if hit:
            prof["plan_people"], prof["plan_year"] = hit
        if prof:
            profiles[p.get("pid") or f"{p['cat']}|{norm(p['name'])}"] = prof
    for l in loans:                                   # SBA borrowers with no listing still get a headcount if they file a 5500
        hit = f5500.get(f"{norm(l['name'])}|{l['zip']}")
        if hit and l["bkey"] not in profiles:
            profiles[l["bkey"]] = {"plan_people": hit[0], "plan_year": hit[1]}

    json.dump(profiles, open(os.path.join(DATA, "profiles.json"), "w"), separators=(",", ":"))
    json.dump(cache, open(CACHE_PATH, "w"), separators=(",", ":"))
    c = lambda k: sum(1 for v in profiles.values() if k in v)
    log(f"profiles: {len(profiles)} | founded {c('founded')} | domain year {c('domain_year')} | family {c('family')} | hiring {c('hiring')} "
        f"| possible group {c('group')} | 5500 headcount {c('plan_people')}")


if __name__ == "__main__":
    main()
