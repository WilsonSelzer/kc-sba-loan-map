# KC Small Business Lending Map

Every SBA 7(a) and 504 loan to a Kansas City metro business since FY2020, on one map, refreshed automatically.

**Live map:** see the GitHub Pages link in this repo's About section.

## What it shows
- ~2,700 loans and ~$1.5B in approvals across 14 metro counties in Missouri and Kansas
- Loans used to **buy an existing business** (SBA's "Change of Ownership" flag) highlighted, since that's where small-business M&A shows up
- Filters for industry, loan size, year, status, program and lender
- A **"What changed this quarter"** panel: new loans, new acquisitions, most active lenders, and loans that changed status (for example, charge-offs)

## How it runs
1. A GitHub Actions job (`.github/workflows/refresh.yml`) runs on the 5th of every month.
2. `scripts/pipeline.py` checks SBA's dataset page for a newer quarterly file. If not, it logs the check and stops.
3. If there is, it downloads the 7(a) and 504 files, keeps KC-metro loans, places each at its ZIP code, and compares against the last snapshot.
4. It commits the new `data/*.json`, and GitHub Pages serves the updated map.

## Notes
- Source: U.S. Small Business Administration 7(a) & 504 FOIA data, a public dataset.
- Loans are placed at ZIP-code level, not exact street address.
- Canceled and unfunded loans are excluded.
