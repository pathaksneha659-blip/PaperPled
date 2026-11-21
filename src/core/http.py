import asyncio
import random
from collections import defaultdict
from urllib.parse import urlsplit
import aiohttp
from src.logging_setup import log

class RetryableHTTPError(Exception):
    def __init__(self, status: int, retry_after: float | None = None):
        self.status = status
        self.retry_after = retry_after

def full_jitter_backoff(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    return random.uniform(0, min(cap, base * (2 ** attempt)))

class HttpClient:
    def __init__(self, total_concurrency: int = 100, per_host_concurrency: int = 4, timeout: int = 30):
        self.session: aiohttp.ClientSession | None = None
        self.total_sem = asyncio.Semaphore(total_concurrency)
        self.host_sems = defaultdict(lambda: asyncio.Semaphore(per_host_concurrency))
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={"User-Agent": "FrontierAtlas-IngestBot/1.0 (+https://frontieratlas.ai)"}
            )
        return self.session

    async def request(self, method: str, url: str, as_json: bool = False, retries: int = 5, **kwargs):
        host = urlsplit(url).netloc
        session = await self.get_session()

        for attempt in range(retries):
            try:
                async with self.total_sem, self.host_sems[host]:
                    async with session.request(method, url, **kwargs) as resp:
                        if resp.status == 429 or resp.status >= 500:
                            ra = resp.headers.get("Retry-After")
                            retry_after = float(ra) if ra and ra.isdigit() else None
                            raise RetryableHTTPError(resp.status, retry_after)
                        resp.raise_for_status()
                        return await (resp.json(content_type=None) if as_json else resp.text())
            except (RetryableHTTPError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                delay = getattr(e, "retry_after", None) or full_jitter_backoff(attempt)
                log.warning("http.request_retry", url=url, attempt=attempt, err=str(e), sleep=round(delay, 2))
                await asyncio.sleep(delay)

        raise RuntimeError(f"HTTP retries exhausted for {url}")

    async def get(self, url: str, as_json: bool = False, **kwargs):
        return await self.request("GET", url, as_json=as_json, **kwargs)

    async def post(self, url: str, as_json: bool = False, **kwargs):
        return await self.request("POST", url, as_json=as_json, **kwargs)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()