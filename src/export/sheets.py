import csv
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import gspread
from gspread.exceptions import APIError, WorksheetNotFound

from src.config import settings
from src.core.dates import parse_date
from src.core.run_stats import record


TAB_INPUTS = {
    "Startups": ("startups.jsonl", "jsonl"),
    "Products": ("products.jsonl", "jsonl"),
    "Research Papers": ("research_papers.jsonl", "jsonl"),
    "Jobs": ("jobs.jsonl", "jsonl"),
    "News": ("news.jsonl", "jsonl"),
    "Entity Mapping Log": ("entity_mapping_log.csv", "csv"),
}
BASE_COLUMNS = ["schemaVersion", "recordType", "source.name", "source.url"]


def flatten(record: dict) -> dict:
    """Flatten nested record mappings into dotted-key columns."""
    flattened = {}

    def visit(value, prefix=""):
        if isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{prefix}.{key}" if prefix else str(key))
        elif isinstance(value, (list, tuple)):
            flattened[prefix] = "; ".join(_cell_value(item) for item in value)
        else:
            flattened[prefix] = value

    visit(record)
    return flattened


def _cell_value(value) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def _prepare_news(record: dict) -> dict:
    prepared = json.loads(json.dumps(record))

    def visit(value):
        if isinstance(value, dict):
            for key, child in list(value.items()):
                if key == "full_text" and isinstance(child, str):
                    truncated = len(child) > 45_000
                    value[key] = child[:45_000]
                    value["full_text_truncated"] = truncated
                else:
                    visit(child)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(prepared)
    return prepared


def _load_rows(tab: str) -> list[dict]:
    filename, kind = TAB_INPUTS[tab]
    path = settings.OUTPUT_DIR / filename
    if not path.exists():
        return []
    if kind == "csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    records = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                record = json.loads(line)
                records.append(_prepare_news(record) if tab == "News" else record)
    return records


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _api_call(callable_, *args, **kwargs):
    for attempt in range(6):
        try:
            return callable_(*args, **kwargs)
        except APIError as error:
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
            if status != 429 or attempt == 5:
                raise
            time.sleep(2**attempt)


def _worksheet(spreadsheet, title: str, rows: int, cols: int):
    try:
        worksheet = _api_call(spreadsheet.worksheet, title)
    except WorksheetNotFound:
        worksheet = _api_call(
            spreadsheet.add_worksheet,
            title=title,
            rows=max(100, rows),
            cols=max(1, cols),
        )
    else:
        _api_call(worksheet.clear)
    return worksheet


def _headers(rows: list[dict]) -> list[str]:
    keys = {key for row in rows for key in row}
    ordered = [key for key in BASE_COLUMNS if key in keys]
    content_columns = {key for key in keys if key.startswith("content.")}
    trailing = {
        key for key in keys
        if any(marker in key.lower() for marker in ("provenance", "pricing"))
    }
    ordered.extend(sorted(content_columns - trailing))
    if "collectedAt" in keys:
        ordered.append("collectedAt")
    ordered.extend(sorted(keys - set(ordered)))
    return ordered


def write_tab(sheet, title: str, rows: list[dict], chunk: int = 2000):
    flattened_rows = [flatten(row) for row in rows]
    headers = _headers(flattened_rows)
    if not headers:
        headers = (
            ["raw", "normalized", "compact", "canonical", "method", "score", "entity_id", "source_url"]
            if title == "Entity Mapping Log"
            else BASE_COLUMNS + ["collectedAt"]
        )
    worksheet = _worksheet(sheet, title, len(rows) + 1, len(headers))
    last_col = _column_name(len(headers))

    _api_call(
        worksheet.values_update,
        f"A1:{last_col}1",
        params={"valueInputOption": "RAW"},
        body={"values": [headers]},
    )
    for offset in range(0, len(flattened_rows), chunk):
        row_values = [
            [_cell_value(row.get(header)) for header in headers]
            for row in flattened_rows[offset:offset + chunk]
        ]
        start_row = offset + 2
        end_row = start_row + len(row_values) - 1
        _api_call(
            worksheet.values_update,
            f"A{start_row}:{last_col}{end_row}",
            params={"valueInputOption": "RAW"},
            body={"values": row_values},
        )
    _api_call(worksheet.freeze, rows=1)
    return worksheet


def _oldest_content_date(rows: list[dict]) -> str:
    parsed = []
    now = datetime.now(timezone.utc)
    for row in rows:
        value = flatten(row).get("content.date")
        if isinstance(value, str) and value:
            candidate = parse_date(value, now, "api_field")
            if candidate:
                parsed.append(candidate)
    return min(parsed, key=lambda item: item.earliest).raw if parsed else ""


def _git_commit(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _run_info(rows_by_tab: dict[str, list[dict]]) -> list[dict]:
    path = settings.OUTPUT_DIR / "run_stats.json"
    stats = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            stats = json.load(handle)

    root = Path.cwd()
    counts = {tab: len(rows) for tab, rows in rows_by_tab.items()}
    return [{
        "run_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "row_counts": counts,
        "oldest_content_date": {
            "Jobs": _oldest_content_date(rows_by_tab.get("Jobs", [])),
            "News": _oldest_content_date(rows_by_tab.get("News", [])),
        },
        "git_commit_hash": _git_commit(root),
        "pricing.llm_stats": stats.get("pricing", {}).get("llm_stats", {}),
        "orchestrator.stats": stats.get("orchestrator", {}).get("stats", {}),
    }]


def export(tabs: list[str] | None = None):
    requested = list(TAB_INPUTS) if tabs is None else tabs
    invalid = sorted(set(requested) - set(TAB_INPUTS))
    if invalid:
        raise ValueError(f"Unknown export tab(s): {', '.join(invalid)}")
    if not settings.GOOGLE_SERVICE_ACCOUNT_FILE or not settings.GOOGLE_SHEET_KEY:
        raise ValueError(
            "Set GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_SHEET_KEY in .env or environment."
        )

    client = gspread.service_account(filename=settings.GOOGLE_SERVICE_ACCOUNT_FILE)
    spreadsheet = client.open_by_key(settings.GOOGLE_SHEET_KEY)
    all_rows = {tab: _load_rows(tab) for tab in TAB_INPUTS}
    record("jobs.source_counts", records=len(all_rows["Jobs"]))
    record("news.source_counts", records=len(all_rows["News"]))
    for tab in requested:
        write_tab(spreadsheet, tab, all_rows[tab])
    write_tab(spreadsheet, "Run Info", _run_info(all_rows))

    print(spreadsheet.url)
    print('Reminder: set sharing to "Anyone with the link – Viewer".')