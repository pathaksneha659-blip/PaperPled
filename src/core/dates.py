import re
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Iterable

import dateparser

from src.logging_setup import log


UTC = timezone.utc
MIN_DATE = datetime(1995, 1, 1, tzinfo=UTC)


class _DateHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.jsonld = []
        self.meta = []
        self.times = []
        self._jsonld_depth = 0
        self._jsonld_parts = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag.lower() == "script" and attributes.get("type", "").lower() == "application/ld+json":
            self._jsonld_depth += 1
            self._jsonld_parts = []
        elif tag.lower() == "meta":
            key = attributes.get("property") or attributes.get("name")
            if key in (
                "article:published_time",
                "og:updated_time",
            ):
                self.meta.append((key, attributes.get("content")))
        elif tag.lower() == "time" and attributes.get("datetime"):
            self.times.append(attributes["datetime"])

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._jsonld_depth:
            self._jsonld_depth -= 1
            self.jsonld.append("".join(self._jsonld_parts).strip())
            self._jsonld_parts = []

    def handle_data(self, data):
        if self._jsonld_depth:
            self._jsonld_parts.append(data)


@dataclass(frozen=True)
class ParsedDate:
    earliest: datetime
    latest: datetime
    source: str
    raw: str

    @property
    def point(self) -> datetime:
        if self.latest - self.earliest < timedelta(hours=1):
            return self.latest
        return self.earliest


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _valid_interval(
    earliest: datetime,
    latest: datetime,
    fetched_at: datetime,
    source: str,
    raw: str,
) -> ParsedDate | None:
    earliest = _utc(earliest)
    latest = _utc(latest)
    fetched_at = _utc(fetched_at)

    if earliest < MIN_DATE:
        return None
    future_limit = fetched_at + timedelta(minutes=10)
    if earliest > future_limit:
        return None
    if latest > future_limit:
        latest = fetched_at
    if latest < earliest:
        latest = earliest
    return ParsedDate(earliest, latest, source, raw)


def parse_date(raw: str | int | None, fetched_at: datetime, source: str) -> ParsedDate | None:
    if raw is None:
        return None
    if isinstance(raw, float):
        return None
    text = str(raw).strip()
    if not text:
        return None
    fetched_at = _utc(fetched_at)

    if isinstance(raw, bool):
        return None

    if isinstance(raw, int) or (isinstance(raw, str) and text.isdigit()):
        try:
            epoch = int(text)
        except (TypeError, ValueError, OverflowError):
            return None
        seconds = epoch / 1000 if epoch > 100_000_000_000 else epoch
        try:
            exact = datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
        return _valid_interval(exact, exact, fetched_at, source, text)

    lower = text.lower()
    relative = re.fullmatch(r"(just now|now)", lower)
    if relative:
        return _valid_interval(fetched_at, fetched_at, fetched_at, source, text)

    match = re.fullmatch(r"(\d+)\s*(min(?:ute)?s?|h(?:our)?s?|d(?:ay)?s?)\s*(?:ago)?", lower)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if unit.startswith("min"):
            earliest = fetched_at - timedelta(minutes=amount + 1)
            latest = fetched_at - timedelta(minutes=amount)
        elif unit.startswith("h"):
            earliest = fetched_at - timedelta(hours=amount + 1)
            latest = fetched_at - timedelta(hours=amount)
        else:
            earliest = fetched_at - timedelta(days=amount + 1)
            latest = fetched_at - timedelta(days=amount)
        return _valid_interval(earliest, latest, fetched_at, source, text)

    if lower == "yesterday":
        yesterday = (fetched_at - timedelta(days=1)).date()
        earliest = datetime(yesterday.year, yesterday.month, yesterday.day, tzinfo=UTC)
        latest = earliest + timedelta(days=1)
        return _valid_interval(earliest, latest, fetched_at, source, text)

    if re.fullmatch(r"(\d+)\s*hours?\s+ago", lower):
        amount = int(lower.split()[0])
        return _valid_interval(
            fetched_at - timedelta(hours=amount + 1),
            fetched_at - timedelta(hours=amount),
            fetched_at,
            source,
            text,
        )

    if re.fullmatch(r"\d+\s*(?:minutes?|mins?)\s+ago", lower):
        amount = int(lower.split()[0])
        return _valid_interval(
            fetched_at - timedelta(minutes=amount + 1),
            fetched_at - timedelta(minutes=amount),
            fetched_at,
            source,
            text,
        )

    if re.fullmatch(r"\d+\s*days?\s+ago", lower):
        amount = int(lower.split()[0])
        return _valid_interval(
            fetched_at - timedelta(days=amount + 1),
            fetched_at - timedelta(days=amount),
            fetched_at,
            source,
            text,
        )

    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            day = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
            return _valid_interval(
                day - timedelta(hours=14),
                day + timedelta(days=1, hours=12),
                fetched_at,
                source,
                text,
            )

        iso_text = text[:-1] + "+00:00" if text.upper().endswith("Z") else text
        iso = datetime.fromisoformat(iso_text)
        if iso.tzinfo is None:
            iso = iso.replace(tzinfo=UTC)
            return _valid_interval(
                iso - timedelta(hours=14),
                iso + timedelta(hours=12),
                fetched_at,
                source,
                text,
            )
        exact = _utc(iso)
        return _valid_interval(exact, exact, fetched_at, source, text)
    except (TypeError, ValueError, OverflowError):
        pass

    try:
        rfc = parsedate_to_datetime(text)
        if rfc is not None:
            if rfc.tzinfo is None:
                rfc = rfc.replace(tzinfo=UTC)
            exact = _utc(rfc)
            return _valid_interval(exact, exact, fetched_at, source, text)
    except (TypeError, ValueError, OverflowError):
        pass

    parsed = dateparser.parse(
        text,
        settings={
            "RELATIVE_BASE": fetched_at,
            "PREFER_DATES_FROM": "past",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TO_TIMEZONE": "UTC",
        },
    )
    if parsed is None:
        return None
    exact = _utc(parsed)
    return _valid_interval(exact, exact, fetched_at, source, text)


