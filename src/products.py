import json
from datetime import datetime, timezone
from src.core.http import HttpClient
from src.schemas import ProductRecord, ProductContent, SourceInfo, PricingModelEnum
from src.config import settings
from src.logging_setup import log


def _norm_name(product_name: str) -> str:
    return " ".join(product_name.lower().split())


async def run_products_extractor(http: HttpClient, target_count: int = 1000) -> list[ProductRecord]:
    log.info("products.fetch_start", target_count=target_count)
    records = []
    seen_ids = set()
    seen_names = set()
    now_iso = datetime.now(timezone.utc).isoformat()

    for page in range(30):   # stop at 30 pages; ~100 per page is plenty for 1,000
        url = f"https://huggingface.co/api/spaces?limit=100&full=false&p={page}"
        try:
            items = await http.get(url, as_json=True)
        except Exception as e:
            log.warning("products.page_error", page=page, error=str(e))
            break

        if not items:
            log.info("products.no_items", page=page)
            break

        before = len(records)
        for s in items:
            space_id = s.get("id")
            if not space_id or space_id in seen_ids:
                continue

            parts = space_id.split("/", 1)
            startup_or_author = parts[0] if len(parts) == 2 else "Independent"
            product_name = parts[1].replace("-", " ").replace("_", " ").title() if len(parts) == 2 else space_id

            # Dedupe by normalized product name (collapse near-identical entries)
            name_key = _norm_name(product_name)
            if name_key in seen_names:
                continue
            seen_names.add(name_key)
            seen_ids.add(space_id)

            rec = ProductRecord(
                schemaVersion="1.0",
                recordType="PRODUCT",
                source=SourceInfo(
                    name="Hugging Face AI Spaces Directory",
                    url=f"https://huggingface.co/spaces/{space_id}"
                ),
                content=ProductContent(
                    productName=product_name,
                    startupName=startup_or_author,
                    pricingModel=PricingModelEnum.FREEMIUM
                ),
                collectedAt=now_iso
            )
            records.append(rec)
            if len(records) >= target_count:
                break

        log.info("products.page_done", page=page, total=len(records))
        if len(records) == before:
            log.info("products.exhausted", page=page, note="no new records — stopping")
            break
        if len(records) >= target_count:
            break

    out_file = settings.OUTPUT_DIR / "products.jsonl"
    with open(out_file, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r.model_dump(), ensure_ascii=False) + "\n")

    log.info("products.saved_to_disk", count=len(records), path=str(out_file))
    return records