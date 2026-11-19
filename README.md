# KC Roll-Up Target Finder

Every SBA 7(a) and 504 loan to a Kansas City metro business since FY2020, on one map, refreshed automatically.

**Live map:** see the GitHub Pages link in this repo's About section.

## What it shows
A sourcing tool for roll-up and lower middle market private equity, built on every SBA 7(a) and 504 loan to a Kansas City metro business since FY2010.

- **Find targets:** pick one of 13 common roll-up categories (HVAC & plumbing, dental, vet, landscaping and others), a platform location and a radius. Get every SBA-financed business nearby, flagged for long-time owners (first SBA loan 10+ years ago, a rough succession signal) and businesses that have sold before. Export to CSV.
- **Market:** businesses, long-time owners and past sales by category, plus businesses sold per year with median deal loan.

## How it runs
1. A GitHub Actions job (`.github/workflows/refresh.yml`) runs on the 5th of every month.
2. `scripts/pipeline.py` checks SBA's dataset page for a newer quarterly file. If not, it logs the check and stops.
3. If there is, it downloads the 7(a) (FY2010+) and 504 files, keeps KC-metro loans, places each at its ZIP code, and compares against the last snapshot.
4. It commits the new `data/*.json`, and GitHub Pages serves the updated map.

## Notes
- Source: U.S. Small Business Administration 7(a) & 504 FOIA data, a public dataset.
- Loans are placed at ZIP-code level, not exact street address.
- Canceled and unfunded loans are excluded.
