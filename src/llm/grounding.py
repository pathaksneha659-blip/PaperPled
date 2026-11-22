import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PricingProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    pricingModel: Literal["FREE", "FREEMIUM", "PAID", "ENTERPRISE"] | None
    pricingEvidence: list[str] = Field(max_length=4)


RULES = {
    "FREE": (r"(?:all plans are free|all features are free)[.!]?",),
    "FREEMIUM": (
        r"free(?: plan)?:\s*[$€£]0(?:\.00)?\s*/\s*(?:mo|month|year)",
        r"(?:pro|plus|premium|team|business)(?: plan)?:\s*"
        r"[$€£][1-9]\d*(?:\.\d{2})?\s*/\s*(?:mo|month|year)",
    ),
    "PAID": (r"(?:no free plan|paid plans only|all plans are paid)[.!]?",),
    "ENTERPRISE": (
        r"(?:enterprise plans only|only available to enterprise customers)[.!]?",
    ),
}


def ground(proposal: PricingProposal, excerpt: str, original: str) -> dict:
    unknown = {"pricingModel": None, "pricingEvidence": []}
    if proposal.pricingModel is None:
        return unknown

    original_lines = {line.strip() for line in original.splitlines() if line.strip()}
    shown_lines = {line.strip() for line in excerpt.splitlines() if line.strip()}
    quotes = [quote.strip() for quote in proposal.pricingEvidence]

    if not quotes or any(quote not in original_lines & shown_lines for quote in quotes):
        return unknown

    def proof(patterns, lines):
        return [
            next((line for line in lines if re.fullmatch(pattern, line, re.I)), None)
            for pattern in patterns
        ]

    # If the source supports conflicting classifications, send it to review.
    supported = {label for label, rules in RULES.items() if all(proof(rules, original_lines))}
    evidence = proof(RULES[proposal.pricingModel], quotes)
    if supported != {proposal.pricingModel} or not all(evidence):
        return unknown

    return {"pricingModel": proposal.pricingModel, "pricingEvidence": evidence}