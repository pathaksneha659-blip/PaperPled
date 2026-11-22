"""FrontierAtlas Main Orchestration Engine.

Bulk data ingestion, entity resolution, and export engine across five AI verticals:
Startups, Products, Research Papers, Jobs, and News.
"""
import argparse
import asyncio
import json
import sys
import time
import warnings
from pathlib import Path

# Event loop policy configuration for Windows platform compatibility
if sys.platform == "win32" and sys.version_info < (3, 14):
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            category=DeprecationWarning,
            message=r"'asyncio\.(WindowsSelectorEventLoopPolicy|set_event_loop_policy)' is deprecated.*",
        )
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # pyright: ignore[reportDeprecated]
        except Exception:
            pass

from src.config import settings
from src.core.http import HttpClient
from src.core.run_stats import record
from src.export.csv_export import run_csv_export
from src.logging_setup import log, setup_logging
from src.resolve.apply import main as apply_resolution
from src.sources.jobs import run_jobs_extractor
from src.sources.news import run_news_extractor
from src.sources.papers import run_papers_extractor
from src.sources.products import run_products_extractor
from src.sources.startups import run_startups_extractor


def handle_export(
    tabs: str | None = None,
    export_format: str = "csv",
    output_dir: Path | None = None,
) -> int:
    """Export datasets to local CSV.

    Returns:
        0 on success, 1 on unhandled errors.
    """
    selected_tabs = [t.strip() for t in tabs.split(",") if t.strip()] if tabs else None
    has_error = False
    base_dir = output_dir or settings.OUTPUT_DIR

    # 1. Local CSV Export (default, zero credentials, Excel UTF-8-sig format)
    if export_format in ("csv", "all"):
        log.info("export.csv_start", tabs=selected_tabs)
        results = run_csv_export(
            input_dir=base_dir,
            output_dir=base_dir / "csv",
            datasets=selected_tabs,
        )

        print("\n" + "=" * 70)
        print("  LOCAL CSV EXPORT RESULTS")
        print("=" * 70)
        print(f"{'dataset':<20}{'status':<10}{'rows':>8}{'cols':>6}   path")
        print("-" * 70)
        for r in results:
            cols = r.get("columns", 0)
            print(f"{r['dataset']:<20}{r['status']:<10}{r['rows']:>8}{cols:>6}   {r['path']}")
            if r.get("error"):
                print(f"  ↳ Error: {r['error']}")
        print("=" * 70)

        if any(r.get("status") == "error" for r in results):
            has_error = True

    return 1 if has_error else 0


def handle_stats(output_dir: Path | None = None) -> None:
    """Display recorded pipeline run metrics from run_stats.json."""
    stats_file = (output_dir or settings.OUTPUT_DIR) / "run_stats.json"
    if not stats_file.exists():
        print(f"No run statistics found at: {stats_file}")
        return

    try:
        with stats_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        print("\n" + "=" * 60)
        print("  FRONTIERATLAS RECORDED PIPELINE METRICS")
        print("=" * 60)
        print(json.dumps(data, indent=2, ensure_ascii=False))
        print("=" * 60)
    except Exception as exc:
        print(f"Error reading statistics: {exc}", file=sys.stderr)


