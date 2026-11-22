import sys
import asyncio
import argparse
import warnings

# CRITICAL WINDOWS FIX: Fixes aiohttp cleanup crashes on Windows
if sys.platform == "win32":
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            category=DeprecationWarning,
            message=r"'asyncio\.(WindowsSelectorEventLoopPolicy|set_event_loop_policy)' is deprecated.*",
        )
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # pyright: ignore[reportDeprecated]

from src.logging_setup import setup_logging, log
from src.core.http import HttpClient
from src.sources.startups import run_startups_extractor
from src.sources.products import run_products_extractor
from src.sources.papers import run_papers_extractor
from src.sources.jobs import run_jobs_extractor
from src.sources.news import run_news_extractor
from src.resolve.apply import main as apply_resolution
from src.core.run_stats import record
from src.export.sheets import export as export_sheets

async def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="FrontierAtlas Bulk Data Ingestion Engine")
    parser.add_argument("vertical", choices=["startups", "products", "papers", "jobs", "news", "all", "resolve", "export"], default="all", nargs="?",
                        help="The vertical to scrape (default: all)")
    parser.add_argument("--limit", type=int, default=1000, help="Minimum records to extract per vertical (default: 1000)")
    parser.add_argument("--tabs", help="Comma-separated export tabs, e.g. Startups,Products")
    args = parser.parse_args()

    if args.vertical == "resolve":
        apply_resolution()
        return
    if args.vertical == "export":
        tabs = [tab.strip() for tab in args.tabs.split(",") if tab.strip()] if args.tabs else None
        export_sheets(tabs=tabs)
        return

    http = HttpClient(total_concurrency=100, per_host_concurrency=5)
    log.info("pipeline.phase1_start", mode=args.vertical, limit=args.limit)
    stats = {"mode": args.vertical, "limit": args.limit}

    try:
        if args.vertical in ("startups", "all"):
            stats["startups"] = len(await run_startups_extractor(http, target_count=args.limit))

        if args.vertical in ("products", "all"):
            stats["products"] = len(await run_products_extractor(http, target_count=args.limit))

        if args.vertical in ("papers", "all"):
            stats["papers"] = len(await run_papers_extractor(http, target_count=args.limit))

        if args.vertical in ("jobs", "all"):
            stats["jobs"] = len(await run_jobs_extractor(http, target_count=args.limit))

        if args.vertical in ("news", "all"):
            stats["news"] = len(await run_news_extractor(http, target_count=args.limit))

    finally:
        await http.close()
        record("orchestrator.stats", **stats)
        log.info("pipeline.phase1_completed")

if __name__ == "__main__":
    asyncio.run(main())