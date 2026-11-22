import csv
import json
from dataclasses import asdict
from pathlib import Path

from src.export.csv_export import (
    KNOWN_DATASETS, export_dataset, flatten_record, run_csv_export, to_cell,
)
from src.resolve.apply import write_entity_mapping_log_jsonl
from src.resolve.resolver import Match


def write_jsonl(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write((r if isinstance(r, str) else json.dumps(r, ensure_ascii=False)) + "\n")


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        return reader.fieldnames or [], rows


def test_successful_export_writes_csv(tmp_path):
    src, out = tmp_path / "in", tmp_path / "out" / "csv"
    write_jsonl(src / "startups.jsonl", [
        {"entityName": "Acme", "employeeCount": 12},
        {"entityName": "Beta", "employeeCount": 3},
    ])

    results = run_csv_export(input_dir=src, output_dir=out, datasets=["startups"])

    assert results[0]["status"] == "ok" and results[0]["rows"] == 2
    fields, rows = read_csv(out / "startups.csv")
    assert fields == ["entityName", "employeeCount"]
    assert rows == [
        {"entityName": "Acme", "employeeCount": "12"},
        {"entityName": "Beta", "employeeCount": "3"},
    ]


def test_jsonl_parsing_skips_bad_lines_without_crashing(tmp_path):
    src, out = tmp_path / "in", tmp_path / "out"
    write_jsonl(src / "news.jsonl", [
        {"title": "one"},
        "",
        "{not valid json",
        "[1, 2, 3]",
        {"title": "two"},
    ])

    result = export_dataset(src / "news.jsonl", out / "news.csv")

    assert result["status"] == "ok" and result["rows"] == 2
    _, rows = read_csv(out / "news.csv")
    assert [r["title"] for r in rows] == ["one", "two"]


def test_nested_dicts_flatten_and_lists_serialize(tmp_path):
    record = {
        "content": {"entityName": "Acme", "data": {"employeeCount": 5}},
        "tags": ["ai", "ml"],
        "empty": {},
    }
    flat = flatten_record(record)
    assert flat["content.data.employeeCount"] == 5
    assert flat["content.entityName"] == "Acme"
    assert to_cell(flat["tags"]) == '["ai", "ml"]'
    assert to_cell(flat["empty"]) == "{}"

    src, out = tmp_path / "in", tmp_path / "out"
    write_jsonl(src / "startups.jsonl", [record])
    export_dataset(src / "startups.jsonl", out / "startups.csv")
    fields, rows = read_csv(out / "startups.csv")
    assert "content.data.employeeCount" in fields
    assert rows[0]["content.data.employeeCount"] == "5"
    assert rows[0]["tags"] == '["ai", "ml"]'


def test_missing_fields_and_nulls_become_blank_and_columns_are_dynamic(tmp_path):
    src, out = tmp_path / "in", tmp_path / "out"
    write_jsonl(src / "products.jsonl", [
        {"a": 1, "b": None},
        {"a": 2, "c": "x"},
    ])
    export_dataset(src / "products.jsonl", out / "products.csv")
    fields, rows = read_csv(out / "products.csv")
    assert fields == ["a", "b", "c"]
    assert rows[0] == {"a": "1", "b": "", "c": ""}
    assert rows[1] == {"a": "2", "b": "", "c": "x"}


def test_missing_datasets_are_reported_not_raised(tmp_path):
    results = run_csv_export(input_dir=tmp_path / "empty_in", output_dir=tmp_path / "out")

    assert [r["dataset"] for r in results] == list(KNOWN_DATASETS)
    assert all(r["status"] == "missing" for r in results)
    assert not (tmp_path / "out" / "startups.csv").exists()


def test_empty_dataset_writes_empty_file(tmp_path):
    src, out = tmp_path / "in", tmp_path / "out"
    write_jsonl(src / "jobs.jsonl", [])

    result = export_dataset(src / "jobs.jsonl", out / "jobs.csv")

    assert result["status"] == "ok" and result["rows"] == 0
    assert (out / "jobs.csv").exists()
    assert (out / "jobs.csv").stat().st_size == 0


def test_utf8_bom_and_unicode_preserved(tmp_path):
    src, out = tmp_path / "in", tmp_path / "out"
    write_jsonl(src / "startups.jsonl", [{"entityName": "Café Zürich 北京"}])
    export_dataset(src / "startups.jsonl", out / "startups.csv")

    raw = (out / "startups.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")               # BOM for Excel
    assert "Café Zürich 北京" in raw.decode("utf-8-sig")


def test_output_directory_is_created(tmp_path):
    src = tmp_path / "in"
    write_jsonl(src / "news.jsonl", [{"title": "x"}])
    out = tmp_path / "a" / "b" / "csv"
    assert not out.exists()

    run_csv_export(input_dir=src, output_dir=out, datasets=["news"])

    assert (out / "news.csv").exists()


def test_entity_mapping_log_jsonl_exports_all_decisions_and_dynamic_columns(tmp_path):
    decisions = [
        Match("Raw One", "raw one", "rawone", "Canonical One", "exact_compact",
              100.0, "seed:one", "https://example.test/one"),
        Match("Raw Two", "raw two", "rawtwo", None, "new_entity",
              0.0, "dyn:rawtwo", "https://example.test/two"),
    ]
    input_dir = tmp_path / "output"
    jsonl_path = input_dir / "entity_mapping_log.jsonl"
    write_entity_mapping_log_jsonl(decisions, jsonl_path)

    records = [
        json.loads(line)
        for line in jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    assert records == [asdict(decision) for decision in decisions]

    output_dir = tmp_path / "csv"
    results = run_csv_export(input_dir=input_dir, output_dir=output_dir)
    mapping_result = next(
        result for result in results if result["dataset"] == "entity_mapping_log"
    )
    assert mapping_result["status"] == "ok"
    fields, rows = read_csv(output_dir / "entity_mapping_log.csv")
    assert {"raw", "canonical", "method", "score", "source_url"} <= set(fields)
    assert len(rows) == len(decisions)


def test_formula_like_text_is_neutralised_and_numbers_untouched():
    assert to_cell("=HYPERLINK('http://x')") == "'=HYPERLINK('http://x')"
    assert to_cell(-5) == "-5"
    assert to_cell(None) == ""
    assert to_cell(True) == "true"


def test_one_failing_dataset_does_not_stop_others(tmp_path):
    src, out = tmp_path / "in", tmp_path / "out"
    write_jsonl(src / "startups.jsonl", [{"entityName": "Acme"}])
    write_jsonl(src / "products.jsonl", [{"productName": "Widget"}])
    # Block the startups output by making its target path a directory.
    (out / "startups.csv").mkdir(parents=True)

    results = {r["dataset"]: r for r in run_csv_export(input_dir=src, output_dir=out,
                                                       datasets=["startups", "products"])}

    assert results["startups"]["status"] == "error"
    assert results["products"]["status"] == "ok"
    assert (out / "products.csv").exists()