"""One-time backfill: walk the last few monthly Overture releases, oldest first, and record the release each
KC business first appeared in. After this, places.py adds one release per month on its own.

    python scripts/backfill_history.py 6     # the last 6 releases before the latest
"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import places as P

n = int(sys.argv[1]) if len(sys.argv) > 1 else 6
allr, latest = P.releases()
older = [r for r in allr if r < latest][-n:]
hist = P.load_history()
done = {r["release"] for r in hist["releases"]}
for rel in older:
    if rel in done:
        continue
    tmp = os.path.join(tempfile.gettempdir(), f"kc_{rel}.geojsonseq")
    try:
        P.download(rel, tmp)
    except Exception as e:
        P.log(f"skipping {rel}: {e}")
        continue
    P.RAW = tmp
    found, _ = P.parse()
    P.record(hist, rel, [p["pid"] for p in found])
    os.remove(tmp)
import json
json.dump(hist, open(P.HIST, "w"), separators=(",", ":"))
P.log(f"backfilled {len(hist['releases'])} releases")
