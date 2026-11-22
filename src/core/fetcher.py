import asyncio
import sys
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from src.logging_setup import log


UTC = timezone.utc


@dataclass
class Page:
    url: str
    status: int
    html: str
    headers: dict
    tier: int
    fetched_at: datetime
    extra_json: list[dict] = field(default_factory=list)


class BlockedError(RuntimeError):
    """A managed unblocker could plug in at tier 4, but this implementation stops."""


def is_blocked(status: int, body: str, headers: dict) -> str | None:
    text = (body or "").lower()
    normalized_headers = {
        str(key).lower(): str(value).lower()
        for key, value in (headers or {}).items()
    }
    if normalized_headers.get("cf-mitigated") == "challenge":
        return "cloudflare"
    if any(marker in text for marker in ("just a moment...", "challenge-platform")):
        return "cloudflare"
    if "x-datadome" in normalized_headers or "captcha-delivery.com" in text:
        return "datadome"
    if "_px" in text or "px-captcha" in text:
        return "perimeterx"
    if "akamai" in text:
        return "akamai"
    if status in (403, 429, 503) and ("access denied" in text or status == 403):
        return "generic_403"
    return None


class _DomainLimiter:
    def __init__(self):
        self.rate = 1.0
        self.next_request = 0.0
        self.last_update = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self):
        async with self.lock:
            now = time.monotonic()
            elapsed_minutes = (now - self.last_update) / 60
            if elapsed_minutes > 0:
                self.rate = min(1.0, self.rate * (1.1 ** elapsed_minutes))
                self.last_update = now
            scheduled = max(now, self.next_request)
            wait = scheduled - now
            self.next_request = scheduled + 1 / max(self.rate, 0.05)
        if wait > 0:
            await asyncio.sleep(wait)

    async def throttle(self):
        async with self.lock:
            self.rate = max(0.05, self.rate / 2)
            self.last_update = time.monotonic()


TierCallable = Callable[[str], Awaitable[Page]]


