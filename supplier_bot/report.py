import os
import subprocess
from functools import lru_cache
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from .models import Product, Supplier
from .style_ai import build_ai_style_cards
from .style_merge import StyleCard, build_style_cards
from .storage import Store


PAGE_WIDTH = 1240
MARGIN = 48
CARD_W = 360
CARD_H = 520
GAP = 28
IMAGE_H = 360


@lru_cache(maxsize=32)
def _font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        os.getenv("REPORT_FONT_PATH", ""),
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for path in candidates:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size=size)
    try:
        matched = subprocess.run(
            ["fc-match", "-f", "%{file}", "Noto Sans CJK SC"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        if matched and Path(matched).exists():
            return ImageFont.truetype(matched, size=size)
    except Exception:
        pass
    return ImageFont.load_default()


def build_daily_report(
    store: Store,
    date: str,
    output_dir: Path,
    use_ai_style: bool = False,
    supplier_ids: Optional[Iterable[str]] = None,
) -> Tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    products = store.list_products_for_date(date)
    if supplier_ids is not None:
        allowed_supplier_ids = set(supplier_ids)
        products = [product for product in products if product.supplier_id in allowed_supplier_ids]
    suppliers = {supplier.supplier_id: supplier for supplier in store.list_suppliers(include_paused=True)}
    if not products:
        raise ValueError(f"No products found for {date}")

    cards = build_ai_style_cards(products, output_dir / "style_groups.ai.json") if use_ai_style else build_style_cards(products)
    sections = _build_report_sections(cards, suppliers)

    rows = sum((len(items) + 2) // 3 + 1 for _, items in sections)
    height = 170 + rows * (CARD_H + 28)
    canvas = Image.new("RGB", (PAGE_WIDTH, height), "#f7f7f4")
    draw = ImageDraw.Draw(canvas)
    title_font = _font(46)
    section_font = _font(30)
    body_font = _font(24)
    small_font = _font(19)

    draw.text((MARGIN, 42), f"{date} 供应商新款选款日报", fill="#171717", font=title_font)
    draw.text(
        (MARGIN, 104),
        f"共 {len(cards)} 个候选款，含 {len(products)} 张图片；截图圈选后发给机器人确认集样",
        fill="#555555",
        font=body_font,
    )

    y = 160
    manifest = {"date": date, "page_width": PAGE_WIDTH, "style_grouping": "ai" if use_ai_style else "local", "products": []}
    for section, items in sections:
        draw.rounded_rectangle((MARGIN, y, PAGE_WIDTH - MARGIN, y + 50), radius=8, fill="#242424")
        draw.text((MARGIN + 20, y + 9), section, fill="#ffffff", font=section_font)
        y += 74

        for idx, card in enumerate(items):
            product = card.product
            col = idx % 3
            if idx and col == 0:
                y += CARD_H + GAP
            x = MARGIN + col * (CARD_W + GAP)
            box = (x, y, x + CARD_W, y + CARD_H)
            _draw_card(canvas, draw, box, card, suppliers.get(product.supplier_id), body_font, small_font)
            product.report_box = [box[0], box[1], box[2], box[3]]
            store.upsert_product(product)
            manifest["products"].append(
                {
                    "product_id": product.product_id,
                    "supplier_id": product.supplier_id,
                    "supplier_name": suppliers.get(product.supplier_id).name if suppliers.get(product.supplier_id) else product.supplier_id,
                    "category": f"{product.category_lv1}/{product.category_lv2}",
                    "confidence": product.confidence,
                    "needs_review": product.confidence < 0.7,
                    "image_count": card.image_count,
                    "related_images": card.related_image_paths,
                    "hidden_product_ids": [detail.product_id for detail in card.detail_products],
                    "suspected_duplicate_ids": card.suspect_product_ids,
                    "report_section": section,
                    "box": product.report_box,
                    "primary_image": product.primary_image,
                }
            )
        y += CARD_H + GAP

    report_png = output_dir / "report.png"
    report_pdf = output_dir / "report.pdf"
    manifest_path = output_dir / "manifest.json"
    canvas.crop((0, 0, PAGE_WIDTH, min(y + 24, height))).save(report_png, quality=94)
    Image.open(report_png).convert("RGB").save(report_pdf, "PDF", resolution=160)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_png, report_pdf, manifest_path


def _draw_card(canvas: Image.Image, draw: ImageDraw.ImageDraw, box, card: StyleCard, supplier: Supplier, body_font, small_font) -> None:
    product = card.product
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=8, fill="#ffffff", outline="#dddddd", width=2)
    id_text = product.product_id
    id_font = body_font
    id_bbox = draw.textbbox((0, 0), id_text, font=id_font)
    if id_bbox[2] - id_bbox[0] > CARD_W - 56:
        id_font = small_font
        id_bbox = draw.textbbox((0, 0), id_text, font=id_font)
    id_w = min(id_bbox[2] - id_bbox[0] + 24, CARD_W - 32)
    draw.rounded_rectangle((x1 + 16, y1 + 16, x1 + 16 + id_w, y1 + 56), radius=6, fill="#111111")
    draw.text((x1 + 28, y1 + 23), id_text, fill="#ffffff", font=id_font)

    with Image.open(product.primary_image) as raw:
        raw = raw.convert("RGB")
        raw = _fit_product_image(raw, CARD_W - 32, IMAGE_H)
        image_x = x1 + (CARD_W - raw.width) // 2
        image_y = y1 + 72 + (IMAGE_H - raw.height) // 2
        canvas.paste(raw, (image_x, image_y))

    supplier_name = supplier.name if supplier else product.supplier_id
    draw.text((x1 + 18, y1 + 448), supplier_name[:16], fill="#222222", font=body_font)
    review = "  需复核" if product.confidence < 0.7 else ""
    image_count = f"  含{card.image_count}图" if card.image_count > 1 else ""
    suspect_count = ""
    if card.suspect_product_ids:
        suspect_count = f"  疑似:{','.join(_short_id(item) for item in card.suspect_product_ids[:3])}"
    draw.text(
        (x1 + 18, y1 + 482),
        f"{product.category_lv1}/{product.category_lv2}  {product.received_at.strftime('%H:%M')}{image_count}{suspect_count}{review}",
        fill="#666666",
        font=small_font,
    )


def _fit_product_image(raw: Image.Image, max_width: int, max_height: int) -> Image.Image:
    scale = min(max_width / raw.width, max_height / raw.height)
    if scale >= 1:
        new_size = (max(1, int(raw.width * scale)), max(1, int(raw.height * scale)))
        return raw.resize(new_size, Image.Resampling.BICUBIC)
    copy = raw.copy()
    copy.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    return copy


def _build_report_sections(cards: List[StyleCard], suppliers: Dict[str, Supplier]) -> List[Tuple[str, List[StyleCard]]]:
    suspect_components = _suspect_components(cards)
    suspect_ids = {card.product.product_id for component in suspect_components for card in component}
    section_order = ["上衣", "下装", "连体", "鞋履", "箱包", "配饰", "其他"]
    normal_groups: Dict[str, List[StyleCard]] = defaultdict(list)
    suspect_groups: Dict[str, Dict[str, List[List[StyleCard]]]] = defaultdict(lambda: defaultdict(list))

    for card in cards:
        if card.product.product_id in suspect_ids:
            continue
        product = card.product
        normal_groups[product.category_lv1].append(card)

    for component in suspect_components:
        section = _component_section(component)
        suspect_groups[section][_component_subcategory(component)].append(component)

    sections: List[Tuple[str, List[StyleCard]]] = []
    all_sections = set(normal_groups) | set(suspect_groups)
    ordered_sections = [item for item in section_order if item in all_sections]
    ordered_sections.extend(sorted(all_sections - set(ordered_sections)))
    for section in ordered_sections:
        items = _merge_section_items(normal_groups.get(section, []), suspect_groups.get(section, {}))
        if items:
            sections.append((section, items))
    return sections


def _component_section(component: List[StyleCard]) -> str:
    counts = defaultdict(int)
    for card in component:
        counts[card.product.category_lv1] += 1
    return min(counts, key=lambda section: (-counts[section], section))


def _component_subcategory(component: List[StyleCard]) -> str:
    counts = defaultdict(int)
    for card in component:
        counts[card.product.category_lv2] += 1
    return min(counts, key=lambda section: (-counts[section], section))


def _merge_section_items(normal_cards: List[StyleCard], suspect_groups: Dict[str, List[List[StyleCard]]]) -> List[StyleCard]:
    by_subcategory: Dict[str, List[StyleCard]] = defaultdict(list)
    for card in normal_cards:
        by_subcategory[card.product.category_lv2].append(card)

    items: List[StyleCard] = []
    subcategories = sorted(set(by_subcategory) | set(suspect_groups))
    for subcategory in subcategories:
        items.extend(sorted(by_subcategory.get(subcategory, []), key=lambda card: (card.product.supplier_id, card.product.product_id)))
        for component in suspect_groups.get(subcategory, []):
            items.extend(sorted(component, key=lambda card: (card.product.supplier_id, card.product.product_id)))
    return items


def _suspect_components(cards: List[StyleCard]) -> List[List[StyleCard]]:
    card_by_id = {card.product.product_id: card for card in cards}
    graph = defaultdict(set)
    for card in cards:
        for suspect_id in card.suspect_product_ids:
            if suspect_id not in card_by_id:
                continue
            graph[card.product.product_id].add(suspect_id)
            graph[suspect_id].add(card.product.product_id)

    components = []
    seen = set()
    for product_id in sorted(graph):
        if product_id in seen:
            continue
        stack = [product_id]
        component_ids = []
        seen.add(product_id)
        while stack:
            current = stack.pop()
            component_ids.append(current)
            for next_id in graph[current]:
                if next_id in seen:
                    continue
                seen.add(next_id)
                stack.append(next_id)
        if len(component_ids) > 1:
            components.append([card_by_id[item] for item in sorted(component_ids)])
    return components


def _short_id(product_id: str) -> str:
    return product_id.rsplit("-", 1)[-1]
