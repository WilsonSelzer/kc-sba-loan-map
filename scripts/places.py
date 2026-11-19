"""Pull every KC-metro business in the 13 roll-up categories from Overture Maps (free, open, monthly),
then match each one to SBA borrowers so the map can show businesses with and without SBA loans.

Runs in GitHub Actions after pipeline.py. Writes:
  data/places.json             businesses in the roll-up categories (name, location, phone, website, SBA match)
  data/place_categories.json   counts of every Overture category in the area, for tuning the mapping
  data/place_history.json      the Overture release each business first appeared in, so the map can filter by date
"""
import json, math, os, re, subprocess, sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
BBOX = "-95.10,38.55,-94.05,39.45"          # KC metro: Leavenworth to Lee's Summit, Liberty to Paola
RAW = os.path.join(ROOT, "kc_places.geojsonseq")
HIST = os.path.join(DATA, "place_history.json")

# Overture category -> roll-up category. Matched on whole category names first, then on keywords.
EXCLUDE = ["pet_", "supplier", "wholesale", "school_district", "insurance_company", "dealer"]

CATEGORY_RULES = [
    ("hvac", ["hvac", "heating_and_air", "air_conditioning", "plumb", "water_heater", "furnace"]),
    ("elec", ["electrician", "electrical_contractor", "electrical_service", "electrical_repair", "lighting_contractor"]),
    ("roof", ["roof", "siding", "gutter", "exterior_contractor"]),
    ("lawn", ["landscap", "lawn", "tree_service", "gardener", "irrigation"]),
    ("pest", ["pest_control", "exterminat", "janitorial", "house_cleaning", "cleaning_service", "carpet_cleaning", "commercial_cleaning"]),
    ("dental", ["dentist", "dental", "orthodont", "endodont", "periodont", "oral_surgeon", "pediatric_dentist"]),
    ("vet", ["veterinar", "animal_hospital", "pet_clinic"]),
    ("pt", ["physical_therap", "chiropract", "physiotherap", "sports_medicine", "occupational_therap"]),
    ("home", ["home_health", "home_care", "senior_care", "assisted_living", "nursing_home", "hospice", "elder_care", "in_home_care"]),
    ("acct", ["accountant", "accounting", "tax_service", "tax_preparation", "bookkeep", "cpa"]),
    ("ins", ["insurance_agen", "insurance_broker", "insurance_service", "insurance"]),
    ("auto", ["auto_repair", "automotive_repair", "auto_body", "collision", "car_wash", "oil_change", "tire_repair", "tire_shop",
              "transmission", "brake", "muffler", "auto_glass", "smog_check", "auto_maintenance", "automotive_service"]),
    ("kids", ["child_care", "day_care", "daycare", "preschool", "childcare"]),
]
FRANCHISES = ["ROTO ROOTER", "MR ROOTER", "BENJAMIN FRANKLIN", "ONE HOUR HEATING", "AIRE SERV", "MR ELECTRIC", "MISTER SPARKY",
    "MOSQUITO JOE", "MOLLY MAID", "MERRY MAIDS", "CHEM DRY", "STANLEY STEEMER", "SERVICEMASTER", "SERVPRO", "TERMINIX", "ORKIN",
    "TRULY NOLEN", "ASPEN DENTAL", "SMILE GENERATION", "BANFIELD", "VCA ", "MIDAS", "MEINEKE", "VALVOLINE", "TAKE 5", "JIFFY LUBE",
    "CHRISTIAN BROTHERS", "GOODYEAR", "FIRESTONE", "PEP BOYS", "CAR X", "CARX", "TIRES PLUS", "BIG O TIRES", "DISCOUNT TIRE", "MAACO",
    "CALIBER COLLISION", "SERVICE KING", "GERBER COLLISION", "KIDS R KIDS", "PRIMROSE", "KIDDIE ACADEMY", "KINDERCARE", "GODDARD",
    "LEARNING EXPERIENCE", "LA PETITE", "CHILDRENS LIGHTHOUSE", "H R BLOCK", "JACKSON HEWITT", "LIBERTY TAX", "THE JOINT", "ATI PHYSICAL",
    "ATHLETICO", "SELECT PHYSICAL", "SERC PHYSICAL", "HOME INSTEAD", "VISITING ANGELS", "RIGHT AT HOME", "COMFORT KEEPERS", "BRIGHTSTAR",
    "GRISWOLD", "STATE FARM", "FARMERS INSURANCE", "ALLSTATE", "AMERICAN FAMILY", "SHELTER INSURANCE", "NATIONWIDE", "PROGRESSIVE",
    "GEICO", "EDWARD JONES", "GREAT CLIPS", "WEED MAN", "TRUGREEN", "LAWN DOCTOR", "BRIGHTVIEW", "DAVEY TREE", "MR HANDYMAN",
    "LEAFFILTER", "ARS RESCUE", "ONE HOUR AIR", "GO CAR WASH", "TIDAL WAVE", "MISTER CAR WASH", "QUICK QUACK", "TOMMYS EXPRESS"]
