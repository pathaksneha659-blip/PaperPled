"""Local CSV export for FrontierAtlas datasets.

Reads each output/<dataset>.jsonl and writes output/csv/<dataset>.csv.
Standard library only: no network, no credentials, no paid services.

Design notes
- Two passes per file: pass 1 discovers the column set, pass 2 writes rows.
  Memory stays flat, so lakh-scale files work too.
- Nested dicts flatten to dotted columns (content.data.employeeCount);
  lists and empty dicts become JSON strings. Nothing is silently dropped.
- Written as UTF-8 with BOM (utf-8-sig) so Excel detects the encoding.
- Each dataset is isolated: a missing or broken file is reported and the
  remaining datasets still export.
"""
import csv
import json
import os
from pathlib import Path
from typing import Any, Iterable, Iterator

from src.logging_setup import log

__all__ = ["KNOWN_DATASETS", "run_csv_export", "export_dataset",
           "read_jsonl", "flatten_record", "to_cell"]

ENCODING = "utf-8-sig"
KNOWN_DATASETS = (
    "startups", "products", "research_papers", "jobs", "news",
    "entity_mapping_log",
)
# Characters that make Excel treat a text cell as a formula.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _normalize(name: str) -> str:
    """'Research Papers' -> 'research_papers'."""
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def read_jsonl(path: Path, log_errors: bool = True) -> Iterator[tuple[int, dict]]:
    """Yield (line_number, record) for each JSON object line. Bad lines are skipped and logged."""
    with path.open("r", encoding="utf-8-sig") as f:
        for line_no, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                if log_errors:
                    log.warning("csv_export.bad_json_line", file=path.name, line=line_no, error=exc.msg)
                continue
            if not isinstance(record, dict):
                if log_errors:
                    log.warning("csv_export.non_object_line", file=path.name, line=line_no)
                continue
            yield line_no, record


def flatten_record(record: dict, prefix: str = "") -> dict[str, Any]:
    """Nested non-empty dicts become dotted keys. Lists and empty dicts are kept as values."""
    flat: dict[str, Any] = {}
    for key, value in record.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            flat.update(flatten_record(value, prefix=f"{name}."))
        else:
            flat[name] = value
    return flat


def to_cell(value: Any) -> str:
    """Convert one value to the text written to a CSV cell."""
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    if isinstance(value, str) and text.startswith(FORMULA_PREFIXES):
        text = "'" + text   # neutralise formula injection in Excel
    return text


def _discover_columns(path: Path) -> list[str]:
    """Union of flattened keys across all records, in first-seen order."""
    seen: dict[str, None] = {}
    for _, record in read_jsonl(path, log_errors=False):
        seen.update(dict.fromkeys(flatten_record(record)))
    return list(seen)


def export_dataset(jsonl_path: Path, csv_path: Path) -> dict:
    """Export one JSONL dataset to one CSV file. Never raises; returns a status dict."""
    name = jsonl_path.stem
    result: dict[str, Any] = {"dataset": name, "status": "ok", "rows": 0,
                              "columns": 0, "path": str(csv_path)}

    if not jsonl_path.is_file():
        result["status"] = "missing"
        log.warning("csv_export.dataset_missing", dataset=name, expected=str(jsonl_path))
        return result

    tmp_path = csv_path.with_name(csv_path.name + ".tmp")
    try:
        columns = _discover_columns(jsonl_path)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        rows = 0
        with tmp_path.open("w", encoding=ENCODING, newline="") as f:
            if columns:                        # zero records -> empty file (Excel opens it blank)
                writer = csv.DictWriter(f, fieldnames=columns, restval="")
                writer.writeheader()
                for _, record in read_jsonl(jsonl_path):
                    flat = flatten_record(record)
                    writer.writerow({key: to_cell(value) for key, value in flat.items()})
                    rows += 1
        os.replace(tmp_path, csv_path)
    except Exception as exc:  # isolation boundary: one bad dataset must not stop the export
        tmp_path.unlink(missing_ok=True)
        result["status"] = "error"
        result["error"] = f"{type(exc).__name__}: {exc}"
        log.error("csv_export.dataset_failed", dataset=name, error=result["error"])
        return result

    result.update(rows=rows, columns=len(columns))
    log.info("csv_export.dataset_done", dataset=name, rows=rows,
             columns=len(columns), path=str(csv_path))
    return result


def run_csv_export(input_dir: Path | None = None,
                   output_dir: Path | None = None,
                   datasets: Iterable[str] | None = None) -> list[dict]:
    """Export every known or discovered dataset in input_dir to output_dir as CSV."""
    if input_dir is None or output_dir is None:
        from src.config import settings        # lazy import keeps tests independent of .env
        input_dir = input_dir if input_dir is not None else settings.OUTPUT_DIR
        output_dir = output_dir if output_dir is not None else settings.OUTPUT_DIR / "csv"
    input_dir, output_dir = Path(input_dir), Path(output_dir)

    discovered = {p.stem for p in input_dir.glob("*.jsonl")} if input_dir.is_dir() else set()
    all_names = list(KNOWN_DATASETS) + sorted(discovered - set(KNOWN_DATASETS))

    names = all_names
    if datasets is not None:
        wanted = {_normalize(d) for d in datasets if d and d.strip()}
        unknown = sorted(wanted - set(all_names))
        if unknown:
            log.warning("csv_export.unknown_dataset", requested=unknown)
        names = [n for n in all_names if n in wanted]

    log.info("csv_export.start", input_dir=str(input_dir), output_dir=str(output_dir), datasets=names)
    results = [export_dataset(input_dir / f"{n}.jsonl", output_dir / f"{n}.csv") for n in names]

    counts = {s: sum(r["status"] == s for r in results) for s in ("ok", "missing", "error")}
    log.info("csv_export.done", output_dir=str(output_dir), **counts)
    return results