class TieredFetcher:
    def __init__(
        self,
        enable_browser: bool = False,
        timeout: float = 30,
        tier_callables: list[TierCallable] | None = None,
    ):
        self.enable_browser = enable_browser
        self.timeout = timeout
        self.tier_callables = tier_callables
        self.domain_min_tier: dict[str, int] = {}
        self.limiters: dict[str, _DomainLimiter] = {}
        self.http_client: httpx.AsyncClient | None = None
        self.curl_session = None
        self.playwright = None
        self.browser_context = None
        self.context_page_count = 0
        self.browser_profile_dir: str | None = None

    @staticmethod
    def _domain(url: str) -> str:
        return (urlsplit(url).hostname or "").lower()

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "sec-ch-ua": '"Chromium";v="131", "Google Chrome";v="131", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }

    async def _ensure_http(self) -> httpx.AsyncClient:
        if self.http_client is None:
            self.http_client = httpx.AsyncClient(
                http2=True,
                timeout=self.timeout,
                follow_redirects=True,
                headers=self._headers(),
            )
        return self.http_client

    async def _fetch_httpx(self, url: str) -> Page:
        client = await self._ensure_http()
        response = await client.get(url)
        return Page(
            url=str(response.url),
            status=response.status_code,
            html=response.text,
            headers=dict(response.headers),
            tier=1,
            fetched_at=datetime.now(UTC),
        )

    async def _fetch_curl_cffi(self, url: str) -> Page:
        from curl_cffi.requests import AsyncSession

        if self.curl_session is None:
            self.curl_session = AsyncSession(
                impersonate="chrome",
                timeout=self.timeout,
                headers=self._headers(),
            )
        response = await self.curl_session.get(url, allow_redirects=True)
        return Page(
            url=str(response.url),
            status=response.status_code,
            html=response.text,
            headers=dict(response.headers),
            tier=2,
            fetched_at=datetime.now(UTC),
        )

    async def _ensure_browser_context(self):
        if not self.enable_browser:
            raise RuntimeError("Playwright tier is opt-in; pass enable_browser=True")

        if sys.platform == "win32":
            policy = asyncio.get_event_loop_policy()
            if policy.__class__.__name__ == "WindowsSelectorEventLoopPolicy":
                raise RuntimeError(
                    "Playwright Tier 3 requires a Windows Proactor event loop. "
                    "Use asyncio.ProactorEventLoopPolicy."
                )

        if self.browser_context is not None and self.context_page_count < 20:
            return self.browser_context

        if self.browser_context is not None:
            await self.browser_context.close()
            self.browser_context = None

        from playwright.async_api import async_playwright

        if self.playwright is None:
            self.playwright = await async_playwright().start()
        if self.browser_profile_dir is None:
            self.browser_profile_dir = tempfile.mkdtemp(prefix="frontieratlas-browser-")
        self.browser_context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=self.browser_profile_dir,
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.context_page_count = 0
        return self.browser_context

    async def _fetch_playwright(self, url: str) -> Page:
        context = await self._ensure_browser_context()
        page = await context.new_page()
        self.context_page_count += 1
        extra_json: list[dict] = []

        async def capture(response):
            request = response.request
            if request.resource_type not in ("xhr", "fetch"):
                return
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return
            try:
                payload = await response.json()
                if isinstance(payload, dict):
                    extra_json.append(payload)
                elif isinstance(payload, list):
                    extra_json.append({"items": payload})
            except Exception:
                pass

        page.on("response", capture)
        try:
            response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=int(self.timeout * 1000),
            )
            status = response.status if response else 0
            html = await page.content()
            headers = await response.all_headers() if response else {}
        finally:
            await page.close()

        return Page(
            url=page.url,
            status=status,
            html=html,
            headers=headers,
            tier=3,
            fetched_at=datetime.now(UTC),
            extra_json=extra_json,
        )

    @staticmethod
    def _retry_seconds(headers: dict) -> float | None:
        value = headers.get("retry-after") or headers.get("Retry-After")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                when = parsedate_to_datetime(str(value))
                if when.tzinfo is None:
                    return 0.0
                return max(0.0, (when - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    async def fetch(self, url: str) -> Page:
        domain = self._domain(url)
        limiter = self.limiters.setdefault(domain, _DomainLimiter())
        await limiter.acquire()

        current = self.domain_min_tier.get(domain, 1)
        tier_callables = self.tier_callables or [
            self._fetch_httpx,
            self._fetch_curl_cffi,
            self._fetch_playwright,
        ]

        while current <= 3:
            try:
                page = await tier_callables[current - 1](url)
            except (RuntimeError, httpx.TransportError) as error:
                log.warning("fetch.tier_error", domain=domain, tier=current, error=str(error))
                if current >= 3:
                    raise
                current += 1
                continue

            kind = is_blocked(page.status, page.html, page.headers)
            if kind is None:
                self.domain_min_tier.setdefault(domain, current)
                log.info("fetch.ok", domain=domain, tier=current, status=page.status)
                return page

            if page.status == 429:
                await limiter.throttle()
                delay = self._retry_seconds(page.headers)
                if delay:
                    await asyncio.sleep(delay)

            log.warning("fetch.blocked", domain=domain, tier=current, kind=kind)
            if current >= 3:
                break
            log.info("fetch.escalate", domain=domain, from_tier=current, to_tier=current + 1)
            current += 1

        log.warning("fetch.dead_letter", domain=domain, tier=4, kind=kind)
        raise BlockedError(f"Blocked on {domain}; tier 4 managed unblocker is required")

    async def close(self):
        if self.http_client is not None:
            await self.http_client.aclose()
            self.http_client = None
        if self.curl_session is not None:
            await self.curl_session.close()
            self.curl_session = None
        if self.browser_context is not None:
            await self.browser_context.close()
            self.browser_context = None
        if self.playwright is not None:
            await self.playwright.stop()
            self.playwright = None
        if self.browser_profile_dir is not None:
            shutil.rmtree(self.browser_profile_dir, ignore_errors=True)
            self.browser_profile_dir = None