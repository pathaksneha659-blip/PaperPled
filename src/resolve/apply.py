import csv
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from src.core.run_stats import record as record_stats
from .resolver import Resolver


def write_entity_mapping_log_jsonl(decisions, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        for decision in decisions:
            handle.write(json.dumps(asdict(decision), ensure_ascii=False) + "\n")


def main():
    root = Path.cwd()
    resolver = Resolver(root / "data" / "seed_entities.json")

    for relative_path, field, raw_field, source_field, domain_field in (
        ("output/startups.jsonl", "entityName", "entityNameRaw", "startupNameSource", "website"),
        ("output/products.jsonl", "startupName", "startupNameRaw", "startupNameSource", None),
    ):
        path = root / relative_path
        records = []
        if not path.exists():
            continue

        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                content = record.setdefault("content", {})
                raw = content.get(raw_field, content.get(field))
                if not isinstance(raw, str):
                    raw = ""

                domain = None
                if domain_field:
                    data = content.get("data") or {}
                    domain = data.get(domain_field)

                source_url = record.get("source", {}).get("url", "")
                if field == "startupName" and "/spaces/" in source_url:
                    path_parts = urlsplit(source_url).path.strip("/").split("/")
                    if len(path_parts) >= 3 and path_parts[0] == "spaces":
                        raw = path_parts[1]
                match = resolver.resolve(raw, domain=domain, source_url=source_url)
                content[raw_field] = raw
                content[source_field] = match.method
                if match.canonical is not None:
                    content[field] = match.canonical

                records.append(record)

        with path.open("w", encoding="utf-8", newline="") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    mapping_path = root / "output" / "entity_mapping_log.csv"
    resolver.export_log(mapping_path)
    write_entity_mapping_log_jsonl(
        resolver.log, root / "output" / "entity_mapping_log.jsonl"
    )

    counts = Counter(match.method for match in resolver.log)
    record_stats("resolve.method_counts", **dict(counts))
    for method in ("registrable_domain", "exact_compact", "fuzzy", "ambiguous_review", "new_entity"):
        print(f"{method}: {counts[method]}")
    print(f"total: {len(resolver.log)}")


if __name__ == "__main__":
    main()
