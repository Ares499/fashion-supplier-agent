import csv
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .models import Product, ProductStatus, Supplier
from .storage import Store


DEFAULT_SIZES_BY_CATEGORY = {
    "上衣": ["S", "M", "L"],
    "下装": ["S", "M", "L"],
    "连体": ["S", "M", "L"],
    "鞋履": ["35", "36", "37", "38", "39"],
    "箱包": ["均码"],
    "配饰": ["均码"],
    "其他": ["均码"],
}


@dataclass
class DouyinSkuDraft:
    color: str
    size: str
    price_cents: int
    stock: int
    stock_source: str = ""


@dataclass
class DouyinListingDraft:
    product_id: str
    supplier_id: str
    supplier_name: str
    title: str
    category: str
    douyin_category_id: str
    images: List[str]
    skus: List[DouyinSkuDraft]
    freight_template_id: str = ""
    brand: str = ""
    product_format: str = "normal"
    source_status: str = ""
    external_code: str = ""
    factory_code: str = ""
    cost_cents: int = 0
    shop_name: str = ""
    material: str = ""
    size_chart: str = ""
    selling_points: str = ""
    remark: str = ""
    raw_stock: str = ""
    category_confidence: float = 0.0
    ready_to_publish: bool = False
    missing_fields: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass
class ListingDefaults:
    default_price_cents: int = 9900
    default_stock: int = 0
    default_color: str = "默认色"
    freight_template_id: str = ""
    brand: str = ""
    title_prefix: str = ""
    title_suffix: str = ""
    category_ids: Dict[str, str] = field(default_factory=dict)
    sizes_by_category: Dict[str, List[str]] = field(default_factory=lambda: dict(DEFAULT_SIZES_BY_CATEGORY))


CSV_COLUMNS = [
    "上架时间",
    "商品名字",
    "供应商/工厂",
    "工厂编码",
    "产品图",
    "线上编码",
    "成本",
    "售价",
    "库存",
    "尺码表",
    "面料成分",
    "产品卖点",
    "店铺",
    "类目",
    "备注",
    "商品名字(线上)",
]

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
LETTER_SIZES = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "2XL", "3XL", "4XL"]

KEYWORD_CATEGORIES = [
    ("鞋履", "单鞋", ["鞋", "靴", "拖", "凉鞋", "单鞋", "分趾鞋", "人字拖"]),
    ("箱包", "包", ["包", "托特", "水桶包"]),
    ("连体", "连衣裙", ["连衣裙", "背心裙", "吊带裙"]),
    ("下装", "半身裙", ["半身裙", "鱼尾裙", "长裙"]),
    ("下装", "裤子", ["裤", "牛仔裤", "休闲裤", "工装裤", "短裤", "瑜伽"]),
    ("上衣", "衬衫", ["衬衫", "衬衣"]),
    ("上衣", "外套", ["外套", "风衣", "防晒", "罩衫", "夹克"]),
    ("上衣", "T恤", ["T恤", "T桖", "短袖", "圆领T", "城市T"]),
    ("上衣", "针织衫", ["针织", "开衫", "马甲", "坎肩"]),
    ("上衣", "背心", ["背心", "吊带"]),
]


def load_listing_defaults(path: Optional[Path]) -> ListingDefaults:
    if path is None or not path.exists():
        return ListingDefaults()
    payload = json.loads(path.read_text(encoding="utf-8"))
    defaults = ListingDefaults()
    for key, value in payload.items():
        if hasattr(defaults, key):
            setattr(defaults, key, value)
    defaults.default_price_cents = int(defaults.default_price_cents)
    defaults.default_stock = int(defaults.default_stock)
    return defaults


