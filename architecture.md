# FrontierAtlas Architecture Specification

## 1. Scale Strategy for 500,000+ Records

To ingest and process 500,000+ AI market records sustainably, the pipeline avoids generic unconstrained crawling in favor of structured, deterministic discovery partitions:

* **Enumerable Discovery Sources**:
  * **Hugging Face Daily Papers**: Query daily archives deterministically (`/api/daily_papers?date=YYYY-MM-DD`) over historical date ranges to gather canonical arXiv IDs and linked GitHub repositories.
  * **arXiv API Batching**: Authoritative paper metadata and author rosters are enriched via batched `id_list` queries (100 IDs per request), strictly adhering to arXiv's 3-second query intervals.
  * **GitHub GraphQL API**: Star counts and repository metadata are queried in batches of 50 repositories per GraphQL payload with bearer token rotation across `GITHUB_TOKENS`.
  * **YC Directory**: Pulls full company indices from the static catalog endpoint (`yc-oss.github.io/api/companies/all.json`) and extracts AI-tagged companies without HTML rendering.
  * **ATS Job Boards**: High-signal hiring velocity is monitored by polling structured Ashby API endpoints (`api.ashbyhq.com/posting-api/job-board/{company}`) across tracked frontier AI labs.
* **URL Frontier with SKIP LOCKED Leasing (*Designed, not implemented locally*)**:
  * Distributed workers lease batches of discovery tasks from a Postgres-backed queue using `SELECT id FROM frontier_queue WHERE status = 'pending' ORDER BY priority DESC LIMIT 100 FOR UPDATE SKIP LOCKED`.
  * This guarantees single-worker leasing without lock contention or distributed transaction deadlocks.
* **Stateless Workers**:
  * Extraction nodes operate statelessly without local disk dependencies. Output items are emitted directly into stream queues or idempotent staging tables.
* **Concurrency and Throttling Control**:
  * Local HTTP client enforces a global limit of 100 concurrent connections via `asyncio.Semaphore(100)` and per-host limits via `defaultdict(lambda: asyncio.Semaphore(5))` with full-jitter exponential backoff.
* **Raw-Payload Archive (*Designed, not implemented locally; local filesystem today*)**:
  * Payloads are stored locally in `output/` as raw newline-delimited JSON (`.jsonl`). In the distributed design, raw HTTP responses and upstream snapshots are written immutable to object storage (e.g., S3/GCS) with content hashes for replayability.
* **Deterministic-First Extraction**:
  * Selectolax (Lexbor) and structured JSON/RSS parsing extract schema fields deterministically. LLMs are reserved strictly for schema normalization and grounded pricing classification where heuristic parsing is ambiguous.

---

## 2. LLM Resilience

LLM inference is isolated to unstructured extraction tasks (such as pricing tier extraction from markdown or raw text). Because upstream providers have varying context limits and strict tier rate limits, resilience is enforced by a multi-provider fallback orchestrator.

```
                  +---------------------------+
                  |  Extract Request (Text)   |
                  +-------------+-------------+
                                |
                                v
                   [ Provider 1: Gemini ] <-------------------+
                                |                             |
                 +--------------+--------------+              |
                 |                             |              |
          (Token / TPM Limit)            (HTTP 429)           |
                 |                             |              |
                 v                             v              |
       Shrink Payload (60%)          Read Retry-After Header  |
                 |                   + Exponential Jitter     |
                 v                             |              |
           Retry Provider                      v              |
                                         Provider Pause       |
                                               |              |
                                        (Quota Limit / 5xx)   |
                                               |              |
                                               v              |
                                  Fallback to Provider 2:     |
                                  [ Groq ] -------------------+
                                               |
                                        (Quota Limit / 5xx)
                                               |
                                               v
                                  Fallback to Provider 3:
                                  [ DeepSeek ]
                                               |
                                               v
                                  +------------+------------+
                                  | Structural Output Match |
                                  +------------+------------+
                                               |
                                               v
                                  +------------+------------+
                                  | Regex Grounding Verif.  |
                                  +------------+------------+
                                       /              \
                              (Pass)  /                \ (Fail / Ungrounded)
                                     v                  v
                          Verified (grounded)    Unverified default
```

* **Fallback Chain**:
  * Configured sequentially as `Gemini -> Groq -> DeepSeek` through `LLMGateway`. If an upstream provider fails due to unrecoverable errors, quota exhaustion, or parsing failures, execution rolls over to the next configured provider.
