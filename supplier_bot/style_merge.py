from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image

from .models import Product


DETAIL_BACKGROUND_THRESHOLD = 0.18
DETAIL_SATURATION_THRESHOLD = 0.72
LOW_NEUTRAL_THRESHOLD = 0.22
STYLE_SIMILARITY_THRESHOLD = 0.86


@dataclass
class StyleCard:
    product: Product
    detail_products: List[Product]
    image_role: str
    suspect_products: List[Product] = field(default_factory=list)

    @property
    def image_count(self) -> int:
        return 1 + len(self.detail_products)

    @property
    def related_image_paths(self) -> List[str]:
        paths = list(self.product.related_images)
        paths.extend(detail.primary_image for detail in self.detail_products)
        for detail in self.detail_products:
            paths.extend(detail.related_images)
        return list(dict.fromkeys(paths))

    @property
    def suspect_product_ids(self) -> List[str]:
        return list(dict.fromkeys(product.product_id for product in self.suspect_products))


def build_style_cards(products: List[Product]) -> List[StyleCard]:
    """Collapse obvious detail images into nearby style cards.

    This is intentionally conservative: it only hides images that look like
    close-up/detail shots and can be attached to a recent full-product image
    from the same supplier and category. Full-product images stay visible even
    when they look similar, because those may be different styles.
    """
    ordered = sorted(products, key=lambda item: (item.supplier_id, item.received_at, item.product_id))
    roles = {product.product_id: classify_image_role(product.primary_image) for product in ordered}
    cards: List[StyleCard] = [
        StyleCard(product=product, detail_products=[], image_role=roles[product.product_id])
        for product in ordered
        if roles[product.product_id] != "detail"
    ]
    anchors_by_key: Dict[str, List[StyleCard]] = {}
    for card in cards:
        anchors_by_key.setdefault(_merge_key(card.product), []).append(card)

    for product in ordered:
        role = roles[product.product_id]
        if role != "detail":
            continue
        anchor = _nearest_anchor(product, anchors_by_key.get(_merge_key(product), []))
        if anchor is not None:
            anchor.detail_products.append(product)
            continue

        card = StyleCard(product=product, detail_products=[], image_role=role)
        cards.append(card)

    cards = _merge_similar_full_views(cards)
    return sorted(cards, key=lambda card: (card.product.category_lv1, card.product.category_lv2, card.product.supplier_id, card.product.product_id))


