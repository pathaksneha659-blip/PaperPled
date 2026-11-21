import json
from datetime import datetime, timezone
from src.core.http import HttpClient
from src.schemas import ProductRecord, ProductContent, SourceInfo, PricingModelEnum
from src.config import settings
from src.logging_setup import log

async def run_products_extractor(http: HttpClient, target_count: int = 1000) -> list[ProductRecord]:
    log.info("products.fetch_start", target_count=target_count)
    records = []
    now_iso = datetime.now(timezone.utc).isoformat()
    seen_products = set()

    limit_per_page = 500
    # Paginate through Hugging Face Spaces API
    for page in range(4):
        url = f"https://huggingface.co/api/spaces?limit={limit_per_page}&full=false"
        try:
            spaces = await http.get(url, as_json=True)
            for s in spaces:
                space_id = s.get("id")
                if not space_id or space_id in seen_products:
                    continue
                seen_products.add(space_id)

                parts = space_id.split("/", 1)
                startup_or_author = parts[0] if len(parts) == 2 else "Independent"
                product_name = parts[1].replace("-", " ").replace("_", " ").title() if len(parts) == 2 else space_id

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
        except Exception as e:
            log.warning("products.page_error", page=page, error=str(e))

        if len(records) >= target_count:
            break

    out_file = settings.OUTPUT_DIR / "products.jsonl"
    with open(out_file, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r.model_dump(), ensure_ascii=False) + "\n")

    log.info("products.saved_to_disk", count=len(records), path=str(out_file))
    return records