* **Shared RPM/TPM Windows**:
  * Each provider is guarded by a `ProviderGate` tracking a 60-second rolling window deque of timestamps and token volumes. Requests exceeding configured `rpm` or `tpm` await available window capacity.
* **413 Handling via Density-Ranked Truncation & Shrink-on-Overflow**:
  * Input documents exceeding token limits are filtered using keyword signal regex (`[$€£]`, `pricing`, `free`, `tier`, `enterprise`).
  * If an upstream provider returns HTTP 413 or a context length error (`context_length_exceeded`), the budget module contracts the excerpt window to 60% of the sent character count (`cap = len(excerpt) * 3 // 5`) and retries up to 4 attempts.
* **429 Handling via Retry-After and Full-Jitter Backoff**:
  * Rate-limited requests evaluate the `Retry-After` HTTP header (supporting both raw integer seconds and RFC 2822 datetime formats).
  * If absent, backoff is randomized with full jitter: $t_{\text{wait}} = \text{Uniform}(0, \min(16, 2^{\text{attempt}}))$.
* **Quota Cooldown**:
  * When a 429 error indicates persistent quota exhaustion (`insufficient_quota`, `daily quota`, `per day`), the `ProviderGate` engages a 300-second pause and trips immediate fallback to the next provider.
* **Grounding of Accepted Output**:
  * Strict schema adherence via Pydantic model validation (`PricingProposal`).
  * The `ground()` function validates that every quote in `pricingEvidence` exists verbatim in both the original input text and the excerpt sent to the LLM.
  * Output that passes quote verification is classified as **Verified (grounded quote)**; ungrounded or unmatched outputs fall back to **Unverified default** (`pricingModel: null, pricingEvidence: []`).

---

## 3. Freshness and Deduplication

* **Interval-Based Date Parsing with Strict 24-Hour Inclusion**:
  * The date parsing module (`src.core.dates`) resolves timestamps, ISO 8601 strings, relative offsets (`X hours ago`, `yesterday`), and RFC dates into closed UTC interval objects `ParsedDate(earliest, latest, source, raw)`.
  * Candidate dates are collected from JSON-LD (`datePublished`), OpenGraph meta tags, `<time>` tags, and URL patterns, prioritized by a confidence hierarchy (`api_field` > `jsonld` > `meta_og` > `time_tag` > `rss`).
  * Strict freshness filtering (`is_fresh()`) enforces `earliest >= (now - 24 hours)` to guarantee that items crossing date boundaries without exact hour precision are not falsely included.
* **Canonical URL Normalization & Content Fingerprinting**:
  * `canonical_url()` strips tracking noise (`utm_*`, `ref`, `fbclid`, `gclid`), lowers hostnames, strips `www.`, and normalizes trailing slashes.
  * `fingerprint()` computes a SHA-256 digest of whitespace-collapsed lowercase text (up to 5,000 characters) to detect republished and syndicated content.
* **SeenStore Claims**:
  * **Locally Implemented**: SQLite database (`data/seen.db`) using `INSERT OR IGNORE` on `(key, source, first_seen_at, run_id)` with transactional atomic claims (`claim()`).
  * **Designed for Distributed Nodes (*Designed, not implemented locally*)**: Redis `SET key run_id NX EX 604800` (7-day lease) providing sub-millisecond atomic lease acquisition across workers.
* **Postgres UNIQUE Constraint (*Designed, not implemented locally*)**:
  * The persistent relational store enforces canonical uniqueness via `UNIQUE (canonical_url)` and `UNIQUE (content_fingerprint)` with `ON CONFLICT DO NOTHING`.

---

## 4. Storage Architecture

* **PostgreSQL as Primary Database (*Design Rationale*)**:
  * Strong ACID guarantees, native JSONB support for semi-structured provenance metadata, and transactional row locking (`FOR UPDATE SKIP LOCKED`) make Postgres the ideal source of truth for pipeline coordination and canonical catalog storage.
* **pgvector for Entity Embeddings (*Designed, not implemented locally*)**:
  * Vector similarity search (`vector(1536)`) using HNSW indexing inside Postgres to link ambiguous product names, company descriptions, and research abstracts without introducing an auxiliary vector database.
* **Neo4j Graph Projection (*Designed, not implemented locally*)**:
  * Projected asynchronously from Postgres via CDC (Change Data Capture) or batch pipelines.
  * **Migration Trigger**: Activated when multi-hop relational traversal queries (e.g., *Startup -> Founded By -> Authored Paper -> Funded By -> Integrated Product*) exceed 3 joins and latency SLAs on Postgres exceed 250ms at 500,000+ nodes.