async def run_pipeline(args: argparse.Namespace) -> int:
    """Execute the ingestion and export pipeline according to CLI args."""
    start_time = time.perf_counter()
    command = args.command
    output_dir = Path(args.output_dir) if args.output_dir else settings.OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    # Standalone utilities
    if command == "stats":
        handle_stats(output_dir=output_dir)
        return 0

    if command == "resolve":
        log.info("pipeline.resolve_start")
        apply_resolution()
        log.info("pipeline.resolve_completed")
        return 0

    if command in ("export", "export-csv"):
        fmt = "csv" if command == "export-csv" else args.format
        return handle_export(tabs=args.tabs, export_format=fmt, output_dir=output_dir)

    # Web extraction pipeline
    http = HttpClient(
        total_concurrency=args.concurrency,
        per_host_concurrency=args.per_host,
        timeout=args.timeout,
    )
    log.info("pipeline.phase1_start", mode=command, limit=args.limit, concurrency=args.concurrency)
    stats: dict[str, object] = {"mode": command, "limit": args.limit}

    try:
        if command in ("startups", "all", "full"):
            records = await run_startups_extractor(http, target_count=args.limit)
            stats["startups"] = len(records)

        if command in ("products", "all", "full"):
            records = await run_products_extractor(http, target_count=args.limit)
            stats["products"] = len(records)

        if command in ("papers", "all", "full"):
            records = await run_papers_extractor(http, target_count=args.limit)
            stats["papers"] = len(records)

        if command in ("jobs", "all", "full"):
            records = await run_jobs_extractor(http, target_count=args.limit)
            stats["jobs"] = len(records)

        if command in ("news", "all", "full"):
            records = await run_news_extractor(http, target_count=args.limit)
            stats["news"] = len(records)

    finally:
        await http.close()
        elapsed = round(time.perf_counter() - start_time, 2)
        stats["duration_seconds"] = elapsed
        record("orchestrator.stats", **stats)
        log.info("pipeline.phase1_completed", elapsed_seconds=elapsed)

    # Print summary of extracted rows
    print("\n" + "=" * 50)
    print("  FRONTIERATLAS EXTRACTION SUMMARY")
    print("=" * 50)
    print(f"Command / Mode:   {command}")
    print(f"Record Limit:     {args.limit} records/vertical")
    print(f"Elapsed Time:     {elapsed:.2f}s")
    print("-" * 50)
    for key in ("startups", "products", "papers", "jobs", "news"):
        if key in stats:
            print(f"  ↳ {key.capitalize():<15}: {stats[key]:>6} records")
    print("=" * 50)

    # Post-scraping chained pipeline actions
    if command == "full" or args.auto_resolve:
        print("\n[Step 2/3] Running Entity Resolution...")
        log.info("pipeline.auto_resolve_start")
        apply_resolution()
        log.info("pipeline.auto_resolve_completed")

    if command == "full" or args.auto_export:
        print("\n[Step 3/3] Exporting Datasets...")
        return handle_export(tabs=args.tabs, export_format=args.format, output_dir=output_dir)

    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Configure and parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="FrontierAtlas Bulk AI Market Intelligence Ingestion & Export Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python -m src.main all --limit 1000       Scrape all 5 verticals up to 1000 records each
  python -m src.main jobs                   Scrape fresh AI jobs from Ashby boards (<24h)
  python -m src.main news                   Scrape fresh AI news from Google News RSS (<24h)
  python -m src.main resolve                Run entity resolution against seed catalog
  python -m src.main export                 Export all ingested JSONL datasets to CSV
  python -m src.main export --tabs Jobs     Export only specified datasets to CSV
  python -m src.main full --limit 1000      End-to-end: scrape all -> resolve -> export CSV
  python -m src.main stats                  Display recorded pipeline run metrics
        """,
    )
    parser.add_argument(
        "command",
        choices=[
            "startups", "products", "papers", "jobs", "news",
            "all", "resolve", "export", "export-csv",
            "full", "stats",
        ],
        default="all",
        nargs="?",
        help="The vertical or pipeline command to execute (default: all)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Minimum records to extract per vertical (default: 1000)",
    )
    parser.add_argument(
        "--tabs",
        type=str,
        default=None,
        help="Comma-separated dataset names/tabs to export (e.g. 'Startups,Products')",
    )
    parser.add_argument(
        "--format",
        choices=["csv"],
        default="csv",
        help="Export format (default: csv)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=100,
        help="Total global concurrent HTTP connections (default: 100)",
    )
    parser.add_argument(
        "--per-host",
        type=int,
        default=5,
        help="Max concurrent HTTP connections per target host (default: 5)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="HTTP request timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--auto-resolve",
        action="store_true",
        help="Automatically trigger entity resolution after extraction",
    )
    parser.add_argument(
        "--auto-export",
        action="store_true",
        help="Automatically trigger dataset export after extraction / resolution",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Custom output directory path (default: output/)",
    )

    args = parser.parse_args(argv)
    # Backward compatibility for any internal callers expecting args.vertical
    args.vertical = args.command
    return args


def main() -> None:
    """CLI entrypoint."""
    setup_logging()
    args = parse_args()
    command = args.command
    output_dir = Path(args.output_dir) if args.output_dir else settings.OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Fast synchronous execution for utilities that do not require async I/O
        if command == "stats":
            handle_stats(output_dir=output_dir)
            sys.exit(0)

        if command == "resolve":
            log.info("pipeline.resolve_start")
            apply_resolution()
            log.info("pipeline.resolve_completed")
            sys.exit(0)

        if command in ("export", "export-csv"):
            fmt = "csv" if command == "export-csv" else args.format
            code = handle_export(tabs=args.tabs, export_format=fmt, output_dir=output_dir)
            sys.exit(code)

        # Asynchronous ingestion pipeline
        exit_code = asyncio.run(run_pipeline(args))
        sys.exit(exit_code or 0)
    except KeyboardInterrupt:
        print("\n[Interrupted] Pipeline execution cancelled by user.")
        sys.exit(130)
    except Exception as exc:
        log.error("pipeline.fatal_error", error=str(exc))
        print(f"\n[FATAL ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
