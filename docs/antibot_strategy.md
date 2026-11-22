# FrontierAtlas Antibot Strategy

| Tier | Cost | Typical latency | Use |
|---|---|---:|---|
| 1 | Lowest | Fast | httpx HTTP/2 with realistic, consistent headers |
| 2 | Low | Fast-moderate | curl_cffi Chrome TLS/JA3 impersonation |
| 3 | Moderate | Seconds | Opt-in Playwright Chromium; rendered HTML and XHR JSON |
| 4 | High/managed | Variable | Dead-letter only; a managed unblocker vendor could integrate here |

Use consistent browser/session identity and retain browser cookies across the
same persistent context. Apply per-domain adaptive rate limits, honor
`Retry-After`, and increase retrieval tier only after a challenge is classified.

Prefer first-party and embedded data: `__NEXT_DATA__`, JSON-LD, sitemaps,
open feeds, and API responses. Intercept JSON XHR/fetch responses when browser
rendering is permitted.

High-value policy: use official APIs or licensed access for Crunchbase and
Wellfound. Their Terms of Service prohibit automated scraping, so this project
does not scrape or bypass protections for them.

Blocked pages are dead-lettered after one escalation per tier and stop at tier
4. Record the domain, tier, challenge class, source URL, and response metadata
for review without attempting captcha solving or unauthorized bypass.
