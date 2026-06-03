import json
import re
from pathlib import Path
from typing import List

from PIL import Image, ImageDraw

from .models import Selection


def detect_selections(manifest_path: Path, screenshot_path: Path, min_confidence: float = 0.36) -> List[Selection]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with Image.open(screenshot_path) as img:
        img = img.convert("RGB")
        scale_x = manifest["page_width"] / img.width
        marker_masks = _selection_marker_bounds(img)

    selections = []
    if not marker_masks:
        return selections

    for item in manifest["products"]:
        box = item["box"]
        scaled_box = [int(box[0] / scale_x), int(box[1] / scale_x), int(box[2] / scale_x), int(box[3] / scale_x)]
        overlap = max(_overlap_area(mask, scaled_box) for mask in marker_masks)
        card_area = max((scaled_box[2] - scaled_box[0]) * (scaled_box[3] - scaled_box[1]), 1)
        confidence = min(1.0, overlap / card_area * 2.2)
        if confidence >= min_confidence:
            selections.append(Selection(item["product_id"], round(confidence, 3), "圈选或彩色标记覆盖款式卡片"))

    return sorted(selections, key=lambda item: item.confidence, reverse=True)


def detect_selection_text(manifest_path: Path, text: str) -> List[Selection]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    products = manifest.get("products", [])
    product_ids = [str(item.get("product_id", "")) for item in products if item.get("product_id")]
    selected: dict[str, Selection] = {}

    normalized_text = text.upper()
    for product_id in product_ids:
        if product_id.upper() in normalized_text:
            selected[product_id] = Selection(product_id, 1.0, "文字回传完整商品编号")

    suffix_map: dict[str, set[str]] = {}
    for product_id in product_ids:
        suffix = product_id.rsplit("-", 1)[-1]
        for key in {suffix, suffix.lstrip("0") or suffix}:
            suffix_map.setdefault(key, set()).add(product_id)

    for line in text.splitlines():
        normalized_line = line.strip()
        if not normalized_line:
            continue
        has_selection_intent = bool(re.search(r"(选|要|编号|商品|款号|款|拍|留|确认|定|拿)", normalized_line))
        if not has_selection_intent and len(normalized_line) > 12:
            continue
        for number in re.findall(r"(?<!\d)(\d{1,3})(?!\d)", normalized_line):
            candidates = suffix_map.get(number) or suffix_map.get(number.lstrip("0") or number)
            if candidates and len(candidates) == 1:
                product_id = next(iter(candidates))
                selected.setdefault(product_id, Selection(product_id, 0.92, "文字回传商品编号后缀"))

    return list(selected.values())


def make_demo_selection(report_path: Path, manifest_path: Path, output_path: Path, limit: int = 2) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with Image.open(report_path) as img:
        img = img.convert("RGB")
        draw = ImageDraw.Draw(img)
        for item in manifest["products"][:limit]:
            x1, y1, x2, y2 = item["box"]
            pad = 8
            draw.rounded_rectangle((x1 - pad, y1 - pad, x2 + pad, y2 + pad), radius=22, outline="#e31937", width=12)
        img.save(output_path, quality=94)
    return output_path


def write_selection_json(selections: List[Selection], output_path: Path) -> Path:
    payload = [selection.__dict__ for selection in selections]
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def _selection_marker_bounds(img: Image.Image):
    pixels = img.load()
    width, height = img.size
    step = 3
    points = set()
    for y in range(0, height, step):
        for x in range(0, width, step):
            r, g, b = pixels[x, y]
            if _is_marker_pixel(r, g, b):
                points.add((x, y))
    boxes = []
    while points:
        start = points.pop()
        stack = [start]
        xs = [start[0]]
        ys = [start[1]]
        while stack:
            x, y = stack.pop()
            for nx, ny in ((x + step, y), (x - step, y), (x, y + step), (x, y - step)):
                if (nx, ny) in points:
                    points.remove((nx, ny))
                    stack.append((nx, ny))
                    xs.append(nx)
                    ys.append(ny)
        if len(xs) > 20 and _looks_like_marker_component(xs, ys, step):
            boxes.append([min(xs), min(ys), max(xs), max(ys)])
    return boxes


def _is_marker_pixel(r: int, g: int, b: int) -> bool:
    bright = max(r, g, b)
    spread = bright - min(r, g, b)
    if bright < 120 or spread < 50:
        return False
    red = r > 165 and r > g * 1.25 and r > b * 1.25
    orange = r > 180 and g > 80 and b < 140 and r > b * 1.45
    yellow = r > 170 and g > 140 and b < 130 and abs(r - g) < 95
    green = g > 130 and g > r * 1.18 and g > b * 1.18
    blue = b > 130 and b > r * 1.18 and b > g * 1.08
    purple = r > 125 and b > 125 and g < 145 and spread > 60
    pink = r > 180 and b > 120 and g < 155
    return red or orange or yellow or green or blue or purple or pink


def _looks_like_marker_component(xs: List[int], ys: List[int], step: int) -> bool:
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    width = max_x - min_x + step
    height = max_y - min_y + step
    if width < 35 or height < 24:
        return False
    sampled_area = max(((width // step) + 1) * ((height // step) + 1), 1)
    fill_ratio = len(xs) / sampled_area
    edge_margin = step * 3
    edge_points = sum(
        1
        for x, y in zip(xs, ys)
        if x <= min_x + edge_margin or x >= max_x - edge_margin or y <= min_y + edge_margin or y >= max_y - edge_margin
    )
    edge_ratio = edge_points / max(len(xs), 1)
    return fill_ratio <= 0.55 and (edge_ratio >= 0.34 or fill_ratio <= 0.28)


def _overlap_area(left, right) -> int:
    x1 = max(left[0], right[0])
    y1 = max(left[1], right[1])
    x2 = min(left[2], right[2])
    y2 = min(left[3], right[3])
    if x2 <= x1 or y2 <= y1:
        return 0
    return (x2 - x1) * (y2 - y1)
