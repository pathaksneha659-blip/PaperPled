from datetime import datetime, timedelta, timezone

import pytest

from src.core.dates import is_fresh, page_date_candidates, parse_date, pick_best


UTC = timezone.utc
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "raw",
    ["just now", "now", "2h", "3 hours ago", "5 min ago", "yesterday", "2 days ago"],
)
def test_relative_forms(raw):
    assert parse_date(raw, NOW, "visible_text") is not None


def test_iso_z():
    parsed = parse_date("2026-11-21T12:00:00Z", NOW, "jsonld")
    assert parsed is not None
    assert parsed.earliest == NOW
    assert parsed.latest == NOW


def test_iso_without_timezone_is_widened():
    parsed = parse_date("2026-11-21T12:00:00", NOW, "meta_og")
    assert parsed is not None
    assert parsed.earliest == datetime(2026, 11, 20, 22, tzinfo=UTC)
    assert parsed.latest == NOW


def test_rfc822():
    parsed = parse_date("Sat, 21 Nov 2026 12:00:00 GMT", NOW, "rss")
    assert parsed is not None
    assert parsed.earliest == NOW


def test_date_only_is_whole_utc_day_with_timezone_widening():
    parsed = parse_date("2026-11-21", NOW, "api_field")
    assert parsed is not None
    assert parsed.earliest == datetime(2026, 11, 20, 10, tzinfo=UTC)
    assert parsed.latest == NOW


def test_epoch_seconds_and_milliseconds():
    seconds = int(NOW.timestamp())
    milliseconds = seconds * 1000
    assert parse_date(seconds, NOW, "api_field").earliest == NOW
    assert parse_date(milliseconds, NOW, "api_field").earliest == NOW


def test_relative_two_hours_bounds():
    parsed = parse_date("2 hours ago", NOW, "visible_text")
    assert parsed.earliest == NOW - timedelta(hours=3)
    assert parsed.latest == NOW - timedelta(hours=2)


def test_fresh_strict_and_non_strict_boundaries():
    parsed = parse_date("2026-11-20T12:00:00Z", NOW, "jsonld")
    assert is_fresh(parsed, NOW, strict=True)
    assert is_fresh(parsed, NOW, strict=False)
    interval = parse_date("2 days ago", NOW, "visible_text")
    window = timedelta(hours=24)
    assert not is_fresh(interval, NOW, strict=True)
    assert not is_fresh(interval, NOW, strict=False)


def test_future_date_rejected():
    assert parse_date("2027-11-21T12:00:00Z", NOW, "jsonld") is None


def test_date_before_1995_rejected():
    assert parse_date("1994-12-31", NOW, "api_field") is None


def test_pick_best_narrowest_at_highest_confidence():
    exact = parse_date("2026-11-21T12:00:00Z", NOW, "api_field")
    broad = parse_date("2026-11-21", NOW, "api_field")
    assert pick_best([broad, exact]) == exact


def test_pick_best_conflict_returns_earlier(monkeypatch):
    earlier = parse_date("2026-11-18T12:00:00Z", NOW, "api_field")
    later = parse_date("2026-11-21T12:00:00Z", NOW, "api_field")
    import src.core.dates as dates
    conflict_log = []
    monkeypatch.setattr(dates.log, "warning", lambda event, **kwargs: conflict_log.append(event))
    assert pick_best([earlier, later]) == earlier
    assert conflict_log == ["date.conflict"]


def test_page_date_candidates_from_jsonld_meta_time_and_url():
    html = """
    <script type="application/ld+json">
    {"@graph":[{"datePublished":"2026-11-21T11:30:00Z"}]}
    </script>
    <meta property="article:published_time" content="2026-11-21T11:45:00Z">
    <time datetime="2026-11-21T11:00:00Z"></time>
    """
    candidates = page_date_candidates(
        html,
        {"Last-Modified": "Sat, 21 Nov 2026 10:00:00 GMT"},
        "https://example.com/2026/11/21/story",
        NOW,
    )
    sources = {candidate.source for candidate in candidates}
    assert {"jsonld", "meta_og", "time_tag", "url_pattern", "http_last_modified"} <= sources