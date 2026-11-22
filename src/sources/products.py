import json
from datetime import datetime, timezone
from urllib.parse import quote

from src.core.http import HttpClient
from src.schemas import ProductRecord, ProductContent, SourceInfo
from src.config import settings
from src.logging_setup import log

async def run_products_extractor(http: HttpClient, target_count: int = 1000) -> list[ProductRecord]:
    log.info("products.fetch_start", target_count=target_count)
    records = []
    now_iso = datetime.now(timezone.utc).isoformat()
    seen_products = set()
    seen_names = set()
    if target_count <= 0:
        return records
    limit = min(max(target_count, 1), 1000)
    spaces = await http.get(
        f"https://huggingface.co/api/spaces?limit={limit}&full=true",
        as_json=True,
    )
    for space in spaces:
        space_id = space.get("id")
        if not isinstance(space_id, str) or not space_id or space_id in seen_products:
            continue
        seen_products.add(space_id)

        parts = space_id.split("/", 1)
        startup_name = parts[0] if len(parts) == 2 else "Independent"
        card_data = space.get("cardData") or {}
        if not isinstance(card_data, dict):
            card_data = {}
        product_name = card_data.get("title") or (
            parts[1].replace("-", " ").replace("_", " ").title()
            if len(parts) == 2 else space_id
        )
        if product_name in seen_names:
            product_name = f"{product_name} ({space_id})"
        suffix = 2
        unique_name = product_name
        while unique_name in seen_names:
            unique_name = f"{product_name} ({suffix})"
            suffix += 1
        product_name = unique_name
        seen_names.add(product_name)

        records.append(ProductRecord(
            schemaVersion="1.0",
            recordType="PRODUCT",
            source=SourceInfo(
                name="Hugging Face Spaces Directory",
                url=f"https://huggingface.co/spaces/{quote(space_id, safe='/')}",
            ),
            content=ProductContent(
                productName=product_name,
                startupName=startup_name,
                pricingModel=None,
            ),
            collectedAt=now_iso,
        ))
        if len(records) >= target_count:
            break

    out_file = settings.OUTPUT_DIR / "products.jsonl"
    with open(out_file, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r.model_dump(), ensure_ascii=False) + "\n")

    log.info("products.saved_to_disk", count=len(records), path=str(out_file))
    return records