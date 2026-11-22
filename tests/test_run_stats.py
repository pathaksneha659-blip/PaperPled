import json

from src.config import settings
from src.core.run_stats import record


def test_record_merges_nested_sections(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "OUTPUT_DIR", tmp_path)
    record("pricing.llm_stats", requests=2)
    record("pricing.llm_stats", ok=1)
    record("orchestrator.stats", startups=10)

    saved = json.loads((tmp_path / "run_stats.json").read_text(encoding="utf-8"))
    assert saved == {
        "pricing": {"llm_stats": {"requests": 2, "ok": 1}},
        "orchestrator": {"stats": {"startups": 10}},
    }
