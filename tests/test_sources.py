import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from src.config import settings
from src.sources import jobs, news, products


class FakeHttp:
    def __init__(self, ashby_response=None, feed_text="", hf_response=None):
        self.ashby_response = ashby_response or {"jobs": []}
        self.feed_text = feed_text
        self.hf_response = hf_response or []

    async def get(self, url, as_json=False, **kwargs):
        if "api.ashbyhq.com" in url:
            return self.ashby_response
        if "huggingface.co" in url:
            return self.hf_response
        return self.feed_text


@pytest.mark.asyncio
async def test_jobs_keep_only_fresh_postings(monkeypatch, tmp_path):
    now = datetime(2026, 11, 21, 18, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(jobs, "record", lambda *args, **kwargs: None)
    http = FakeHttp(ashby_response={"jobs": [
        {
            "title": "Research Engineer",
            "publishedAt": (now - timedelta(hours=1)).isoformat(),
            "jobUrl": "https://jobs.ashbyhq.com/openai/1",
            "descriptionHtml": "<p>Build <b>AI</b> systems.</p>",
        },
        {
            "title": "Old Role",
            "publishedAt": (now - timedelta(hours=25)).isoformat(),
            "jobUrl": "https://jobs.ashbyhq.com/openai/2",
        },
    ]})

    result = await jobs.run_jobs_extractor(http, now=now)

    assert len(result) == 1
    assert result[0].content.date == (now - timedelta(hours=1)).isoformat()
    assert result[0].content.description == "Build AI systems."
    stored = [json.loads(line) for line in (tmp_path / "jobs.jsonl").read_text().splitlines()]
    assert stored[0]["recordType"] == "JOB"


@pytest.mark.asyncio
async def test_news_keeps_only_fresh_rss_entries(monkeypatch, tmp_path):
    now = datetime(2026, 11, 21, 18, 30, tzinfo=timezone.utc)
    fresh_date = format_datetime(now - timedelta(hours=2), usegmt=True)
    stale_date = format_datetime(now - timedelta(hours=30), usegmt=True)
    feed = f"""<?xml version="1.0"?>
    <rss version="2.0"><channel><title>News</title>
      <item><title>Fresh AI news</title><link>https://news.example/fresh</link>
        <pubDate>{fresh_date}</pubDate><description><![CDATA[<p>New launch</p>]]></description>
      </item>
      <item><title>Old AI news</title><link>https://news.example/old</link>
        <pubDate>{stale_date}</pubDate>
      </item>
    </channel></rss>"""
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(news, "record", lambda *args, **kwargs: None)

    result = await news.run_news_extractor(FakeHttp(feed_text=feed), now=now)

    assert len(result) == 1
    assert result[0].content.title == "Fresh AI news"
    assert result[0].content.summary == "New launch"
    stored = [json.loads(line) for line in (tmp_path / "news.jsonl").read_text().splitlines()]
    assert stored[0]["recordType"] == "NEWS"


@pytest.mark.asyncio
async def test_products_have_unique_names_and_unverified_pricing_is_null(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    http = FakeHttp(hf_response=[
            {"id": "owner/first", "cardData": {"title": "Repeated title"}},
            {"id": "owner/second", "cardData": {"title": "Repeated title"}},
            {"id": "owner/third", "cardData": {"title": "Different title"}},
        ])

    result = await products.run_products_extractor(http, target_count=3)

    names = [item.content.productName for item in result]
    assert len(set(names)) == 3
    assert all(item.content.pricingModel is None for item in result)
    stored = [json.loads(line) for line in (tmp_path / "products.jsonl").read_text().splitlines()]
    assert len(stored) == 3