def is_fresh(
    pd: ParsedDate,
    now: datetime,
    window: timedelta = timedelta(hours=24),
    strict: bool = True,
) -> bool:
    now = _utc(now)
    boundary = now - window
    return pd.earliest >= boundary if strict else pd.latest >= boundary


CONFIDENCE = {
    "api_field": 8,
    "jsonld": 7,
    "meta_og": 6,
    "time_tag": 5,
    "rss": 4,
    "url_pattern": 3,
    "visible_text": 2,
    "http_last_modified": 1,
}


def pick_best(candidates: list[ParsedDate]) -> ParsedDate | None:
    if not candidates:
        return None

    highest = max(CONFIDENCE.get(candidate.source, 0) for candidate in candidates)
    finalists = [
        candidate for candidate in candidates
        if CONFIDENCE.get(candidate.source, 0) == highest
    ]

    best = min(
        finalists,
        key=lambda candidate: (
            candidate.latest - candidate.earliest,
            abs((candidate.earliest - candidate.latest).total_seconds()),
        ),
    )

    if len(finalists) > 1:
        sorted_candidates = sorted(finalists, key=lambda candidate: candidate.earliest)
        conflict = True
        for left in range(len(sorted_candidates)):
            for right in range(left + 1, len(sorted_candidates)):
                first = sorted_candidates[left]
                second = sorted_candidates[right]
                if first.latest >= second.earliest:
                    conflict = False
                    break
            if not conflict:
                break
        if conflict:
            separated = max(
                (sorted_candidates[right].earliest - sorted_candidates[left].earliest
                 for left in range(len(sorted_candidates))
                 for right in range(left + 1, len(sorted_candidates))),
                default=timedelta(),
            )
            if separated > timedelta(hours=24):
                log.warning("date.conflict", candidates=[candidate.raw for candidate in finalists])
                best = min(finalists, key=lambda candidate: candidate.point)

    return best


def _jsonld_values(value: object) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("datePublished", "dateModified") and isinstance(item, (str, int)):
                yield str(item)
            elif isinstance(item, (dict, list)):
                yield from _jsonld_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _jsonld_values(item)


def page_date_candidates(
    html: str,
    headers: dict[str, str],
    url: str,
    fetched_at: datetime,
) -> list[ParsedDate]:
    candidates: list[ParsedDate] = []
    if not html:
        html = ""
    parser = _DateHTMLParser()
    parser.feed(html)

    for raw_jsonld in parser.jsonld:
        try:
            payload = json.loads(raw_jsonld)
        except (json.JSONDecodeError, TypeError):
            continue
        for raw in _jsonld_values(payload):
            parsed = parse_date(raw, fetched_at, "jsonld")
            if parsed:
                candidates.append(parsed)

    for _, raw in parser.meta:
        parsed = parse_date(raw, fetched_at, "meta_og")
        if parsed:
            candidates.append(parsed)

    for raw in parser.times:
        parsed = parse_date(raw, fetched_at, "time_tag")
        if parsed:
            candidates.append(parsed)

    match = re.search(r"/(\d{4})/(\d{2})/(\d{2})(?:/|$)", url)
    if match:
        parsed = parse_date("-".join(match.groups()), fetched_at, "url_pattern")
        if parsed:
            candidates.append(parsed)

    normalized_headers = {str(key).lower(): str(value) for key, value in (headers or {}).items()}
    last_modified = normalized_headers.get("last-modified")
    parsed = parse_date(last_modified, fetched_at, "http_last_modified")
    if parsed:
        candidates.append(parsed)

    return candidates