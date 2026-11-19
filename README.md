# KC Small Business Lending Map

Every SBA 7(a) and 504 loan to a Kansas City metro business since FY2020, on one map, refreshed automatically.

**Live map:** see the GitHub Pages link in this repo's About section.

## What it shows
Built as a sourcing tool for roll-up and lower middle market private equity.

- **Overview:** ~6,300 SBA loans to ~5,300 KC metro businesses since FY2010 (14 counties in Missouri and Kansas), with a "What changed this quarter" feed: new loans, new acquisitions, most active lenders and charge-offs.
- **Add-on screener:** pick an industry, click the map to place a platform company, set a radius, and get every SBA borrower in that industry nearby. Flags long-time owners (first SBA loan 10+ years ago, a rough succession signal), businesses that changed hands, and repeat borrowers. Export the list to CSV with free Google and LinkedIn lookup links.
- **Industries:** a fragmentation table (operators, long-time owners, acquisitions and median loan by 6-digit NAICS) to spot roll-up markets.
- **Acquisitions:** loans SBA flags as a change of ownership (tracked since FY2018) by year, by industry and by lender.

## How it runs
1. A GitHub Actions job (`.github/workflows/refresh.yml`) runs on the 5th of every month.
2. `scripts/pipeline.py` checks SBA's dataset page for a newer quarterly file. If not, it logs the check and stops.
3. If there is, it downloads the 7(a) (FY2010+) and 504 files, keeps KC-metro loans, places each at its ZIP code, and compares against the last snapshot.
4. It commits the new `data/*.json`, and GitHub Pages serves the updated map.

## Notes
- Source: U.S. Small Business Administration 7(a) & 504 FOIA data, a public dataset.
- Loans are placed at ZIP-code level, not exact street address.
- Canceled and unfunded loans are excluded.
