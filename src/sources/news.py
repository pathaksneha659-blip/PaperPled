import calendar
import json
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser
from html.parser import HTMLParser

from src.config import settings
from src.core.http import HttpClient
from src.core.run_stats import record
from src.logging_setup import log
from src.schemas import NewsContent, NewsRecord, SourceInfo

NEWS_FEED = (
    "https://news.google.com/rss/search?"
    "q=AI+startup+OR+artificial+intelligence+when%3A1d"
    "&hl=en-US&gl=US&ceid=US:en"
)


class _PlainTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def _entry_date(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)
    raw = entry.get("published") or entry.get("updated")
    if not raw:
        return None
    try:
        value = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _plain_text(value: str) -> str:
    parser = _PlainTextParser()
    parser.feed(value)
    return " ".join("".join(parser.parts).split())


async def run_news_extractor(
    http: HttpClient,
    target_count: int = 1000,
    now: datetime | None = None,
) -> list[NewsRecord]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    if target_count <= 0:
        return []
    feed_text = await http.get(NEWS_FEED, as_json=False)
    parsed_feed = feedparser.parse(feed_text)
    if parsed_feed.bozo and not parsed_feed.entries:
        raise ValueError(f"Unable to parse the Google News RSS feed: {parsed_feed.bozo_exception}")

    records: list[NewsRecord] = []
    seen_urls = set()
    for entry in parsed_feed.entries:
        title = entry.get("title")
        url = entry.get("link")
        published_at = _entry_date(entry)
        if (
            not isinstance(title, str)
            or not title.strip()
            or not isinstance(url, str)
            or not url
            or published_at is None
            or not cutoff <= published_at <= now
            or url in seen_urls
        ):
            continue
        seen_urls.add(url)

        source = entry.get("source") or {}
        publisher = source.get("title") if isinstance(source, dict) else None
        summary = entry.get("summary")
        records.append(NewsRecord(
            source=SourceInfo(name="Google News RSS", url=url),
            content=NewsContent(
                title=title.strip(),
                publisher=publisher,
                date=published_at.isoformat(),
                summary=_plain_text(summary) if summary else None,
            ),
            collectedAt=now.isoformat(),
        ))
        if len(records) >= target_count:
            break

    out_file = settings.OUTPUT_DIR / "news.jsonl"
    with out_file.open("w", encoding="utf-8") as handle:
        for record_item in records:
            handle.write(json.dumps(record_item.model_dump(), ensure_ascii=False) + "\n")

    record("news.source_counts", records=len(records), feed="Google News RSS")
    log.info("news.saved_to_disk", count=len(records), path=str(out_file))
    return records