def classify_image_role(image_path: str) -> str:
    """Return full/detail/unknown using local visual cues.

    Detail shots usually fill the thumbnail with fabric or a close crop, while
    useful selection photos usually have visible background around the product.
    """
    try:
        with Image.open(image_path) as raw:
            img = raw.convert("RGB").resize((96, 96), Image.Resampling.LANCZOS)
    except Exception:
        return "unknown"

    pixels = list(img.getdata())
    corner_pixels = []
    for x0, y0 in [(0, 0), (84, 0), (0, 84), (84, 84)]:
        for y in range(y0, y0 + 12):
            for x in range(x0, x0 + 12):
                corner_pixels.append(img.getpixel((x, y)))
    bg = tuple(sum(pixel[i] for pixel in corner_pixels) // len(corner_pixels) for i in range(3))
    bg_like = sum(1 for pixel in pixels if _color_distance(pixel, bg) < 42) / len(pixels)
    brightness = sum(bg) / 3
    non_white_pixels = [pixel for pixel in pixels if not _is_ui_white(pixel)]
    if not non_white_pixels:
        return "unknown"
    non_white_ratio = len(non_white_pixels) / len(pixels)
    saturated_ratio = sum(1 for pixel in non_white_pixels if _saturation(pixel) > 50) / len(non_white_pixels)
    neutral_ratio = sum(1 for pixel in non_white_pixels if _saturation(pixel) < 25) / len(non_white_pixels)

    if non_white_ratio < 0.08:
        return "full"
    if saturated_ratio > DETAIL_SATURATION_THRESHOLD and neutral_ratio < LOW_NEUTRAL_THRESHOLD:
        return "detail"
    if bg_like < DETAIL_BACKGROUND_THRESHOLD and saturated_ratio > 0.58 and neutral_ratio < LOW_NEUTRAL_THRESHOLD:
        return "detail"
    if bg_like > 0.9 and brightness < 235:
        return "detail"
    if bg_like > 0.34 or neutral_ratio > 0.25:
        return "full"
    return "unknown"


def _merge_key(product: Product) -> str:
    # For detail shots, lv1 is safer than exact lv2 because close-ups of a
    # shirt may be classified as T恤/衬衫/针织衫 depending on visible texture.
    return f"{product.supplier_id}:{product.category_lv1}"


def _nearest_anchor(product: Product, anchors: List[StyleCard]) -> Optional[StyleCard]:
    if not anchors:
        return None
    return min(anchors, key=lambda card: _distance(product, card.product))


def _merge_similar_full_views(cards: List[StyleCard]) -> List[StyleCard]:
    merged: List[StyleCard] = []
    histograms: Dict[str, List[float]] = {}
    for card in sorted(cards, key=lambda item: (item.product.supplier_id, item.product.received_at, item.product.product_id)):
        anchor = _nearest_similar_card(card, merged, histograms)
        if anchor is None:
            merged.append(card)
            histograms[card.product.product_id] = _color_histogram(card.product.primary_image)
            continue

        if _presentation_score(card) > _presentation_score(anchor):
            previous_product = anchor.product
            previous_role = anchor.image_role
            previous_details = anchor.detail_products
            anchor.product = card.product
            anchor.image_role = card.image_role
            anchor.detail_products = [previous_product, *previous_details, *card.detail_products]
            histograms[anchor.product.product_id] = histograms.pop(previous_product.product_id)
            histograms[anchor.product.product_id] = _color_histogram(anchor.product.primary_image)
            continue

        anchor.detail_products.append(card.product)
        anchor.detail_products.extend(card.detail_products)
    return merged


def _nearest_similar_card(card: StyleCard, anchors: List[StyleCard], histograms: Dict[str, List[float]]) -> Optional[StyleCard]:
    matches = [
        anchor
        for anchor in anchors
        if _can_merge_as_same_style(card.product, anchor.product)
        and _has_merge_signal(card, anchor)
        and _histogram_intersection(_color_histogram(card.product.primary_image), histograms[anchor.product.product_id]) >= STYLE_SIMILARITY_THRESHOLD
    ]
    if not matches:
        return None
    return min(matches, key=lambda anchor: _distance(card.product, anchor.product))


def _can_merge_as_same_style(left: Product, right: Product) -> bool:
    if left.supplier_id != right.supplier_id:
        return False
    if left.category_lv1 != right.category_lv1:
        return False
    if left.category_lv2 == right.category_lv2:
        return True
    return left.category_lv1 in {"上衣", "下装", "连体"}


def _has_merge_signal(left: StyleCard, right: StyleCard) -> bool:
    return bool(left.detail_products and right.detail_products)


def _presentation_score(card: StyleCard) -> Tuple[int, float, str]:
    role_rank = {"full": 2, "unknown": 1, "detail": 0}.get(card.image_role, 0)
    return (role_rank, card.product.confidence, card.product.product_id)


def _distance(detail: Product, anchor: Product) -> Tuple[float, str]:
    return (abs((detail.received_at - anchor.received_at).total_seconds()), anchor.product_id)


def _color_distance(left, right) -> int:
    return sum(abs(left[i] - right[i]) for i in range(3))


def _saturation(pixel) -> int:
    return max(pixel) - min(pixel)


def _is_ui_white(pixel) -> bool:
    return pixel[0] > 245 and pixel[1] > 245 and pixel[2] > 245


def _color_histogram(image_path: str) -> List[float]:
    try:
        with Image.open(image_path) as raw:
            img = raw.convert("RGB").resize((64, 64), Image.Resampling.LANCZOS)
    except Exception:
        return [0.0] * 64

    buckets = [0] * 64
    for red, green, blue in img.getdata():
        if _is_ui_white((red, green, blue)):
            continue
        index = (red // 64) * 16 + (green // 64) * 4 + (blue // 64)
        buckets[index] += 1
    total = sum(buckets) or 1
    return [bucket / total for bucket in buckets]


def _histogram_intersection(left: List[float], right: List[float]) -> float:
    return sum(min(a, b) for a, b in zip(left, right))
