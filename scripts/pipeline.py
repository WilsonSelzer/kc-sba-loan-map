"""KC SBA loan pipeline.

Pulls the SBA 7(a) and 504 FOIA files, keeps loans in the Kansas City metro,
places each on the map by ZIP code, and compares against the last snapshot
to flag what changed. Runs monthly via GitHub Actions; only rebuilds when
SBA has published a new quarterly file.

Usage:
  python scripts/pipeline.py                      # fetch from data.sba.gov
  python scripts/pipeline.py --local 7a.csv 504.csv --asof 2026-09-30
"""
import argparse, hashlib, io, json, os, re, urllib.parse, urllib.request
from datetime import datetime, timezone

import pandas as pd
import zipcodes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
DATASET_PAGE = "https://data.sba.gov/dataset/7a-504-foia"

KC_COUNTIES = {
    "MO": ["JACKSON", "CLAY", "PLATTE", "CASS", "CLINTON", "LAFAYETTE", "RAY", "BATES", "CALDWELL"],
    "KS": ["JOHNSON", "WYANDOTTE", "LEAVENWORTH", "MIAMI", "LINN"],
}
DROP_STATUS = {"CANCLD", "NOT FUNDED"}
STATUS_LABEL = {"EXEMPT": "Active", "P I F": "Paid in full", "PIF": "Paid in full",
                "COMMIT": "Approved, not yet disbursed", "CHGOFF": "Charged off"}
SECTORS = {"11": "Agriculture", "21": "Mining & energy", "22": "Utilities", "23": "Construction & trades",
           "31": "Manufacturing", "32": "Manufacturing", "33": "Manufacturing", "42": "Wholesale",
           "44": "Retail", "45": "Retail", "48": "Transportation & logistics", "49": "Transportation & logistics",
           "51": "Information & media", "52": "Finance & insurance", "53": "Real estate",
           "54": "Professional services", "55": "Management", "56": "Business services",
           "61": "Education", "62": "Healthcare", "71": "Arts & recreation", "72": "Restaurants & hotels",
           "81": "Personal & repair services", "92": "Public administration"}


def log(msg):
    print(f"[pipeline] {msg}", flush=True)


def find_sources():
    """Read SBA's dataset page and pick out the current 7(a) FY2020+ and 504 FY2010+ CSV links."""
    req = urllib.request.Request(DATASET_PAGE, headers={"User-Agent": "kc-sba-loan-map"})
    with urllib.request.urlopen(req, timeout=60) as r:
        html = r.read().decode("utf-8", "replace")
    def pick(pattern):
        hits = re.findall(r'href="([^"]*' + pattern + r')"', html)
        if not hits:
            raise RuntimeError(f"No link matching {pattern} on {DATASET_PAGE}")
        url = urllib.parse.urljoin(DATASET_PAGE, hits[0])
        return url, re.search(r"asof_(\d{6})", url).group(1)
    url7, d7 = pick(r"FOIA_7a_FY2020_Present_asof_\d{6}\.csv")
    url5, _ = pick(r"FOIA_504_FY2010_Present_asof_\d{6}\.csv")
    asof = datetime.strptime(d7, "%y%m%d").date().isoformat()
    log(f"SBA files as of {asof}")
    return url7, url5, asof