* **Raw Payload Storage**:
  * **Locally Implemented**: Plaintext newline-delimited JSON (`.jsonl`) files in the local filesystem under `output/`.
  * **Production Target (*Designed, not implemented locally*)**: Immutable raw payloads stored in S3/GCS bucket paths keyed by `s3://frontieratlas-raw/{vertical}/{date}/{content_hash}.json.gz`.

---

## 5. Anti-Bot and High-Value Sources

* **Tiered Fetcher Escalation**:
  * **Tier 1 (httpx)**: Lightweight HTTP/2 client for static APIs and standard endpoints.
  * **Tier 2 (curl_cffi)**: Browser TLS fingerprint impersonation (`impersonate="chrome"`).
  * **Tier 3 (Playwright Persistent Context)**: Headless Chromium with persistent contexts and `--disable-blink-features=AutomationControlled` to execute client-rendered JavaScript and intercept dynamic background XHR/JSON responses.
  * **Tier 4 (Licensed Unblocker / Commercial Proxies - *Designed, not implemented locally*)**: The pipeline detects hard bot walls and raises `BlockedError` to halt rather than attempting illegal circumvention.
* **Challenge Detection Signals**:
  * Inspected via `is_blocked()`:
    * Cloudflare: Header `cf-mitigated: challenge` or body tokens (`just a moment...`, `challenge-platform`).
    * DataDome: Header `x-datadome` or body token `captcha-delivery.com`.
    * PerimeterX / Akamai: `_px`, `px-captcha`, or `akamai` access denial signatures.
* **Per-Domain Adaptive Throttling**:
  * Each domain is governed by a thread-safe `_DomainLimiter` adjusting request interval schedules ($1 / \text{rate}$).
  * On receipt of HTTP 429, the limiter halves its throughput schedule (`rate = max(0.05, rate / 2)`) and respects the `Retry-After` header. After sustained successful responses, the rate recovers multiplicatively ($1.1^{\Delta t}$).
* **Official APIs and Embedded JSON**:
  * The pipeline explicitly prefers structured endpoints (Ashby posting APIs, arXiv XML export, Hugging Face JSON APIs) over scraping HTML presentation markup.
* **Compliance-First Policy**:
  * The pipeline does **not** bypass Cloudflare or DataDome.
  * Captchas are **never** solved or bypassed.
  * Sources prohibiting automated indexing in their Terms of Service are excluded, deferred to licensed data partnerships, or accessed via sanctioned developer APIs.

---

## 6. Measured Results

All metrics below are drawn strictly from pipeline execution records in `output/run_stats.json` and generated output datasets:

| Metric | Measured Value |
| :--- | :--- |
| **Pipeline Target Execution Limit** | 1000 records / vertical |
| **Startups Ingested (`startups.jsonl`)** | 1000 |
| **Products Ingested (`products.jsonl`)** | 1000 |
| **Research Papers Ingested (`research_papers.jsonl`)** | 1000 |
| **Fresh Jobs Ingested (`jobs.jsonl`, < 24h)** | 24 |
| **Fresh News Ingested (`news.jsonl`, < 24h)** | 71 |
| **Entity Resolution Total Records Processed** | 2000 |
| ↳ `new_entity` (Dynamic Entity Creation) | 1894 |
| ↳ `ambiguous_review` (Manual Review Flag) | 59 |
| ↳ `exact_compact` (Exact Match on Compact String) | 30 |
| ↳ `fuzzy` (High-confidence WRatio Match) | 13 |
| ↳ `registrable_domain` (Domain Hash Match) | 4 |
| **LLM Inference Stats** | |
| ↳ `primary.requests` | 1 |
| ↳ `primary.ok` | 1 |
| ↳ `primary.overflow` | 1 |
| ↳ `primary.429` | 1 |
| ↳ `primary.401` | 1 |
| ↳ `fallback` (Cascade Triggered) | 1 |
| ↳ `backup.requests` | 1 |
| ↳ `backup.ok` | 1 |
| ↳ `gemini.requests` | 1 |
| ↳ `gemini.403` | 1 |
| ↳ `groq.requests` | 1 |
| ↳ `groq.ok` | 1 |
| ↳ `groq.404` | 1 |
| ↳ `groq.ungrounded` | 1 |
| **Product Pricing Status** | |
| ↳ Verified (grounded quote) | 0 |
| ↳ Unverified default (`null`) | 1000 |
| **Overall Pipeline Execution Duration** | TBD |
