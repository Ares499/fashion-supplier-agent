from datetime import datetime
from pathlib import Path
from typing import List

from .classifier import classify_image_detail_with_deadline
from .images import archive_image, phash, similar_hashes
from .models import Product
from .storage import Store


def ingest_supplier_images(store: Store, data_dir: Path, supplier_id: str, image_paths: List[Path], received_at: datetime) -> List[Product]:
    supplier = store.get_supplier(supplier_id)
    if supplier is None:
        raise ValueError(f"Unknown supplier_id: {supplier_id}")

    created = []
    day_key = received_at.strftime("%Y-%m-%d")
    date_products = store.list_products_for_date(day_key)
    same_supplier_count = sum(1 for product in date_products if product.supplier_id == supplier_id)
    next_index = same_supplier_count + 1

    for image_path in image_paths:
        archived = archive_image(image_path, data_dir, supplier_id, received_at)
        image_hash = phash(str(archived))

        matched = None
        for product in date_products + created:
            if product.supplier_id == supplier_id and similar_hashes(product.phash, image_hash):
                matched = product
                break

        if matched:
            if str(archived) not in matched.related_images and str(archived) != matched.primary_image:
                matched.related_images.append(str(archived))
                store.upsert_product(matched)
            continue

        classification = classify_image_detail_with_deadline(str(archived), supplier.main_categories)
        lv1, lv2, confidence = (
            classification.category_lv1,
            classification.category_lv2,
            classification.confidence,
        )
        product_id = f"{supplier_id}-{received_at.strftime('%y%m%d')}-{next_index:02d}"
        next_index += 1
        product = Product(
            product_id=product_id,
            supplier_id=supplier_id,
            received_at=received_at,
            primary_image=str(archived),
            related_images=[],
            category_lv1=lv1,
            category_lv2=lv2,
            phash=image_hash,
            confidence=confidence,
        )
        store.upsert_product(product)
        created.append(product)

    return created
