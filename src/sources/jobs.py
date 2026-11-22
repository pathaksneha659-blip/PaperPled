import json
import re
import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp
from selectolax.lexbor import LexborHTMLParser

from src.config import settings
from src.core.http import HttpClient
from src.core.run_stats import record
from src.logging_setup import log
from src.schemas import JobContent, JobRecord, SourceInfo

ASHBY_BOARDS = {
    "cursor": "Cursor",
    "openai": "OpenAI",
    "perplexity": "Perplexity",
    "cerebras": "Cerebras",
    "harvey": "Harvey",
}


def _plain_text(html: str) -> str:
    return " ".join(LexborHTMLParser(html).text().split())


async def run_jobs_extractor(
    http: HttpClient,
    target_count: int = 1000,
    now: datetime | None = None,
) -> list[JobRecord]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    records: list[JobRecord] = []
    seen_urls = set()
    if target_count <= 0:
        return records

    for board_slug, company in ASHBY_BOARDS.items():
        url = f"https://api.ashbyhq.com/posting-api/job-board/{board_slug}"
        try:
            response = await http.get(url, as_json=True)
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as error:
            log.warning("jobs.board_error", board=board_slug, error=str(error))
            continue

        for job in response.get("jobs", []):
            job_url = job.get("jobUrl")
            published = job.get("publishedAt")
            title = job.get("title")
            if not all(isinstance(value, str) and value for value in (job_url, published, title)):
                continue
            try:
                published_at = datetime.fromisoformat(published.replace("Z", "+00:00"))
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
                published_at = published_at.astimezone(timezone.utc)
            except ValueError:
                continue
            if not cutoff <= published_at <= now or job_url in seen_urls:
                continue
            seen_urls.add(job_url)

            description_html = job.get("descriptionHtml") or ""
            description = _plain_text(description_html) if description_html else None
            description = re.sub(r"\s+", " ", description).strip() if description else None
            records.append(JobRecord(
                source=SourceInfo(name=f"{company} Ashby job board", url=job_url),
                content=JobContent(
                    title=title.strip(),
                    companyName=company,
                    date=published_at.isoformat(),
                    location=job.get("location"),
                    employmentType=job.get("employmentType"),
                    description=description,
                ),
                collectedAt=now.isoformat(),
            ))
            if len(records) >= target_count:
                break
        if len(records) >= target_count:
            break

    out_file = settings.OUTPUT_DIR / "jobs.jsonl"
    with out_file.open("w", encoding="utf-8") as handle:
        for record_item in records:
            handle.write(json.dumps(record_item.model_dump(), ensure_ascii=False) + "\n")

    record("jobs.source_counts", records=len(records), boards=len(ASHBY_BOARDS))
    log.info("jobs.saved_to_disk", count=len(records), path=str(out_file))
    return records