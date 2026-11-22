"""Multi-provider LLM gateway.

  * shared per-provider RPM/TPM windows (one gate per provider per process)
  * 429 / 5xx: Retry-After-aware backoff with full jitter
  * 413 / context overflow: shrink the payload that was actually sent, retry same provider
  * hard failures (auth, quota, region, invalid output): record diagnostics, fall over
  * every accepted answer is grounded (grounding.py)
  * every HTTP failure is logged with status, provider, model and a redacted body snippet
"""
import argparse
import asyncio
import json
import logging
import math
import random
import re
import sys
import time
from collections import Counter, deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from .budget import fit_request
from .grounding import PricingProposal, ground
from .providers import Provider, load_providers

__all__ = ["ProviderGate", "LLMGateway", "ExtractionFailed", "retry_delay", "redact"]

log = logging.getLogger("frontieratlas.llm")

MAX_ATTEMPTS = 4                 # per provider, per extraction
PATIENCE_S = 10.0                # max wait for a window slot when another provider can take over
LAST_RESORT_PATIENCE_S = 90.0    # max wait when this is the final provider in the chain
QUOTA_COOLDOWN_S = 300.0
ERROR_BODY_LIMIT = 1000          # max characters of a response body that get logged
REDACTED = "***REDACTED***"
QUOTA_MARKERS = ("per day", "perday", "daily", "insufficient_quota", "insufficient balance")
CONTEXT_MARKERS = ("context_length_exceeded", "context window", "maximum context",
                   "too many tokens", "token count exceeds", "request too large")

# Credential-shaped strings. Removed from every log line and diagnostic.
_SECRET_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),       # Google API keys
    re.compile(r"gsk_[0-9A-Za-z]{20,}"),          # Groq keys
    re.compile(r"sk-[0-9A-Za-z_\-]{16,}"),        # DeepSeek / OpenAI-style keys
    re.compile(r"(?i)bearer\s+[0-9A-Za-z._\-]+"),
)


def redact(text: str, secret: str | None = None) -> str:
    """Remove the provider's own key and any credential-shaped string from `text`."""
    out = text.replace(secret, REDACTED) if secret else text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def _hint(status: int, body: str) -> str:
    """Plain-language next step for common failures. `body` must already be redacted."""
    low = body.lower()
    if status == 401:
        return "Key missing or invalid. Check <PROVIDER>_API_KEY in .env."
    if status == 403:
        if "leaked" in low:
            return "Key reported as leaked. Revoke it and create a new key."
        if "not enabled" in low or "has not been used" in low or "disabled" in low:
            return "The API is not enabled for this key's project. Enable it in the cloud console."
        if "restricted" in low or "referer" in low or "ip address" in low:
            return "Key restrictions block this request. Adjust the key's API or referrer restrictions."
        if "location" in low or "region" in low or "country" in low:
            return "Region or country is not supported for this account."
        return "Permission denied. Check model access for this key, project status, and billing."
    if status == 404:
        return "Model or endpoint not found. Check <PROVIDER>_MODEL (try it with and without a 'models/' prefix)."
    if status == 400:
        return "Bad request. Check the model name and request format."
    if status == 429:
        return "Rate or quota limit. Check the quota shown in the response body."
    if status >= 500:
        return "Provider server error. Retried with backoff."
    return ""


def _unknown() -> dict:
    return {"pricingModel": None, "pricingEvidence": []}


def retry_delay(attempt: int, header: str | None = None) -> float:
    """Full-jitter exponential backoff, never shorter than the server's Retry-After."""
    jitter = random.uniform(0, min(16, 2 ** attempt))
    if not header:
        return jitter
    try:
        seconds = float(header)                                   # delta-seconds form
    except ValueError:
        try:                                                      # HTTP-date form
            when = parsedate_to_datetime(header)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return jitter
    return max(jitter, seconds) if math.isfinite(seconds) else jitter


class ExtractionFailed(RuntimeError):
    """No provider produced a grounded answer. `errors` holds the last redacted diagnostic per provider."""

    def __init__(self, stats: dict, errors: dict):
        self.stats = stats
        self.errors = errors
        super().__init__(
            "No provider completed extraction.\n"
            f"stats={json.dumps(stats)}\n"
            f"last_errors={json.dumps(errors, ensure_ascii=False, indent=2)}"
        )


class ProviderGate:
    """Rolling 60-second RPM/TPM window plus cooldown, shared by every coroutine using one provider."""

    def __init__(self) -> None:
        self.calls: deque[tuple[float, int]] = deque()            # (monotonic_ts, tokens)
        self.tokens = 0
        self.paused_until = 0.0
        self.lock = asyncio.Lock()

    async def pause(self, seconds: float) -> None:
        async with self.lock:
            self.paused_until = max(self.paused_until, time.monotonic() + seconds)

    async def reserve(self, provider: Provider, tokens: int, patience: float = PATIENCE_S) -> bool:
        """Book one request + `tokens`. False if the slot is further away than `patience` seconds."""
        if tokens > provider.tpm:
            return False
        deadline = time.monotonic() + patience
        while True:
            async with self.lock:
                now = time.monotonic()
                while self.calls and self.calls[0][0] <= now - 60:
                    _, spent = self.calls.popleft()
                    self.tokens -= spent
                wait = max(0.0, self.paused_until - now)
                if self.calls and (len(self.calls) >= provider.rpm or self.tokens + tokens > provider.tpm):
                    wait = max(wait, self.calls[0][0] + 60 - now)
                if wait <= 0:
                    self.calls.append((now, tokens))
                    self.tokens += tokens
                    return True
            if now + wait > deadline:
                return False
            await asyncio.sleep(wait)