SUFFIX = re.compile(r"\b(LLC|L L C|INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LLP|PLLC|PC|PA|DBA|THE|AND)\b")


def log(m):
    print(f"[places] {m}", flush=True)


def category_for(cats):
    cats = [c for c in cats if c]
    if cats and any(x in cats[0] for x in EXCLUDE):
        return None
    for cid, keys in CATEGORY_RULES:
        if any(any(k in c for k in keys) for c in cats[:1]):     # primary category decides first
            return cid
    for cid, keys in CATEGORY_RULES:
        if any(any(k in c for k in keys) for c in cats[1:]):     # then alternates
            return cid
    return None


def norm(name):
    n = re.sub(r"[^A-Z0-9 ]", " ", str(name or "").upper())
    return re.sub(r"\s+", " ", SUFFIX.sub(" ", n)).strip()


def tokens(name):
    return {t for t in norm(name).split() if len(t) > 2}


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def first(v, *path):
    for p in path:
        if isinstance(v, list):
            v = v[0] if v else None
        if not isinstance(v, dict):
            return None
        v = v.get(p)
    if isinstance(v, list):
        v = v[0] if v else None
    return v


def releases():
    """All Overture releases still published, oldest first, and the latest one."""
    from overturemaps.core import get_available_releases
    allr, latest = get_available_releases()
    return sorted(allr), latest


def download(release=None, out=None):
    cmd = ["overturemaps", "download", f"--bbox={BBOX}", "-f", "geojsonseq", "--type=place", "-o", out or RAW]
    if release:
        cmd += ["-r", release]
    log(f"downloading Overture places {release or 'latest'} for bbox {BBOX}")
    subprocess.run(cmd, check=True)


def load_history():
    if os.path.exists(HIST):
        return json.load(open(HIST))
    return {"releases": [], "first": {}}


def record(hist, release, pids):
    """Remember the first release each listing appeared in. The first release ever tracked is the baseline."""
    day = release.split(".")[0]
    if any(r["release"] == release or r["date"] == day for r in hist["releases"]):   # same-day re-releases are one pull
        return
    new = 0
    for pid in pids:
        if pid not in hist["first"]:
            hist["first"][pid] = day
            new += 1
    hist["releases"].append({"release": release, "date": day, "listings": len(pids), "new": new,
                             "baseline": not hist["releases"]})
    hist["releases"].sort(key=lambda r: r["date"])
    log(f"history: {release} has {len(pids)} listings, {new} new")


def parse():
    seen, out, allcats = 0, [], Counter()
    with open(RAW) as f:
        for line in f:
            line = line.strip().lstrip("\x1e")
            if not line:
                continue
            feat = json.loads(line)
            p = feat.get("properties", {})
            for k, v in list(p.items()):
                if isinstance(v, str) and v[:1] in "[{":
                    try:
                        p[k] = json.loads(v)
                    except ValueError:
                        pass
            seen += 1
            tax = p.get("taxonomy") if isinstance(p.get("taxonomy"), dict) else {}
            cats = p.get("categories") if isinstance(p.get("categories"), dict) else {}
            prim = tax.get("primary") or cats.get("primary") or ""
            alts = list(tax.get("alternates") or []) + list(cats.get("alternate") or []) + list(tax.get("hierarchy") or [])
            basic = p.get("basic_category") or ""
            allcats[prim or basic or "none"] += 1
            if seen == 1:
                log("sample property keys: " + ", ".join(sorted(p.keys())))
            cid = category_for([prim] + alts)
            if not cid or (p.get("confidence") or 0) < 0.5 or str(p.get("operating_status") or "").lower() in ("closed", "permanently_closed", "temporarily_closed"):
                continue
            lng, lat = feat["geometry"]["coordinates"][:2]
            name = first(p, "names", "primary")
            if not name:
                continue
            out.append({
                "pid": p.get("id") or feat.get("id"), "name": name, "cat": cid, "category": prim or basic,
                "lat": round(lat, 5), "lng": round(lng, 5),
                "street": first(p, "addresses", "freeform") or "", "city": (first(p, "addresses", "locality") or "").title(),
                "state": (first(p, "addresses", "region") or "").replace("US-", ""), "zip": str(first(p, "addresses", "postcode") or "")[:5],
                "phone": first(p, "phones") or "", "website": first(p, "websites") or "",
                "brand": first(p, "brand", "names", "primary") or "",
                "conf": round(p.get("confidence") or 0, 2),
            })
    log(f"{seen} Overture places in the area; {len(out)} in the roll-up categories")
    return out, allcats


