# FrontierAtlas Ingestion Pipeline

FrontierAtlas is an automated, high-throughput market intelligence pipeline designed to ingest, resolve, normalize, and export entity data across five core AI verticals: Startups, Products, Research Papers, Jobs, and News. To prevent synthetic inaccuracy, the engine operates under a strict anti-hallucination policy: LLMs are used exclusively to extract and structure data from verified source text, never to generate facts, infer entities, or invent sources. Every stored record carries immutable provenance, including `source.name`, `source.url`, and `collectedAt` timestamps. Pricing models are classified as **Verified (grounded quote)** only when substantiated by verbatim quotes from the fetched source text; all ungrounded or absent classifications default to **Unverified default** (`null`).

---

## Windows Setup (PowerShell)

### Prerequisites
* **OS**: Windows 10/11 x64
* **Python**: Python 3.14+ (or Python 3.11+)

### Environment Initialization
Open PowerShell in the project directory:

```powershell
# 1. Create a virtual environment
python -m venv .venv

# 2. Activate the virtual environment
.\.venv\Scripts\Activate.ps1

# 3. Upgrade pip and install core dependencies
python -m pip install --upgrade pip
pip install -r requirements.txt

# 4. Optional: Install browser binaries for Tier 3 Playwright fetching
playwright install chromium
```

### Environment Variables (`.env`)
Create a `.env` file in the repository root. Only keys are listed below (never commit secret values):

```env
# GitHub API Tokens (comma-separated for rotation)
GITHUB_TOKENS=

# Local Pipeline Configuration
OUTPUT_DIR=output

# LLM Gateway: Google Gemini
GEMINI_API_KEY=
GEMINI_MODEL=
GEMINI_RPM=
GEMINI_TPM=
GEMINI_CONTEXT_TOKENS=
GEMINI_MAX_BYTES=

# LLM Gateway: Groq
GROQ_API_KEY=
GROQ_MODEL=
GROQ_RPM=
GROQ_TPM=
GROQ_CONTEXT_TOKENS=
GROQ_MAX_BYTES=

# LLM Gateway: DeepSeek
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=
DEEPSEEK_RPM=
DEEPSEEK_TPM=
DEEPSEEK_CONTEXT_TOKENS=
DEEPSEEK_MAX_BYTES=
```

---

## Execution Commands

Execute pipeline commands from the repository root within PowerShell:

```powershell
# Run all extractors with a minimum target limit of 1,000 records per vertical
python -m src.main all --limit 1000

# Run only the Ashby ATS jobs extractor (24-hour freshness window)
python -m src.main jobs

# Run only the Google News RSS extractor (24-hour freshness window)
python -m src.main news

# Run cross-source entity resolution against seed organizations
python -m src.main resolve

# Export all datasets to output\csv\ as UTF-8 CSV files (startups, products, research_papers, jobs, news, entity_mapping_log).
python -m src.main export --tabs "Startups,Products,Research Papers,Jobs,News,Entity Mapping Log"

# Execute the test suite
python -m pytest -q
```

---

## Measured Pipeline Results

All statistics reflect recorded values in `output/run_stats.json` and local dataset outputs:

### Dataset Records by Tab

| Tab / Vertical | Recorded Output Rows | Freshness Window | Pricing Classification Status |
| :--- | :--- | :--- | :--- |
| **Startups** (`startups.jsonl`) | 1000 | Static catalog | N/A |
| **Products** (`products.jsonl`) | 1000 | Directory index | 1000 Unverified default (`null`), 0 Verified (grounded quote) |
| **Research Papers** (`research_papers.jsonl`) | 1000 | Last 180 days | N/A |
| **Jobs** (`jobs.jsonl`) | 24 | < 24 hours (`2026-11-21T19:13:50Z`) | N/A |
| **News** (`news.jsonl`) | 71 | < 24 hours (`2026-11-21T19:13:53Z`) | N/A |
| **Entity Mapping Log** (`entity_mapping_log.csv`) | 2000 | Cross-vertical run | N/A |
| **Total Run Duration** | Not measured in this run | — | — |

### LLM Gateway Provider Statistics

Recorded metrics from the multi-provider resilient fallback orchestrator (`run_stats.json`):

| Provider | Requests | HTTP 429 | HTTP 413 / Overflow | Fallback Triggered | OK (200) | Ungrounded Output |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **primary** | 1 | 1 | 1 | 1 | 1 | 0 |
| **backup** | 1 | 0 | 0 | 0 | 1 | 0 |
| **gemini** | 1 | 0 | 0 | 0 | 0 (HTTP 403) | 0 |
| **groq** | 1 | 0 | 0 | 0 | 1 (HTTP 404 handled) | 1 |
| **DeepSeek** | Not configured in this run (Gemini 403; Groq carried the chain) | — | — | — | — | — |

---

## Design Decisions

* **Deterministic Discovery Over Web Crawling**: Ingests high-signal feeds (Hugging Face daily papers, arXiv ID batch APIs, Y Combinator indexes, Ashby ATS endpoints) rather than executing unbounded recursive link crawling.
* **Deterministic-First Extraction**: High-speed Lexbor DOM parsing and structured API payloads extract schema attributes deterministically; LLMs are used only for unstructured pricing analysis.
* **Resilient LLM Cascades**: `LLMGateway` implements sequential multi-provider failover (`Gemini -> Groq -> DeepSeek`), rolling RPM/TPM concurrency windows, payload shrink-on-overflow (60% stepdown), and full-jitter backoff on 429 rate limits.
* **Strict Grounding Enforcement**: Extracted pricing proposals require verbatim line-level evidence matching both the source document and model prompt; ungrounded outputs are discarded to `null`.
* **Hybrid Deduplication & Freshness**: Closed UTC interval parsing (`earliest`, `latest`) enforces strict 24-hour freshness boundaries. Canonical URL normalization and SHA-256 content fingerprinting prevent duplicate ingestion.
* **Tiered Anti-Bot Architecture**: Tier 1 (`httpx`) escalates to Tier 2 (`curl_cffi` browser impersonation) and Tier 3 (`Playwright` persistent context) upon encountering challenge signals; hard blocks halt safely at Tier 4 without attempting captcha circumvention.

---

## Known Limitations

* **Hugging Face Spaces Capping**: The Hugging Face Spaces API limits unauthenticated traversal batches. Products are derived from available catalog partitions and YC entities; pricing is marked **Verified (grounded quote)** only when an explicit source quote is validated, and remains **Unverified default** otherwise.
* **ATS Coverage Scope**: Job tracking covers only configured ATS platforms (Ashby boards for tracked organizations: Cursor, OpenAI, Perplexity, Cerebras, Harvey); unconfigured company boards or non-Ashby portals are not indexed.
* **News Freshness Dependency**: Google News RSS enforces a rolling 24-hour window that is evaluated relative to the execution timestamp `now()`. Total record counts vary directly depending on news volume at run time.
* **Anti-Bot Compliance Boundaries**: The pipeline does **not** bypass Cloudflare or DataDome bot protection. If challenge barriers or captchas are encountered, the fetcher logs challenge signals, adjusts adaptive backoff, and halts at Tier 4. Protected or ToS-restricted sources must be accessed via licensed APIs or authorized partnerships.