def download(url):
    log(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "kc-sba-loan-map"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return io.BytesIO(r.read())


def load(src, prog):
    df = pd.read_csv(src, dtype=str, encoding="latin-1")
    df["ProjectCounty"] = df["ProjectCounty"].fillna("").str.strip().str.upper()
    mask = False
    for st, counties in KC_COUNTIES.items():
        mask = mask | ((df["ProjectState"] == st) & df["ProjectCounty"].isin(counties))
    d = df[mask].copy()
    d["Prog"] = prog
    d["Lender"] = d["CDC_Name"] if prog == "504" else d["BankName"]
    if prog == "504":
        d = d[d["ApprovalFY"].astype(int) >= 2020]
    return d


def point(zip5, key):
    """ZIP-code centroid with a small fixed offset so loans in one ZIP don't stack."""
    hit = zipcodes.matching(zip5) if zip5 and zip5.isdigit() and len(zip5) == 5 else []
    if not hit:
        return None, None
    lat, lng = float(hit[0]["lat"]), float(hit[0]["long"])
    h = hashlib.md5(key.encode()).digest()
    return round(lat + (h[0] - 128) / 128 * 0.012, 5), round(lng + (h[1] - 128) / 128 * 0.015, 5)


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def build(df):
    rows = []
    df = df.astype(object).where(df.notna(), None)
    for r in df.to_dict("records"):
        if (r.get("LoanStatus") or "").strip() in DROP_STATUS:
            continue
        key = "|".join(str(r.get(k) or "") for k in ("Prog", "LocationID", "ApprovalDate", "GrossApproval", "BorrName"))
        lid = hashlib.sha1(key.encode()).hexdigest()[:12]
        zip5 = str(r.get("BorrZip") or "")[:5]
        lat, lng = point(zip5, key)
        if lat is None:
            continue
        naics = str(r.get("NaicsCode") or "")
        rows.append({
            "id": lid, "prog": r["Prog"], "name": (r.get("BorrName") or "").strip(),
            "street": (r.get("BorrStreet") or "").strip(), "city": (r.get("BorrCity") or "").strip().title(),
            "state": r.get("BorrState"), "zip": zip5, "county": r["ProjectCounty"].title(),
            "lender": (r.get("Lender") or "").strip(), "amount": num(r.get("GrossApproval")),
            "date": r.get("ApprovalDate"), "fy": int(r.get("ApprovalFY")),
            "naics": naics, "industry": (r.get("NaicsDescription") or "").strip(),
            "sector": SECTORS.get(naics[:2], "Other"),
            "franchise": (r.get("FranchiseName") or "").strip() or None,
            "age": (r.get("BusinessAge") or "Unanswered").strip(),
            "status": STATUS_LABEL.get((r.get("LoanStatus") or "").strip(), (r.get("LoanStatus") or "").strip()),
            "jobs": int(num(r.get("JobsSupported")) or 0), "lat": lat, "lng": lng,
        })
    rows.sort(key=lambda x: x["date"] or "", reverse=True)
    return rows


def summarize(loans):
    acq = [l for l in loans if l["age"] == "Change of Ownership"]
    by = {}
    for l in loans:
        by.setdefault(l["sector"], [0, 0.0])
        by[l["sector"]][0] += 1
        by[l["sector"]][1] += l["amount"] or 0
    return {"loans": len(loans), "dollars": sum(l["amount"] or 0 for l in loans),
            "acquisitions": len(acq), "acq_dollars": sum(l["amount"] or 0 for l in acq),
            "by_sector": {k: {"loans": v[0], "dollars": v[1]} for k, v in sorted(by.items(), key=lambda kv: -kv[1][1])}}


def diff(prev, cur, asof, prev_asof):
    pmap = {l["id"]: l for l in prev}
    new = [l for l in cur if l["id"] not in pmap]
    changed = [{"id": l["id"], "name": l["name"], "from": pmap[l["id"]]["status"], "to": l["status"]}
               for l in cur if l["id"] in pmap and pmap[l["id"]]["status"] != l["status"]]
    lenders = {}
    for l in new:
        lenders[l["lender"]] = lenders.get(l["lender"], 0) + (l["amount"] or 0)
    sectors = {}
    for l in new:
        sectors[l["sector"]] = sectors.get(l["sector"], 0) + 1
    return {
        "asof": asof, "compared_to": prev_asof,
        "new_loans": len(new), "new_dollars": sum(l["amount"] or 0 for l in new),
        "new_acquisitions": [l["id"] for l in new if l["age"] == "Change of Ownership"],
        "new_ids": [l["id"] for l in new],
        "status_changes": changed,
        "charge_offs": [c for c in changed if c["to"] == "Charged off"],
        "top_lenders": sorted(lenders.items(), key=lambda kv: -kv[1])[:5],
        "top_sectors": sorted(sectors.items(), key=lambda kv: -kv[1])[:5],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", nargs=2, metavar=("SEVEN_A", "FIVE04"))
    ap.add_argument("--asof")
    ap.add_argument("--baseline-quarter", action="store_true",
                    help="first run only: treat loans approved in the latest quarter as 'new'")
    a = ap.parse_args()
    os.makedirs(DATA, exist_ok=True)
    meta_path = os.path.join(DATA, "meta.json")
    meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {"asof": None, "runs": []}
    run = {"ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    if a.local:
        src7, src5, asof = a.local[0], a.local[1], a.asof
    else:
        url7, url5, asof = find_sources()
        if asof and asof == meta.get("asof"):
            run.update(result="no new SBA file", asof=asof)
            meta["runs"] = ([run] + meta["runs"])[:24]
            json.dump(meta, open(meta_path, "w"), indent=1)
            log(f"SBA data still as of {asof}; nothing to do")
            return
        src7, src5 = download(url7), download(url5)

    loans = build(pd.concat([load(src7, "7(a)"), load(src5, "504")]))
    loans_path = os.path.join(DATA, "loans.json")
    prev = json.load(open(loans_path)) if os.path.exists(loans_path) else None
    if prev is not None:
        changes = diff(prev, loans, asof, meta.get("asof"))
    elif a.baseline_quarter:
        last = max(l["date"] for l in loans)
        q_start = f"{last[:4]}-{((int(last[5:7]) - 1) // 3) * 3 + 1:02d}-01"
        prior = [l for l in loans if l["date"] < q_start]
        changes = diff(prior, loans, asof, f"loans approved before {q_start}")
    else:
        changes = diff([], loans, asof, None)

    json.dump(loans, open(loans_path, "w"), separators=(",", ":"))
    json.dump(changes, open(os.path.join(DATA, "changes.json"), "w"), indent=1)
    json.dump(summarize(loans), open(os.path.join(DATA, "summary.json"), "w"), indent=1)
    run.update(result="rebuilt", asof=asof, loans=len(loans), new_loans=changes["new_loans"])
    meta.update(asof=asof, updated=run["ran_at"])
    meta["runs"] = ([run] + meta["runs"])[:24]
    json.dump(meta, open(meta_path, "w"), indent=1)
    log(f"{len(loans)} KC loans as of {asof}; {changes['new_loans']} new")


if __name__ == "__main__":
    main()
