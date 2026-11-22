import argparse
import asyncio
import json
import math
import random
import time
from collections import Counter, deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

from .budget import fit_request
from .grounding import PricingProposal, ground
from .providers import Provider, load_providers
from src.core.run_stats import record


class ProviderGate:
    """One shared rolling RPM/TPM window and cooldown per provider."""

    def __init__(self):
        self.calls = deque()
        self.tokens = 0
        self.paused_until = 0.0
        self.lock = asyncio.Lock()

    async def pause(self, seconds: float):
        async with self.lock:
            self.paused_until = max(self.paused_until, time.monotonic() + seconds)

    async def reserve(self, provider: Provider, tokens: int) -> bool:
        deadline = time.monotonic() + 10
        while True:
            async with self.lock:
                now = time.monotonic()
                while self.calls and self.calls[0][0] <= now - 60:
                    _, expired = self.calls.popleft()
                    self.tokens -= expired

                wait = max(0.0, self.paused_until - now)
                if len(self.calls) >= provider.rpm or self.tokens + tokens > provider.tpm:
                    wait = max(wait, self.calls[0][0] + 60 - now)

                if wait == 0:
                    self.calls.append((now, tokens))
                    self.tokens += tokens
                    return True

            # Do not retry early. A long wait makes this provider unavailable
            # for this extraction; another configured provider may be ready.
            if now + wait > deadline:
                return False
            await asyncio.sleep(wait)


def retry_delay(attempt: int, header: str | None = None) -> float:
    jitter = random.uniform(0, min(16, 2 ** attempt))
    if not header:
        return jitter

    try:
        seconds = float(header)
    except ValueError:
        try:
            when = parsedate_to_datetime(header)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return jitter

    return max(jitter, seconds) if math.isfinite(seconds) else jitter


class LLMGateway:
    def __init__(self, client: httpx.AsyncClient, providers: list[Provider]):
        self.client = client
        self.providers = providers
        self.gates = {p.name: ProviderGate() for p in providers}
        self.slots = {p.name: asyncio.Semaphore(4) for p in providers}
        self.stats = Counter()

    async def extract_pricing(self, text: str) -> dict:
        if not text.strip():
            return {"pricingModel": None, "pricingEvidence": []}

        for index, provider in enumerate(self.providers):
            gate = self.gates[provider.name]
            cap = min(len(text), provider.max_bytes)

            for attempt in range(4):
                fitted = fit_request(provider, text, cap)
                if fitted is None:
                    break
                body, excerpt, tokens = fitted

                async with self.slots[provider.name]:
                    if not await gate.reserve(provider, tokens):
                        self.stats[f"{provider.name}.quota_wait"] += 1
                        break

                    self.stats[f"{provider.name}.requests"] += 1
                    try:
                        response = await self.client.post(
                            provider.url,
                            content=body,
                            headers={
                                "Authorization": f"Bearer {provider.key}",
                                "Content-Type": "application/json",
                            },
                        )
                    except httpx.TransportError:
                        self.stats[f"{provider.name}.transport_error"] += 1
                        await gate.pause(retry_delay(attempt))
                        continue

                    status = response.status_code
                    if status == 429 or status >= 500:
                        self.stats[f"{provider.name}.{status}"] += 1
                        delay = retry_delay(attempt, response.headers.get("Retry-After"))
                        quota_exhausted = status == 429 and any(
                            marker in response.text.lower()
                            for marker in ("insufficient_quota", "daily quota", "per day")
                        )
                        await gate.pause(max(delay, 300) if quota_exhausted else delay)
                        if quota_exhausted:
                            break
                        continue

                context_error = status == 400 and any(
                    marker in response.text.lower()
                    for marker in ("context_length_exceeded", "context window", "too many tokens")
                )
                if status == 413 or context_error:
                    self.stats[f"{provider.name}.overflow"] += 1
                    cap = len(excerpt) * 3 // 5  # shrink what was actually sent
                    continue

                if not response.is_success:
                    self.stats[f"{provider.name}.{status}"] += 1
                    break

                try:
                    choice = response.json()["choices"][0]
                    if choice.get("finish_reason") == "length":
                        raise ValueError("Truncated completion")
                    proposal = PricingProposal.model_validate_json(choice["message"]["content"])
                except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                    self.stats[f"{provider.name}.invalid_output"] += 1
                    break

                result = ground(proposal, excerpt, text)
                self.stats[f"{provider.name}.ok"] += 1
                if proposal.pricingModel and result["pricingModel"] is None:
                    self.stats[f"{provider.name}.ungrounded"] += 1
                record("pricing.llm_stats", **dict(self.stats))
                return result

            if index + 1 < len(self.providers):
                self.stats["fallback"] += 1

        record("pricing.llm_stats", **dict(self.stats))
        raise RuntimeError(f"No provider completed extraction. Stats: {dict(self.stats)}")


async def demo(path: Path):
    async with httpx.AsyncClient(timeout=30) as client:
        gateway = LLMGateway(client, load_providers())
        result = await gateway.extract_pricing(path.read_text(encoding="utf-8-sig"))
        print(json.dumps({"result": result, "stats": dict(gateway.stats)}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path, help="Plain-text pricing section for one product")
    args = parser.parse_args()
    asyncio.run(demo(args.path))