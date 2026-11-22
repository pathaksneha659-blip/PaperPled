# FrontierAtlas Architecture

## Page 1 — Scale strategy for 500k+

**Current implementation:** startup, product, research-paper, job, and news extractors fetch public APIs/feeds through `src/core/http.py`, then write JSONL files. Jobs and news retain only items published in the prior 24 hours. `src/core/fetcher.py` provides an opt-in tiered fetcher but is not wired into those source modules. There is no persistent frontier, worker pool, or content-addressed raw archive in the demo.

**Production design (not in demo):**

```text
Enumerable source discovery
       |
       v
Postgres frontier (SKIP LOCKED + expiring leases)
       |
       +--> stateless fetch/parse workers (scale by replicas)
       |         |
       |         +--> per-host limiter + compliance/challenge policy
       |         +--> content-addressed raw response archive (S3)
       v
normalized records + append-only observations --> exports / consumers
```

- Enumerate source APIs, feeds, sitemaps, and bounded archives before crawling broad page spaces. Persist cursors so discovery is resumable.
- Claim work with short leases using `SELECT ... FOR UPDATE SKIP LOCKED`; workers should be stateless so replicas can be added without shared in-memory queues.
- Decouple discovery, fetching, extraction, and export. Store raw responses by content hash in S3 and keep metadata/status in Postgres.
- Apply per-host concurrency and adaptive rates. Respect provider/source rules; classify a block, escalate only where permitted, then dead-letter.
- Keep deterministic parsing ahead of expensive LLM calls; cache fetches and model results by normalized content hash.

**Theoretical throughput only (not a measured benchmark):**

`daily items = workers × sustained requests/second/worker × 86,400 × utilization`

For illustration, 10 workers × 1 request/second × 86,400 seconds × 1.0 utilization = 864,000 requests/day before retries, source quotas, parse failures, or non-1:1 item/page ratios. This is arithmetic, not a capacity claim; real sustainable throughput is source-policy-limited.

Cost levers: deterministic-first extraction, batch-friendly APIs (the current arXiv fetcher batches up to 100 IDs and GitHub GraphQL batches 50 repositories), response/result caching, and LLM only for residual ambiguous fields.

<div class="page-break"></div>

## Page 2 — LLM payload and rate-limit handling

**Implemented in `src/llm/budget.py`, `src/llm/providers.py`, and `src/llm/orchestrator.py`:**

- Budget uses `min(context_tokens, tpm)` as the combined estimated-token ceiling. It reserves 384 output tokens and 96 fixed estimator tokens, leaving at most `min(context_tokens, tpm) - 480` estimated input units. The input estimate is UTF-8 byte length, not a provider tokenizer. Serialized request bytes are independently limited by `max_bytes`.
- Truncation keeps a shortened lead line/block first, then ranks remaining lines for pricing signals. A fitting request also has to fit the provider byte cap.
- Initial fitting shrinks the character cap by `0.6×` when it does not fit. HTTP 413 or recognized context overflow shrinks the excerpt actually sent by `0.6×`, then retries within the four-attempt provider loop.
- `ProviderGate` maintains a rolling 60-second RPM/TPM window and shared pause per provider **within one `LLMGateway` instance**. It is not a cross-process/global distributed limiter.
- `retry_delay` applies capped randomized exponential jitter and honors `Retry-After` seconds or HTTP dates by waiting at least the requested delay. `src/core/http.py` separately uses full-jitter exponential backoff for source HTTP requests.
- Repeated 429/5xx responses pause that provider gate. A quota-exhausted 429 gets at least a 300-second cooldown for that provider and execution falls through to the next configured provider.
- Every successful completion is schema-validated and passed through evidence grounding. Unsupported/conflicting pricing becomes null. Provider counters include requests, status codes, overflows, fallbacks, invalid output, and ungrounded output.

```text
source text
    |
budget + rank/truncate
    |
provider RPM/TPM reservation
    |
request --> 413/context? --yes--> shrink 0.6x --retry
    | no
429/5xx? --yes--> Retry-After/jitter pause --retry or fallback
    | success
schema validate --> evidence ground --> accepted result or null
```

**Not implemented:** map-reduce for multi-record pages, provider-specific tokenization, and persistent/distributed rate gates. Pricing is a standalone gateway/demo and is not integrated into product extraction.

<div class="page-break"></div>

## Page 3 — Freshness, exactly-once, and storage

**Current implementation:** `src/core/dates.py` parses supported timestamps to UTC uncertainty intervals; strict freshness uses the earliest possible time. `src/core/seen_store.py` implements URL canonicalization, a whitespace-normalized text fingerprint, SQLite `seen` claims, and source `runs` metadata. These helpers are not yet integrated as a complete source watermark pipeline.

**Production design (not in demo):**

- Treat uncertain publication dates as `[earliest, latest]`; strict freshness requires `earliest >= now - window`. If no date exists, use a per-source top-key watermark heuristic and overlap/reconcile on the next run rather than assigning fetch time as publication time.
- Canonicalize URLs (remove tracking parameters, fragment, `www.`, and trailing slash) and fingerprint normalized content for dedupe.
- Use Redis `SET NX EX` as a fast distributed claim path, with a Postgres `UNIQUE` constraint as the durable exactly-once authority. The demo currently uses SQLite only; it has no cross-node atomicity or TTL.
- Store normalized entities and JSONB source payloads in Postgres. Keep mutable popularity such as GitHub stars in append-only `metric_observations`, not as overwritten history.
- Store raw HTML/API payloads in S3 by content hash. Use pgvector for entity-resolution embeddings only as a review candidate generator; deterministic domain/name matching remains authoritative.
- Start with relational edge tables for company/product/paper relationships. **Proposed migration trigger:** move graph queries to Neo4j or Apache AGE only if multi-hop traversals exceed 30% of graph-query workload and indexed relational p95 remains above 500 ms for 30 consecutive days. These are proposed operational thresholds, not measured current metrics.

**Explicit demo boundary:** SQLite is implemented in `SeenStore`; Postgres, Redis, S3, JSONB, append-only metric history, pgvector, graph databases, leases, and the watermark integration are production design, not in this demo.

