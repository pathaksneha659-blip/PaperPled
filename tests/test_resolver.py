import pytest

from src.resolve.resolver import Resolver


@pytest.fixture
def resolver():
    return Resolver("data/seed_entities.json")


@pytest.mark.parametrize(
    ("raw", "method"),
    [
        ("OpenAI, Inc.", "exact_compact"),
        ("Open AI", "exact_compact"),
        ("Anthropic PBC", "exact_compact"),
        ("Mistral AI SAS", "exact_compact"),
        ("HuggingFace", "exact_compact"),
        ("xAI Corp", "exact_compact"),
    ],
)
def test_exact_compact_variants(resolver, raw, method):
    match = resolver.resolve(raw)
    assert match.method == method
    assert match.canonical in ("OpenAI", "Anthropic", "Mistral AI", "Hugging Face", "xAI")


def test_domain_match(resolver):
    match = resolver.resolve("Unknown Name", domain="https://www.openai.com/blog")
    assert match.canonical == "OpenAI"
    assert match.method == "registrable_domain"


def test_coherent_does_not_match_cohere(resolver):
    match = resolver.resolve("Coherent Labs")
    assert match.method in ("ambiguous_review", "new_entity")
    assert match.canonical != "Cohere"


def test_dynamic_entity_is_stable(resolver):
    first = resolver.resolve("Acme Robotics Pvt Ltd")
    second = resolver.resolve("Acme Robotics Pvt Ltd")
    assert first.method == "new_entity"
    assert second.method == "new_entity"
    assert first.entity_id == second.entity_id
    assert first.entity_id.startswith("dyn:")


def test_empty_string(resolver):
    match = resolver.resolve("")
    assert match.canonical is None