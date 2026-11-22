import json
from datetime import datetime, timezone
from src.core.http import HttpClient
from src.schemas import StartupRecord, StartupContent, StartupData, SourceInfo
from src.config import settings
from src.logging_setup import log

AI_TAGS = {
    "artificial intelligence", "ai", "machine learning", "generative ai", 
    "nlp", "computer vision", "deep learning", "robotics", "llm", "ai assistant",
    "autonomous", "speech recognition", "data science"
}

async def run_startups_extractor(http: HttpClient, target_count: int = 1000) -> list[StartupRecord]:
    log.info("startups.fetch_start", target_count=target_count)
    url = "https://yc-oss.github.io/api/companies/all.json"
    companies = await http.get(url, as_json=True)

    records = []
    now_iso = datetime.now(timezone.utc).isoformat()
    seen_slugs = set()

    for c in companies:
        raw_tags = c.get("tags") or []
        raw_industries = c.get("industries") or []
        tags = {
            str(tag).strip().lower()
            for tag in raw_tags + raw_industries
            if str(tag).strip()
        }
        is_ai = bool(tags & AI_TAGS) or any("ai" in t for t in tags)
        if not is_ai:
            continue

        name = c.get("name")
        slug = c.get("slug")
        if not isinstance(name, str) or not name.strip() or not isinstance(slug, str) or not slug:
            continue
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)

        team_size = c.get("team_size")
        emp_count = int(team_size) if team_size and str(team_size).isdigit() else None

        canonical_url = f"https://www.ycombinator.com/companies/{slug}"

        rec = StartupRecord(
            schemaVersion="1.0",
            recordType="STARTUP",
            source=SourceInfo(
                name="Y Combinator Directory",
                url=canonical_url
            ),
            content=StartupContent(
                entityName=name.strip(),
                data=StartupData(
                    employeeCount=emp_count,
                    website=c.get("website"),
                    batch=c.get("batch"),
                    tags=list(tags)[:5]
                )
            ),
            collectedAt=now_iso
        )
        records.append(rec)
        if len(records) >= target_count:
            break

    out_file = settings.OUTPUT_DIR / "startups.jsonl"
    with open(out_file, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r.model_dump(), ensure_ascii=False) + "\n")

    log.info("startups.saved_to_disk", count=len(records), path=str(out_file))
    return records