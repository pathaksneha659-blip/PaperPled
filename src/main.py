import sys
import asyncio
import argparse

# CRITICAL WINDOWS FIX: Fixes aiohttp cleanup crashes on Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from src.logging_setup import setup_logging, log
from src.core.http import HttpClient
from src.sources.startups import run_startups_extractor
from src.sources.products import run_products_extractor
from src.sources.papers import run_papers_extractor

async def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="FrontierAtlas Bulk Data Ingestion Engine")
    parser.add_argument("vertical", choices=["startups", "products", "papers", "all"], default="all", nargs="?",
                        help="The vertical to scrape (default: all)")
    parser.add_argument("--limit", type=int, default=1000, help="Minimum records to extract per vertical (default: 1000)")
    args = parser.parse_args()

    http = HttpClient(total_concurrency=100, per_host_concurrency=5)
    log.info("pipeline.phase1_start", mode=args.vertical, limit=args.limit)

    try:
        if args.vertical in ("startups", "all"):
            await run_startups_extractor(http, target_count=args.limit)

        if args.vertical in ("products", "all"):
            await run_products_extractor(http, target_count=args.limit)

        if args.vertical in ("papers", "all"):
            await run_papers_extractor(http, target_count=args.limit)

    finally:
        await http.close()
        log.info("pipeline.phase1_completed")

if __name__ == "__main__":
    asyncio.run(main())