def match_sba(places):
    """Link a place to an SBA borrower when the names share most of their words and they're within 3 km (SBA points are ZIP-level)."""
    loans_path = os.path.join(DATA, "loans.json")
    if not os.path.exists(loans_path):
        return
    biz = {}
    for l in json.load(open(loans_path)):
        biz.setdefault(l["bkey"], (tokens(l["name"]), (l["lat"], l["lng"]), l["zip"]))
    grid = {}
    for k, (t, ll, z) in biz.items():
        grid.setdefault((round(ll[0], 1), round(ll[1], 1)), []).append(k)
    matched = 0
    for pl in places:
        pt, best, score = tokens(pl["name"]), None, 0.0
        if not pt:
            continue
        cy, cx = round(pl["lat"], 1), round(pl["lng"], 1)
        for dy in (-0.1, 0, 0.1):
            for dx in (-0.1, 0, 0.1):
                for k in grid.get((round(cy + dy, 1), round(cx + dx, 1)), []):
                    t, ll, z = biz[k]
                    if not t:
                        continue
                    j = len(pt & t) / len(pt | t)
                    near = km((pl["lat"], pl["lng"]), ll) < (8 if j >= 0.8 else 3)
                    if j > score and (near or (pl["zip"] and pl["zip"] == z)):
                        best, score = k, j
        if best and score >= 0.6:
            pl["sba"] = best
            matched += 1
    log(f"{matched} places matched to an SBA borrower")


def main():
    try:
        _, release = releases()
    except Exception as e:                     # catalog unreachable: fall back to the latest download, dated today
        log(f"release list unavailable ({e}); using latest")
        import datetime
        release = datetime.date.today().isoformat() + ".latest"
    if "--skip-download" not in sys.argv:
        download(None if release.endswith(".latest") else release)
    places, allcats = parse()
    hist = load_history()
    record(hist, release, [p["pid"] for p in places])
    # drop chains and franchises with many KC locations: PE targets are independents
    brand_counts = Counter(p["brand"] or norm(p["name"]) for p in places)
    for p in places:
        n = norm(p["name"]).replace("-", " ")
        p["chain"] = (brand_counts[p["brand"] or norm(p["name"])] >= 5 or any(n.startswith(f) or f" {f}" in f" {n}" for f in FRANCHISES)
                      or (bool(p["brand"]) and brand_counts[p["brand"]] >= 3))
    # one row per business: merge same-name listings in the same category, keep the most complete one
    merged = {}
    for p in sorted(places, key=lambda p: (-bool(p["phone"]), -bool(p["website"]), -p["conf"])):
        k = (p["cat"], norm(p["name"]))
        seen = hist["first"].get(p["pid"], release.split(".")[0])
        if k in merged:
            merged[k]["locations"] += 1
            merged[k]["added"] = min(merged[k]["added"], seen)   # a business is as old as its oldest listing
        else:
            p["locations"] = 1
            p["added"] = seen
            merged[k] = p
    places = list(merged.values())
    for p in places:
        for f in ("brand", "conf"):
            p.pop(f, None)
    match_sba(places)
    by_cat = Counter(p["cat"] for p in places if not p["chain"])
    log("independents by category: " + ", ".join(f"{k} {v}" for k, v in by_cat.most_common()))
    json.dump(places, open(os.path.join(DATA, "places.json"), "w"), separators=(",", ":"))
    json.dump(hist, open(HIST, "w"), separators=(",", ":"))
    json.dump(dict(allcats.most_common(400)), open(os.path.join(DATA, "place_categories.json"), "w"), indent=0)
    if os.path.exists(RAW):
        os.remove(RAW)


if __name__ == "__main__":
    main()
