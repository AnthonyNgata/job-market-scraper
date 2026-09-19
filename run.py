#!/usr/bin/env python3
"""
Master script for the job market scraper pipeline.

    scrape -> archive raw -> clean -> export CSV + JSON + insights

Usage
-----
    python run.py --sample                 # offline demo against samples/
    python run.py --query "data engineer"  # live scrape
    python run.py --from-raw data/raw/jobs_20260918T140355Z.json
    python run.py --sample --verbose

Exit codes
----------
    0  success
    1  pipeline ran but produced no listings
    2  unrecoverable error
"""

from __future__ import annotations

import argparse
import logging
import sys

from config import settings
from src import api_source, pipeline, scraper, storage

logger = logging.getLogger("run")


def configure_logging(verbose: bool = False) -> None:
    """Send logs to stdout so GitHub Actions captures them in the job log."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # requests/urllib3 are chatty at DEBUG and drown out our own messages.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape, clean and export tech job listings.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--sample",
        action="store_true",
        help="parse the bundled sample HTML instead of making network requests",
    )
    source.add_argument(
        "--sample-api",
        action="store_true",
        help="normalise the bundled sample API payloads instead of making network requests",
    )
    source.add_argument(
        "--from-raw",
        metavar="PATH",
        help="re-clean an existing raw JSON archive without re-scraping",
    )
    parser.add_argument("--query", default=settings.DEFAULT_QUERY, help="search term")
    parser.add_argument(
        "--pages", type=int, default=settings.MAX_PAGES, help="max pages to walk"
    )
    parser.add_argument(
        "--no-archive", action="store_true", help="skip writing the raw JSON snapshot"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def print_summary(insights: dict, exports: dict) -> None:
    """Human-readable run report — the bit you actually read in the CI log."""
    top_key = f"top_{settings.TOP_N_SKILLS}_skills"

    print("\n" + "=" * 58)
    print("  JOB MARKET SCRAPE — SUMMARY")
    print("=" * 58)
    print(f"  Listings (cleaned)  : {insights.get('total_listings', 0)}")
    print(f"  Unique companies    : {insights.get('unique_companies', 0)}")
    print(f"  Remote listings     : {insights.get('remote_listings', 0)}")

    print(f"\n  TOP {settings.TOP_N_SKILLS} SKILLS IN DEMAND")
    print("  " + "-" * 44)
    skills = insights.get(top_key, [])
    if skills:
        for rank, row in enumerate(skills, start=1):
            print(
                f"  {rank}. {row['skill']:<22} "
                f"{row['job_count']:>3} jobs  ({row['pct_of_listings']}%)"
            )
    else:
        print("  (no skills matched the vocabulary)")

    print("\n  OUTPUT FILES")
    print("  " + "-" * 44)
    for label, path in exports.items():
        status = path if path else "FAILED"
        print(f"  {label:<10}: {status}")
    print("=" * 58 + "\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose)

    try:
        storage.ensure_directories()

        # ---- 1. Acquire listings ------------------------------------------
        if args.from_raw:
            logger.info("Re-cleaning from raw archive: %s", args.from_raw)
            listings = storage.load_raw(args.from_raw)
        elif args.sample:
            logger.info("Sample mode — parsing %s", settings.SAMPLE_HTML)
            listings = scraper.scrape_local(settings.SAMPLE_HTML)
        elif args.sample_api:
            logger.info("Sample mode — normalising %s", settings.SAMPLE_API_JSON)
            listings = api_source.load_sample(settings.SAMPLE_API_JSON)
        elif settings.SOURCE == "api":
            logger.info(
                "Querying %s API for %r (max %d page(s))",
                settings.API_PROVIDER, args.query, args.pages,
            )
            listings = api_source.fetch(query=args.query, max_pages=args.pages)
        else:
            logger.info("Scraping %r (max %d page(s))", args.query, args.pages)
            listings = scraper.scrape(query=args.query, max_pages=args.pages)

        if not listings:
            logger.error("No listings collected — nothing to clean or export.")
            return 1

        # ---- 2. Archive raw before cleaning -------------------------------
        # Skipped when re-cleaning, otherwise we'd duplicate the archive we
        # just read from.
        if not args.no_archive and not args.from_raw:
            storage.save_raw(listings)

        # ---- 3. Clean + analyse -------------------------------------------
        df, insights = pipeline.clean(listings)

        if df.empty:
            logger.error("All %d listing(s) were filtered out during cleaning.", len(listings))
            return 1

        # ---- 4. Export ----------------------------------------------------
        exports = storage.export_all(df, insights)

        print_summary(insights, exports)

        if any(path is None for path in exports.values()):
            logger.error("One or more exports failed — see log above.")
            return 2

        return 0

    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        return 2
    except Exception:
        # Full traceback, because an unexpected failure in a cron job is only
        # debuggable from the log it left behind.
        logger.exception("Pipeline failed with an unhandled error")
        return 2


if __name__ == "__main__":
    sys.exit(main())
