import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.fetcher import TieredFetcher, is_blocked


async def main():
    fetcher = TieredFetcher(enable_browser=True)
    rows = []

    try:
        for label, url in (
            ("httpbin", "https://httpbin.org/headers"),
            ("js", "https://quotes.toscrape.com/js/"),
            ("cloudflare", "https://www.cloudflare.com/"),
        ):
            if label == "js" and sys.platform == "win32":
                policy = asyncio.get_event_loop_policy()
                if policy.__class__.__name__ == "WindowsSelectorEventLoopPolicy":
                    rows.append((url, "-", "-", "requires Proactor", 0, 0))
                    continue
            if label == "js":
                fetcher.domain_min_tier["quotes.toscrape.com"] = 3
            try:
                page = await fetcher.fetch(url)
                blocked = is_blocked(page.status, page.html, page.headers)
                rows.append((
                    url,
                    page.tier,
                    page.status,
                    blocked or "-",
                    len(page.html),
                    len(page.extra_json),
                ))
            except Exception as error:
                rows.append((url, "-", "-", type(error).__name__, 0, 0))
    finally:
        await fetcher.close()

    print("| url | final_tier | status | blocked_kind | html_len | json_responses_captured |")
    print("|---|---:|---:|---|---:|---:|")
    for row in rows:
        print("| {} | {} | {} | {} | {} | {} |".format(*row))


if __name__ == "__main__":
    asyncio.run(main())
