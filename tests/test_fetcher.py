import pytest

from src.core.fetcher import BlockedError, Page, TieredFetcher, is_blocked


def test_cloudflare_challenge():
    body = '<html><title>Just a moment...</title><script src="/cdn-cgi/challenge-platform"></script></html>'
    assert is_blocked(403, body, {}) == "cloudflare"


def test_datadome_header():
    assert is_blocked(403, "<html>forbidden</html>", {"x-datadome": "captcha"}) == "datadome"


def test_normal_200():
    assert is_blocked(200, "<html><body>Hello</body></html>", {}) is None


@pytest.mark.asyncio
async def test_escalation_stops_at_tier_4():
    calls = []
    from datetime import datetime, timezone

    async def blocked(url):
        tier = len(calls) + 1
        calls.append(tier)
        return Page(
            url,
            403,
            "Access Denied",
            {},
            tier,
            datetime.now(timezone.utc),
        )

    fetcher = TieredFetcher(tier_callables=[blocked, blocked, blocked])
    with pytest.raises(BlockedError):
        await fetcher.fetch("https://example.com/page")

    assert calls == [1, 2, 3]
    assert fetcher.domain_min_tier == {}