def load_selection_product_ids(path: Path) -> List[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("selection file must contain a list")
    product_ids = []
    for item in payload:
        if isinstance(item, dict) and item.get("product_id"):
            product_ids.append(str(item["product_id"]))
        elif isinstance(item, str):
            product_ids.append(item)
    return _dedupe(product_ids)


def load_csv_listing_rows(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        rows = []
        for row in csv.DictReader(fp):
            item = {column: (row.get(column) or "").strip() for column in CSV_COLUMNS}
            if any(item.values()):
                rows.append(item)
        return rows


def build_listing_drafts_from_csv(
    path: Path,
    image_dir: Optional[Path],
    defaults: ListingDefaults,
    product_name: str = "",
    external_code: str = "",
) -> List[DouyinListingDraft]:
    image_index = _build_image_index(image_dir) if image_dir else []
    drafts = []
    for row in load_csv_listing_rows(path):
        if product_name and row["商品名字"] != product_name and product_name not in row["商品名字(线上)"]:
            continue
        if external_code and row["线上编码"] != external_code:
            continue
        drafts.append(_build_listing_draft_from_csv_row(row, image_index, defaults))
    return drafts


def choose_products(store: Store, date: Optional[str], product_ids: Sequence[str]) -> List[Product]:
    if product_ids:
        products = []
        missing = []
        for product_id in _dedupe(product_ids):
            product = store.get_product(product_id)
            if product is None:
                missing.append(product_id)
                continue
            products.append(product)
        if missing:
            raise ValueError(f"Unknown product_id(s): {', '.join(missing)}")
        return products

    if not date:
        raise ValueError("Either --date, --selection, or --product-id is required")
    return store.list_products_for_date(date)


def build_listing_drafts(store: Store, products: Iterable[Product], defaults: ListingDefaults) -> List[DouyinListingDraft]:
    suppliers = {supplier.supplier_id: supplier for supplier in store.list_suppliers(include_paused=True)}
    return [_build_listing_draft(product, suppliers.get(product.supplier_id), defaults) for product in products]


def write_listing_outputs(drafts: Sequence[DouyinListingDraft], output_dir: Path) -> Tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "douyin_listing_drafts.json"
    csv_path = output_dir / "douyin_listing_drafts.csv"
    json_path.write_text(
        json.dumps([_draft_to_dict(draft) for draft in drafts], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "product_id",
                "supplier_name",
                "external_code",
                "factory_code",
                "title",
                "category",
                "douyin_category_id",
                "cost_yuan",
                "price_yuan",
                "stock",
                "shop_name",
                "freight_template_id",
                "ready_to_publish",
                "missing_fields",
                "warnings",
                "images",
                "raw_stock",
            ],
        )
        writer.writeheader()
        for draft in drafts:
            writer.writerow(
                {
                    "product_id": draft.product_id,
                    "supplier_name": draft.supplier_name,
                    "external_code": draft.external_code,
                    "factory_code": draft.factory_code,
                    "title": draft.title,
                    "category": draft.category,
                    "douyin_category_id": draft.douyin_category_id,
                    "cost_yuan": f"{draft.cost_cents / 100:.2f}" if draft.cost_cents else "",
                    "price_yuan": f"{draft.skus[0].price_cents / 100:.2f}" if draft.skus else "",
                    "stock": sum(sku.stock for sku in draft.skus),
                    "shop_name": draft.shop_name,
                    "freight_template_id": draft.freight_template_id,
                    "ready_to_publish": "是" if draft.ready_to_publish else "否",
                    "missing_fields": "；".join(draft.missing_fields),
                    "warnings": "；".join(draft.warnings),
                    "images": "；".join(draft.images),
                    "raw_stock": draft.raw_stock,
                }
            )
    return json_path, csv_path


def mark_drafted_products(store: Store, drafts: Iterable[DouyinListingDraft]) -> None:
    store.update_status([draft.product_id for draft in drafts], ProductStatus.LISTING_DRAFTED)


def _build_listing_draft(product: Product, supplier: Optional[Supplier], defaults: ListingDefaults) -> DouyinListingDraft:
    category = f"{product.category_lv1}/{product.category_lv2}"
    category_id = defaults.category_ids.get(category) or defaults.category_ids.get(product.category_lv1, "")
    images = [product.primary_image, *product.related_images]
    skus = [
        DouyinSkuDraft(
            color=defaults.default_color,
            size=size,
            price_cents=defaults.default_price_cents,
            stock=defaults.default_stock,
        )
        for size in defaults.sizes_by_category.get(product.category_lv1, defaults.sizes_by_category.get("其他", ["均码"]))
    ]
    draft = DouyinListingDraft(
        product_id=product.product_id,
        supplier_id=product.supplier_id,
        supplier_name=supplier.name if supplier else product.supplier_id,
        title=_build_title(product, supplier, defaults),
        category=category,
        douyin_category_id=str(category_id),
        images=images,
        skus=skus,
        freight_template_id=defaults.freight_template_id,
        brand=defaults.brand,
        source_status=product.status.value,
    )
    _validate_draft(draft)
    return draft


def _build_listing_draft_from_csv_row(
    row: Dict[str, str],
    image_index: Sequence[Path],
    defaults: ListingDefaults,
) -> DouyinListingDraft:
    online_title = row["商品名字(线上)"]
    base_name = row["商品名字"]
    title = online_title or _fallback_online_title(base_name, row["线上编码"])
    category_lv1, category_lv2, confidence = infer_category(title, row["线上编码"])
    category = f"{category_lv1}/{category_lv2}"
    category_id = defaults.category_ids.get(category) or defaults.category_ids.get(category_lv1, "")
    price_cents = _money_to_cents(row["售价"]) or defaults.default_price_cents
    cost_cents = _money_to_cents(row["成本"]) or 0
    skus, sku_warnings = parse_stock_skus(row["库存"], category_lv1, price_cents, defaults)
    images, image_warnings = match_listing_images(image_index, row["线上编码"], row["工厂编码"], base_name)
    draft = DouyinListingDraft(
        product_id=row["线上编码"] or row["工厂编码"] or base_name,
        supplier_id=row["供应商/工厂"],
        supplier_name=row["供应商/工厂"],
        title=title,
        category=category,
        douyin_category_id=str(category_id),
        images=[str(image) for image in images],
        skus=skus,
        freight_template_id=defaults.freight_template_id,
        brand=defaults.brand,
        source_status="CSV导入",
        external_code=row["线上编码"],
        factory_code=row["工厂编码"],
        cost_cents=cost_cents,
        shop_name=row["店铺"],
        material=row["面料成分"],
        size_chart=row["尺码表"],
        selling_points=row["产品卖点"],
        remark=row["备注"],
        raw_stock=row["库存"],
        category_confidence=confidence,
    )
    draft.warnings.extend(sku_warnings + image_warnings)
    if not online_title:
        draft.warnings.append("商品名字(线上)为空，已按商品名字生成标题，需复核")
    if confidence < 0.7:
        draft.warnings.append("类目推断置信度低，需复核")
    _validate_draft(draft)
    return draft


def _build_title(product: Product, supplier: Optional[Supplier], defaults: ListingDefaults) -> str:
    supplier_name = supplier.name if supplier else product.supplier_id
    parts = [
        defaults.title_prefix.strip(),
        "新款",
        product.category_lv2,
        supplier_name[:8],
        product.product_id.rsplit("-", 1)[-1],
        defaults.title_suffix.strip(),
    ]
    return " ".join(part for part in parts if part)[:60]


def _validate_draft(draft: DouyinListingDraft) -> None:
    missing = list(draft.missing_fields)
    warnings = list(draft.warnings)
    if not draft.title:
        missing.append("商品标题")
    if not draft.douyin_category_id:
        missing.append("抖店类目ID")
    if not draft.freight_template_id:
        missing.append("运费模板ID")
    if not draft.images:
        missing.append("商品图片")
    for image in draft.images:
        if not Path(image).exists():
            warnings.append(f"图片不存在: {image}")
    if not draft.skus:
        missing.append("SKU")
    elif any(sku.price_cents <= 0 for sku in draft.skus):
        missing.append("SKU价格")
    elif any(sku.stock < 0 for sku in draft.skus):
        missing.append("SKU库存")

    draft.missing_fields = missing
    draft.warnings = _dedupe(warnings)
    draft.ready_to_publish = not missing and not warnings


def _draft_to_dict(draft: DouyinListingDraft) -> dict:
    payload = asdict(draft)
    payload["skus"] = [asdict(sku) for sku in draft.skus]
    return payload


def _dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def infer_category(title: str, code: str = "") -> Tuple[str, str, float]:
    normalized = title.upper()
    for lv1, lv2, keywords in KEYWORD_CATEGORIES:
        if any(keyword.upper() in normalized for keyword in keywords):
            return lv1, lv2, 0.9
    prefix_match = re.match(r"[A-Za-z]+", code or "")
    prefix = prefix_match.group(0).upper() if prefix_match else ""
    if prefix == "KZ":
        return "下装", "裤子", 0.65
    if prefix == "QZ":
        return "下装", "半身裙", 0.65
    if prefix == "LYQ":
        return "连体", "连衣裙", 0.65
    if prefix in {"WT", "SY"}:
        return "上衣", "其他上衣", 0.55
    if prefix == "XZ":
        return "鞋履", "单鞋", 0.6
    return "其他", "待分类", 0.0


def parse_stock_skus(
    stock_text: str,
    category_lv1: str,
    price_cents: int,
    defaults: ListingDefaults,
) -> Tuple[List[DouyinSkuDraft], List[str]]:
    raw = (stock_text or "").strip()
    if not raw:
        return _default_skus(category_lv1, price_cents, defaults, "库存为空"), ["库存为空，已使用默认尺码和默认库存"]

    normalized = raw.replace("－", "-").replace("—", "-").replace("，", " ").replace(",", " ")
    skus = _parse_color_letter_stock(normalized, price_cents)
    if skus:
        return skus, []
    skus = _parse_numeric_stock(normalized, price_cents)
    if skus:
        return skus, []

    shoe_range = re.search(r"\b(3[0-9]|4[0-9])\s*-\s*(3[0-9]|4[0-9])\b", normalized)
    if shoe_range:
        start, end = int(shoe_range.group(1)), int(shoe_range.group(2))
        if start <= end and end - start <= 12:
            return [
                DouyinSkuDraft(defaults.default_color, str(size), price_cents, defaults.default_stock, raw)
                for size in range(start, end + 1)
            ], [f"库存未给数量，已按鞋码{start}-{end}使用默认库存"]

    bare_sizes = _parse_bare_sizes(normalized)
    if bare_sizes:
        return [
            DouyinSkuDraft(defaults.default_color, size, price_cents, defaults.default_stock, raw)
            for size in bare_sizes
        ], ["库存只给尺码未给数量，已使用默认库存"]

    return _default_skus(category_lv1, price_cents, defaults, raw), [f"库存无法解析，已保留原始库存并使用默认SKU: {raw}"]


def match_listing_images(
    image_index: Sequence[Path],
    external_code: str,
    factory_code: str,
    product_name: str,
) -> Tuple[List[Path], List[str]]:
    if not image_index:
        return [], ["未提供图片目录或目录中没有图片"]
    for label, needle in [("线上编码", external_code), ("工厂编码", factory_code), ("商品名字", product_name)]:
        if not needle:
            continue
        matches = [path for path in image_index if _path_contains_token(path, needle)]
        if matches:
            warning = []
            if label != "线上编码":
                warning.append(f"图片按{label}匹配，需复核")
            return sorted(matches), warning
    return [], ["未匹配到商品图片"]


def _path_contains_token(path: Path, token: str) -> bool:
    lowered = token.lower()
    return any(lowered in part.lower() for part in [path.stem, *path.parts])


def _build_image_index(image_dir: Optional[Path]) -> List[Path]:
    if image_dir is None or not image_dir.exists():
        return []
    return sorted(path for path in image_dir.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS)


def _fallback_online_title(product_name: str, code: str) -> str:
    lv1, lv2, _ = infer_category(product_name, code)
    category_word = lv2 if lv2 != "待分类" else lv1
    return f"XUE【{product_name}】 {category_word}".strip()


def _money_to_cents(value: str) -> int:
    if not value:
        return 0
    match = re.search(r"\d+(?:\.\d+)?", value)
    return int(round(float(match.group(0)) * 100)) if match else 0


def _parse_color_letter_stock(text: str, price_cents: int) -> List[DouyinSkuDraft]:
    skus = []
    token_re = re.compile(r"([^\s\dA-Za-z/-]*)(XXXL|XXL|XL|XS|S|M|L|2XL|3XL|4XL|F)\s*[/:-]?\s*(\d+)", re.IGNORECASE)
    current_color = "默认色"
    for match in token_re.finditer(text):
        color, size, stock = match.groups()
        if color:
            current_color = color.strip()
        skus.append(DouyinSkuDraft(current_color or "默认色", size.upper(), price_cents, int(stock), text))
    return _dedupe_skus(skus)


def _parse_numeric_stock(text: str, price_cents: int) -> List[DouyinSkuDraft]:
    skus = []
    for size, stock in re.findall(r"\b(2[0-9]|3[0-9]|4[0-9])\s*/\s*(\d+)\b", text):
        skus.append(DouyinSkuDraft("默认色", size, price_cents, int(stock), text))
    return _dedupe_skus(skus)


def _parse_bare_sizes(text: str) -> List[str]:
    found = []
    for size in LETTER_SIZES:
        if re.search(rf"(?<![A-Za-z]){re.escape(size)}(?![A-Za-z0-9])", text, re.IGNORECASE):
            found.append(size.upper())
    if found:
        return found
    range_match = re.search(r"\b(2[0-9]|3[0-9]|4[0-9])\s*-\s*(2[0-9]|3[0-9]|4[0-9])\b", text)
    if range_match:
        start, end = int(range_match.group(1)), int(range_match.group(2))
        if start <= end and end - start <= 20:
            return [str(size) for size in range(start, end + 1)]
    return []


def _default_skus(category_lv1: str, price_cents: int, defaults: ListingDefaults, stock_source: str) -> List[DouyinSkuDraft]:
    sizes = defaults.sizes_by_category.get(category_lv1, defaults.sizes_by_category.get("其他", ["均码"]))
    return [
        DouyinSkuDraft(defaults.default_color, size, price_cents, defaults.default_stock, stock_source)
        for size in sizes
    ]


def _dedupe_skus(skus: Iterable[DouyinSkuDraft]) -> List[DouyinSkuDraft]:
    merged: Dict[Tuple[str, str], DouyinSkuDraft] = {}
    for sku in skus:
        key = (sku.color, sku.size)
        if key in merged:
            merged[key].stock += sku.stock
        else:
            merged[key] = sku
    return list(merged.values())
