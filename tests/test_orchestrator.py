import json

import httpx
import pytest

from src.llm.grounding import PricingProposal, ground
from src.llm.orchestrator import LLMGateway
from src.llm.providers import Provider


FREE = "Free plan: $0/month"
PAID = "Pro: $15/month"
PRICING = f"{FREE}\n{PAID}"
PROPOSAL = {"pricingModel": "FREEMIUM", "pricingEvidence": [FREE, PAID]}


def success():
    return httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(PROPOSAL)}}],
    })


@pytest.mark.asyncio
async def test_413_shrinks_and_429_retries(monkeypatch):
    monkeypatch.setattr("src.llm.orchestrator.random.uniform", lambda *_: 0)
    sent = []

    def handler(request):
        sent.append(json.loads(request.content)["messages"][1]["content"])
        if len(sent) == 1:
            return httpx.Response(413)
        if len(sent) == 2:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return success()

    provider = Provider("primary", "https://primary.example/chat", "test", "fake")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = LLMGateway(client, [provider])
        result = await gateway.extract_pricing("Demo\n" + "Footer text\n" * 70 + PRICING)

    assert len(sent) == 3
    assert len(sent[1]) < len(sent[0])
    assert sent[1] == sent[2]
    assert result == PROPOSAL


@pytest.mark.asyncio
async def test_fallback_after_provider_failure():
    def handler(request):
        if request.url.host == "primary.example":
            return httpx.Response(401)
        return success()

    providers = [
        Provider("primary", "https://primary.example/chat", "test", "fake"),
        Provider("backup", "https://backup.example/chat", "test", "fake"),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        gateway = LLMGateway(client, providers)
        assert await gateway.extract_pricing(PRICING) == PROPOSAL
        assert gateway.stats["fallback"] == 1


def test_free_trial_does_not_prove_freemium():
    text = "Free trial for 7 days\nPro: $15/month"
    proposal = PricingProposal(pricingModel="FREEMIUM", pricingEvidence=text.splitlines())
    assert ground(proposal, text, text)["pricingModel"] is None