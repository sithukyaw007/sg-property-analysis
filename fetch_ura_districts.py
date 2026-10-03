#!/usr/bin/env python3
"""Fetch URA transaction data by postal district (D01-D28).

Instead of searching project-by-project (which requires knowing every condo name),
this searches by postal district and retrieves ALL condo transactions in that
district for the last 5 years in a single request.

28 requests cover the entire Singapore condo market vs hundreds for individual projects.

Data source: URA Property Market Information
URL: https://eservice.ura.gov.sg/property-market-information/pmiResidentialTransactionSearch

Usage:
    # Fetch all districts (D01-D28)
    python fetch_ura_districts.py

    # Fetch specific districts
    python fetch_ura_districts.py --districts 3,5,14,15

    # Force re-fetch even if cached
    python fetch_ura_districts.py --districts 5 --force

    # Set max age for cache freshness (default: 30 days)
    python fetch_ura_districts.py --max-age 14

    # Build/update ura_cache.json after fetching
    python fetch_ura_districts.py --districts 3,5 --build-cache
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from scrapers.ura_scraper import parse_ura_csv

DATA_DIR = Path(__file__).parent / "data"
URA_CACHE_FILE = DATA_DIR / "ura_cache.json"
URA_URL = "https://eservice.ura.gov.sg/property-market-information/pmiResidentialTransactionSearch"

# All valid postal districts in Singapore
ALL_DISTRICTS = [
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
    16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28,
]

# URA's compulsory property-type dropdown for postal-district searches.
# "condo" is the historical default; "ec" lets EC prints be fetched too —
# 250 EC listings in the DB had no URA prints at all because the fetch
# hardcoded Apartments & Condominiums.
PROPERTY_TYPE_LABELS = {
    "condo": "Apartments & Condominiums",
    "ec": "Executive Condominium",
}


def normalize_property_type(value: str) -> str:
    """Accept short keys ('ec') or full URA labels ('Executive Condominium')."""
    v = value.strip().lower()
    if v in PROPERTY_TYPE_LABELS:
        return v
    for key, label in PROPERTY_TYPE_LABELS.items():
        if v == label.lower():
            return key
    raise ValueError(
        f"Unknown property type {value!r} — expected one of "
        f"{sorted(PROPERTY_TYPE_LABELS)} or their full URA labels")


def district_csv_path(district: int, property_type: str = "condo") -> Path:
    """Path to saved CSV for a district (+ property type).

    The condo CSV keeps its historical name (downstream joins read
    ura_district_D{NN}.csv); other types get a suffix. Both shapes match the
    cache builder's ura_district_D*.csv glob.
    """
    suffix = "" if property_type == "condo" else f"_{property_type.upper()}"
    return DATA_DIR / f"ura_district_D{district:02d}{suffix}.csv"


def is_fresh(csv_path: Path, max_age_days: int) -> bool:
    """Check if a cached CSV file is fresh enough."""
    if not csv_path.exists():
        return False
    mtime = datetime.fromtimestamp(csv_path.stat().st_mtime)
    age_days = (datetime.now() - mtime).days
    return age_days <= max_age_days


def fetch_district(page, district: int,
                   property_type: str = "condo") -> tuple[str | None, str | None]:
    """Fetch URA CSV for one postal district.

    URA search popup UI structure (discovered via browser inspection):
    - Click "button.search-popup-btn" to open popup
    - Popup has tabs: "Project" (default) and "Postal District"
    - "Postal District" tab link has href="#postalDistrict"
    - District checkboxes labeled "D{NN} / {area description}"
    - Property Type dropdown is COMPULSORY for postal district searches
    - Must select a type label (PROPERTY_TYPE_LABELS) before searching

    Returns (csv_content, error_message). One of them will be None.
    """
    district_str = f"D{district:02d}"
    type_label = PROPERTY_TYPE_LABELS[property_type]

    try:
        # Navigate fresh each time (most reliable)
        page.goto(URA_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2000)

        # Open the search popup (project/location selector)
        page.locator("button.search-popup-btn").click()
        page.wait_for_timeout(1000)

        # Switch to "Postal District" tab
        # Tab is an <a> link with href="#postalDistrict"
        postal_tab = page.locator('a[href="#postalDistrict"]')
        if postal_tab.count() == 0:
            # Fallback: try matching by text
            postal_tab = page.locator('a:has-text("Postal District")')
        if postal_tab.count() == 0:
            return None, "Could not find 'Postal District' tab in popup"
        postal_tab.first.click()
        page.wait_for_timeout(500)

        # Find and check the district checkbox
        # Checkboxes are labeled like "D05 / Pasir Panjang, Hong Leong Garden, Clementi New Town"
        # The checkbox input is inside a clickable container div
        cb = page.locator('input[type="checkbox"]').filter(
            has=page.locator(f'text=/{district_str} \\//'),
        )
        if cb.count() == 0:
            # Try clicking the container that has the district text
            container = page.locator(f'text=/{district_str} \\//')
            if container.count() > 0:
                container.first.click()
                page.wait_for_timeout(300)
            else:
                # Last resort: find by exact checkbox value
                for val in [district_str, f"{district:02d}", str(district)]:
                    cb = page.locator(f'input[type="checkbox"][value="{val}"]')
                    if cb.count() > 0:
                        cb.first.click()
                        page.wait_for_timeout(300)
                        break
                else:
                    return None, f"Could not find checkbox for {district_str}"
        else:
            cb.first.click()
            page.wait_for_timeout(300)

        # Click Apply to close popup
        page.locator('button:has-text("Apply"):visible').click()
        page.wait_for_timeout(500)

        # COMPULSORY: set the property type for postal district searches
        # (URA requires this; label comes from PROPERTY_TYPE_LABELS)
        page.locator('select').filter(has=page.locator(f'option:has-text("{type_label}")')).first.select_option(
            label=type_label
        )
        page.wait_for_timeout(300)

        # Click Search
        page.locator('button.btn-primary:has-text("Search")').click()
        page.wait_for_timeout(3000)

        # Wait for results (district queries can be large, allow more time)
        try:
            page.wait_for_selector("text=/Showing \\d+/", timeout=30000)
        except Exception:
            # Check if "No results" message appeared
            no_results = page.locator('text=/No results|No transaction/i')
            if no_results.count() > 0:
                return None, f"No results for {district_str}"
            return None, f"Timeout waiting for results for {district_str}"

        result_text = page.locator("text=/Showing \\d+/").first.inner_text()
        print(f"  {result_text}", file=sys.stderr)

        # Download CSV via the downloadCSV link
        csv_path = str(district_csv_path(district, property_type))
        with page.expect_download(timeout=30000) as dl_info:
            page.evaluate("document.querySelector('a.downloadCSV').scrollIntoView()")
            page.wait_for_timeout(500)
            page.evaluate("document.querySelector('a.downloadCSV').click()")

        dl = dl_info.value
        dl.save_as(csv_path)

        # URA exports are windows-1252 when a project name carries an accent
        # (e.g. ENCHANTÉ in D11) — a default-encoding read here made a
        # SUCCESSFUL download report FAILED.
        try:
            with open(csv_path, encoding="utf-8") as f:
                csv_content = f.read()
        except UnicodeDecodeError:
            with open(csv_path, encoding="windows-1252") as f:
                csv_content = f.read()

        return csv_content, None

    except Exception as e:
        return None, str(e)


def build_cache_from_district_csvs(districts: list[int] | None = None) -> dict:
    """Build/update ura_cache.json from district-level CSV files.

    Delegates to invest.build_ura_cache_from_csv — the single cache builder
    (per-project grouping, median PSF, en-bloc exclusion, district/tenure/
    lease metadata). This module previously had its own builder which wrote
    an older entry shape (no district/median_psf/lease fields) and silently
    diverged.
    """
    from invest import build_ura_cache_from_csv

    if districts:
        patterns = [str(district_csv_path(d, pt))
                    for d in districts for pt in PROPERTY_TYPE_LABELS
                    if district_csv_path(d, pt).exists()]
    else:
        patterns = [str(p) for p in sorted(DATA_DIR.glob("ura_district_D*.csv"))]

    if not patterns:
        print("No district CSV files found", file=sys.stderr)
        if URA_CACHE_FILE.exists():
            with open(URA_CACHE_FILE) as f:
                return json.load(f).get("projects", {})
        return {}

    return build_ura_cache_from_csv(patterns)


def main():
    parser = argparse.ArgumentParser(
        description="Fetch URA transaction data by postal district",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python fetch_ura_districts.py                        # All districts
  python fetch_ura_districts.py --districts 3,5,14,15  # Specific districts
  python fetch_ura_districts.py --districts 5 --force  # Force re-fetch
  python fetch_ura_districts.py --build-cache           # Rebuild cache from CSVs
        """,
    )
    parser.add_argument(
        "--districts", "-d", type=str,
        help="Comma-separated district numbers (default: all 28 districts)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-fetch even if cached CSV exists and is fresh",
    )
    parser.add_argument(
        "--max-age", type=int, default=30,
        help="Max age in days before re-fetching (default: 30)",
    )
    parser.add_argument(
        "--build-cache", action="store_true",
        help="Build/update ura_cache.json from district CSVs (no browser needed)",
    )
    parser.add_argument(
        "--property-types", type=str, default="condo",
        help="Comma-separated property types to fetch: "
             f"{sorted(PROPERTY_TYPE_LABELS)} or full URA labels "
             "(default: condo). 'ec' fetches Executive Condominium prints "
             "into ura_district_D{NN}_EC.csv.",
    )
    parser.add_argument(
        "--browser", action="store_true",
        help="use the old headed-browser download instead of the URA API",
    )
    args = parser.parse_args()

    # The official API (via sgprop, see ura_api.py) is the default: every
    # district in ~40 s, no browser. The browser path below is the fallback
    # for machines without sgprop or a URA key. EC files
    # (ura_district_D*_EC.csv) come from the same API export since the fork's
    # EC support landed, so an EC request no longer forces the browser.
    wants_ec = any(normalize_property_type(t) != "condo"
                   for t in args.property_types.split(",") if t.strip())
    if not args.build_cache and not args.browser:
        import ura_api
        ok, why = ura_api.available()
        if ok:
            print("\nURA API: syncing transactions (no browser)…", file=sys.stderr)
            out = ura_api.refresh(transactions=True, rentals=False, force=args.force,
                                  ec=wants_ec)
            print(json.dumps(out, indent=2), file=sys.stderr)
            return
        print(f"\nURA API unavailable ({why}); falling back to the browser.",
              file=sys.stderr)

    # Parse district list
    if args.districts:
        districts = [int(d.strip()) for d in args.districts.split(",")]
    else:
        districts = ALL_DISTRICTS

    property_types = [normalize_property_type(t)
                      for t in args.property_types.split(",") if t.strip()]

    # Build cache only mode (no browser)
    if args.build_cache:
        print("\nBuilding URA cache from district CSV files...", file=sys.stderr)
        build_cache_from_district_csvs(districts)
        return

    # Determine which (district, property_type) pairs to fetch
    to_fetch = []
    for d in districts:
        for pt in property_types:
            csv_path = district_csv_path(d, pt)
            if args.force or not is_fresh(csv_path, args.max_age):
                to_fetch.append((d, pt))

    print("\nURA District Fetch", file=sys.stderr)
    print(f"  Districts requested: {districts}", file=sys.stderr)
    print(f"  Property types: {property_types}", file=sys.stderr)
    print(f"  Already cached (fresh): {len(districts) * len(property_types) - len(to_fetch)}",
          file=sys.stderr)
    print(f"  To fetch: {len(to_fetch)}", file=sys.stderr)

    if not to_fetch:
        print("\nAll districts cached and fresh!", file=sys.stderr)
        print("Use --force to re-fetch or --build-cache to rebuild cache.", file=sys.stderr)
        # Still rebuild cache if needed
        build_cache_from_district_csvs(districts)
        return

    # Launch browser and fetch
    from playwright.sync_api import sync_playwright

    DATA_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        ok = 0
        fail = 0
        for i, (district, ptype) in enumerate(to_fetch, 1):
            print(f"\n[{i}/{len(to_fetch)}] District D{district:02d} "
                  f"({PROPERTY_TYPE_LABELS[ptype]})", file=sys.stderr)
            csv_content, error = fetch_district(page, district, property_type=ptype)

            if csv_content:
                transactions = parse_ura_csv(csv_content)
                # Count unique projects
                projects = set(t.project_name for t in transactions)
                print(f"  -> {len(transactions)} transactions, {len(projects)} projects",
                      file=sys.stderr)
                ok += 1
            else:
                print(f"  -> FAILED: {error}", file=sys.stderr)
                fail += 1

            # Small delay between requests
            if i < len(to_fetch):
                time.sleep(2)

        browser.close()

    print(f"\n{'='*50}", file=sys.stderr)
    print(f"Fetched: {ok} districts, {fail} failed", file=sys.stderr)

    # Build cache from all fetched district CSVs
    print("\nBuilding cache from district CSVs...", file=sys.stderr)
    cache = build_cache_from_district_csvs(districts)

    print(f"\n{'='*50}", file=sys.stderr)
    print(f"Done! {len(cache)} projects in cache", file=sys.stderr)


if __name__ == "__main__":
    main()