class LLMGateway:
    def __init__(self, client: httpx.AsyncClient, providers: list[Provider], in_flight_per_provider: int = 4) -> None:
        if not providers:
            raise ValueError("LLMGateway needs at least one Provider")
        self.client = client
        self.providers = list(providers)
        self.gates = {p.name: ProviderGate() for p in self.providers}
        self.slots = {p.name: asyncio.Semaphore(in_flight_per_provider) for p in self.providers}
        self.stats: Counter[str] = Counter()
        self.last_errors: dict[str, dict] = {}                    # provider name -> last redacted diagnostic

    async def extract_pricing(self, text: str) -> dict:
        if not text.strip():
            return _unknown()
        last = len(self.providers) - 1
        for index, provider in enumerate(self.providers):
            result = await self._try_provider(provider, text, has_fallback=index < last)
            if result is not None:
                return result
            if index < last:
                self.stats["fallback"] += 1
        raise ExtractionFailed(dict(self.stats), dict(self.last_errors))

    def _record_http_error(self, provider: Provider, status: int, raw: str, attempt: int,
                           level: int = logging.ERROR) -> None:
        """Log and store one HTTP failure. Never includes headers or the raw key."""
        body = redact(raw, provider.key)
        info = {
            "provider": provider.name,
            "model": provider.model,
            "status": status,
            "attempt": attempt + 1,
            "hint": _hint(status, body),
            "body_truncated": len(body) > ERROR_BODY_LIMIT,
            "body": body[:ERROR_BODY_LIMIT],
        }
        self.last_errors[provider.name] = info
        log.log(level, "llm.http_error %s", json.dumps(info, ensure_ascii=False))

    async def _try_provider(self, provider: Provider, text: str, has_fallback: bool) -> dict | None:
        gate = self.gates[provider.name]
        patience = PATIENCE_S if has_fallback else LAST_RESORT_PATIENCE_S
        cap = min(len(text), provider.max_bytes)

        for attempt in range(MAX_ATTEMPTS):
            fitted = fit_request(provider, text, cap)
            if fitted is None:
                self.stats[f"{provider.name}.unfittable"] += 1
                return None
            body, excerpt, tokens = fitted

            async with self.slots[provider.name]:                 # bounds in-flight requests only
                if not await gate.reserve(provider, tokens, patience):
                    self.stats[f"{provider.name}.quota_wait"] += 1
                    return None
                self.stats[f"{provider.name}.requests"] += 1
                try:
                    response = await self.client.post(
                        provider.url,
                        content=body,
                        headers={"Authorization": f"Bearer {provider.key}",
                                 "Content-Type": "application/json"},
                    )
                except httpx.TransportError as exc:
                    self.stats[f"{provider.name}.transport_error"] += 1
                    log.warning("llm.transport_error provider=%s model=%s attempt=%d error=%s",
                                provider.name, provider.model, attempt + 1, type(exc).__name__)
                    await gate.pause(retry_delay(attempt))
                    continue

            status = response.status_code
            raw = response.text
            lowered = raw.lower()

            if status == 429 or status >= 500:                     # transient: back off, retry
                self.stats[f"{provider.name}.{status}"] += 1
                delay = retry_delay(attempt, response.headers.get("Retry-After"))
                quota = status == 429 and any(m in lowered for m in QUOTA_MARKERS)
                self._record_http_error(provider, status, raw, attempt,
                                        level=logging.ERROR if quota else logging.WARNING)
                if quota:
                    await gate.pause(max(delay, QUOTA_COOLDOWN_S)) # quota gone: fall over now
                    return None
                await gate.pause(delay)
                continue

            if status == 413 or (status == 400 and any(m in lowered for m in CONTEXT_MARKERS)):
                self.stats[f"{provider.name}.overflow"] += 1
                self._record_http_error(provider, status, raw, attempt, level=logging.WARNING)
                cap = len(excerpt) * 3 // 5                        # shrink what was actually sent
                continue

            if not response.is_success:                            # 401 / 403 / 404 / other 4xx
                self.stats[f"{provider.name}.{status}"] += 1
                self._record_http_error(provider, status, raw, attempt)
                return None

            try:
                choice = response.json()["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise ValueError("truncated completion")
                proposal = PricingProposal.model_validate_json(choice["message"]["content"])
            except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
                self.stats[f"{provider.name}.invalid_output"] += 1
                self.last_errors[provider.name] = {
                    "provider": provider.name, "model": provider.model, "status": status,
                    "error": f"invalid_output: {type(exc).__name__}",
                }
                log.warning("llm.invalid_output provider=%s model=%s error=%s",
                            provider.name, provider.model, type(exc).__name__)
                return None

            result = ground(proposal, excerpt, text)
            self.stats[f"{provider.name}.ok"] += 1
            if proposal.pricingModel and result["pricingModel"] is None:
                self.stats[f"{provider.name}.ungrounded"] += 1
            return result

        self.stats[f"{provider.name}.exhausted"] += 1
        return None


async def demo(path: Path) -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    text = path.read_text(encoding="utf-8-sig")
    async with httpx.AsyncClient(timeout=30) as client:
        gateway = LLMGateway(client, load_providers())
        result = await gateway.extract_pricing(text)   # on failure: raises ExtractionFailed
    print(json.dumps({"result": result, "stats": dict(gateway.stats)}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live smoke test for the LLM gateway")
    parser.add_argument("path", type=Path, help="Plain-text pricing section of ONE verified product page")
    asyncio.run(demo(parser.parse_args().path))