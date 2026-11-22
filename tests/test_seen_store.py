from src.core.seen_store import SeenStore, canonical_url, fingerprint


def test_canonical_url_normalization():
    url = "https://WWW.Example.com/articles/one/?utm_source=x&ref=home&gclid=123#section"
    assert canonical_url(url) == "https://example.com/articles/one"


def test_rel_canonical_is_supported():
    assert canonical_url(
        "https://example.com/news",
        "https://www.example.com/news/story/?fbclid=1",
    ) == "https://example.com/news/story"


def test_claim_is_idempotent(tmp_path):
    store = SeenStore(tmp_path / "seen.db")
    assert store.claim("article-key", "news", "run-1")
    assert not store.claim("article-key", "news", "run-2")
    assert store.last_run("news") is None
    store.set_run("news", "article-key")
    assert store.last_run("news") is not None


def test_fingerprint_normalizes_whitespace_and_case():
    assert fingerprint("  Hello\nWORLD  ") == fingerprint("hello world")
