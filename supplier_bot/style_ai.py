import base64
import json
import mimetypes
import multiprocessing
import os
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

import requests

from .config import load_env_file
from .models import Product
from .style_merge import StyleCard, build_style_cards, classify_image_role


CACHE_VERSION = 8
SUSPECT_STYLE_SIMILARITY_THRESHOLD = 0.86
SUSPECT_GROUP_SIMILARITY_THRESHOLD = 0.72


def build_ai_style_cards(products: List[Product], cache_path: Path) -> List[StyleCard]:
    """Group supplier images into styles with a vision model, falling back locally.

    The AI result only changes the report surface: every original Product stays
    in storage and non-representative images are kept as hidden/related images.
    """
    load_env_file()
    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        return build_style_cards(products)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cached = _read_cache(cache_path)
    cache_key = _cache_key(products)
    if cached.get("version") == CACHE_VERSION and cached.get("cache_key") == cache_key and cached.get("groups"):
        suspect_groups = _normalize_suspect_groups(products, cached.get("suspect_groups", []))
        return _cards_from_groups(products, _merge_model_groups(cached["groups"]), suspect_groups)

    groups = []
    suspect_groups = []
    for supplier_id, supplier_products in _by_supplier(products).items():
        try:
            grouped = _group_supplier_with_deadline(supplier_id, supplier_products, api_key)
            groups.extend(grouped.get("groups", []))
            suspect_groups.extend(grouped.get("suspect_groups", []))
        except Exception as exc:
            groups.extend(
                {
                    "supplier_id": product.supplier_id,
                    "style_id": product.product_id,
                    "representative_product_id": product.product_id,
                    "product_ids": [product.product_id],
                    "reason": f"AI分组失败，保留独立款：{exc.__class__.__name__}",
                }
                for product in supplier_products
            )

    normalized_groups, split_suspects = _normalize_groups(products, _merge_model_groups(groups))
    normalized_suspects = _normalize_suspect_groups(products, [*suspect_groups, *split_suspects])
    payload = {"version": CACHE_VERSION, "cache_key": cache_key, "groups": normalized_groups, "suspect_groups": normalized_suspects}
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return _cards_from_groups(products, payload["groups"], payload["suspect_groups"])


def _group_supplier_with_deadline(supplier_id: str, products: List[Product], api_key: str) -> Dict[str, List[Dict]]:
    timeout = int(os.getenv("STYLE_GROUP_DEADLINE_SECONDS", "35") or "35")
    if timeout <= 0 or os.getenv("STYLE_CHILD_PROCESS") == "1":
        return _group_supplier_with_gemini(supplier_id, products, api_key)

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue(maxsize=1)
    process = ctx.Process(target=_group_supplier_worker, args=(supplier_id, products, api_key, queue))
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(5)
        raise TimeoutError(f"style_group_timeout={timeout}s")
    if process.exitcode != 0:
        raise RuntimeError(f"style_group_child_exit={process.exitcode}")
    if queue.empty():
        raise RuntimeError("style_group_child_empty_result")
    status, payload = queue.get()
    if status == "ok":
        return payload
    raise RuntimeError(str(payload))


def _group_supplier_worker(supplier_id: str, products: List[Product], api_key: str, queue) -> None:
    os.environ["STYLE_CHILD_PROCESS"] = "1"
    try:
        queue.put(("ok", _group_supplier_with_gemini(supplier_id, products, api_key)))
    except Exception as exc:  # pragma: no cover - parent handles fallback
        queue.put(("error", f"{exc.__class__.__name__}: {exc}"))


def _group_supplier_with_gemini(supplier_id: str, products: List[Product], api_key: str) -> Dict[str, List[Dict]]:
    parts = [{"text": _grouping_prompt(supplier_id, products)}]
    for product in products:
        path = Path(product.primary_image)
        mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        parts.append({"text": f"product_id: {product.product_id}"})
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(path.read_bytes()).decode("ascii")}})

    model = os.getenv("GOOGLE_STYLE_MODEL") or os.getenv("GOOGLE_VISION_MODEL", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    response = requests.post(
        url,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json={
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json"},
        },
        timeout=_style_request_timeout(),
        proxies=_google_api_proxies(),
    )
    response.raise_for_status()
    text = _extract_gemini_text(response.json())
    parsed = json.loads(text)
    if isinstance(parsed, dict):
        groups = list(parsed.get("groups", []))
        suspect_groups = list(parsed.get("suspect_groups", []))
    else:
        groups = list(parsed)
        suspect_groups = []
    for group in groups:
        group["supplier_id"] = supplier_id
    for group in suspect_groups:
        group["supplier_id"] = supplier_id
    return {"groups": groups, "suspect_groups": suspect_groups}


def _grouping_prompt(supplier_id: str, products: List[Product]) -> str:
    rows = "\n".join(f"- {product.product_id}: {product.category_lv1}/{product.category_lv2}" for product in products)
    return (
        "你是服装电商选款报表的款式去重员。下面是一位供应商同一批发来的商品图。"
        "请按“真实商品款式”分组：同一件商品的全貌、近景、局部细节、不同角度、视频封面可以放同组；"
        "颜色、版型、图案、品类不同，或者只是很相似但无法确定同款时，必须分开。"
        "真实落地时宁愿多展示也不要漏款：只有一个清晰全貌图搭配同款近景/局部细节图时才合并；"
        "两个都像完整商品图时，即使很相似，也放入不同 groups，并可在 suspect_groups 标记疑似重复。"
        "每组选择 representative_product_id，优先选择完整展示单件商品、适合选款人快速判断的图；"
        "不要选面料纹理、领口/纽扣/鞋面局部、模糊视频封面作为代表图，除非该组只有这一张。"
        "groups 只放确定同款；suspect_groups 放疑似同款但不确定、需要选款人知道可能重复的图片。"
        "所有 product_id 在 groups 中必须且只能出现一次；suspect_groups 可以引用 groups 里的 product_id。"
        "只输出 JSON：{\"groups\":[{\"style_id\":\"S1\",\"representative_product_id\":\"...\",\"product_ids\":[\"...\"],\"reason\":\"...\"}],"
        "\"suspect_groups\":[{\"product_ids\":[\"...\"],\"reason\":\"...\"}]}。"
        f"供应商：{supplier_id}。待分组图片：\n{rows}"
    )


def _cards_from_groups(products: List[Product], groups: List[Dict], suspect_groups: List[Dict]) -> List[StyleCard]:
    product_by_id = {product.product_id: product for product in products}
    cards = []
    used = set()
    card_by_product_id = {}
    for group in groups:
        product_ids = [product_id for product_id in group.get("product_ids", []) if product_id in product_by_id]
        representative_id = group.get("representative_product_id")
        if representative_id not in product_ids:
            representative_id = product_ids[0] if product_ids else None
        if representative_id is None or representative_id in used:
            continue
        details = [product_by_id[product_id] for product_id in product_ids if product_id != representative_id and product_id not in used]
        card = StyleCard(product_by_id[representative_id], details, "ai_representative")
        cards.append(card)
        used.add(representative_id)
        used.update(product.product_id for product in details)
        card_by_product_id[representative_id] = card
        for detail in details:
            card_by_product_id[detail.product_id] = card

    for product in products:
        if product.product_id not in used:
            card = StyleCard(product, [], "ai_unmatched")
            cards.append(card)
            card_by_product_id[product.product_id] = card
    _attach_suspects(cards, card_by_product_id, suspect_groups)
    return sorted(cards, key=lambda card: (card.product.category_lv1, card.product.category_lv2, card.product.supplier_id, card.product.product_id))


def _normalize_groups(products: List[Product], groups: List[Dict]) -> tuple[List[Dict], List[Dict]]:
    expected = {product.product_id for product in products}
    product_by_id = {product.product_id: product for product in products}
    normalized = []
    suspect_groups = []
    seen = set()
    for group in groups:
        raw_ids = [str(product_id) for product_id in group.get("product_ids", []) if str(product_id) in expected and str(product_id) not in seen]
        raw_representative = str(group.get("representative_product_id", raw_ids[0] if raw_ids else ""))
        split_groups = _split_group_safely(raw_ids, raw_representative, product_by_id)
        if len(split_groups) > 1:
            suspect_groups.append({"product_ids": raw_ids, "reason": "AI认为可能同款，但后处理因保守去重规则拆开为疑似同款"})
        for product_ids in split_groups:
            representative = _best_group_representative(product_ids, raw_representative, product_by_id)
            seen.update(product_ids)
            normalized.append(
                {
                    "style_id": str(group.get("style_id", representative)),
                    "representative_product_id": representative,
                    "product_ids": product_ids,
                    "reason": str(group.get("reason", "")),
                }
            )
    for missing in sorted(expected - seen):
        normalized.append({"style_id": missing, "representative_product_id": missing, "product_ids": [missing], "reason": "AI结果未覆盖，保留为独立款"})
    return normalized, suspect_groups


def _merge_model_groups(groups: List[Dict]) -> List[Dict]:
    merged: Dict[tuple[str, str], Dict] = {}
    passthrough = []
    for group in groups:
        supplier_id = str(group.get("supplier_id", "")) or _infer_supplier_id(group)
        style_id = str(group.get("style_id", ""))
        if not supplier_id or not style_id:
            passthrough.append(group)
            continue
        key = (supplier_id, style_id)
        if key not in merged:
            merged[key] = dict(group)
            merged[key]["product_ids"] = list(group.get("product_ids", []))
            continue
        existing = merged[key]
        existing_ids = list(existing.get("product_ids", []))
        for product_id in group.get("product_ids", []):
            if product_id not in existing_ids:
                existing_ids.append(product_id)
        existing["product_ids"] = existing_ids
        existing["reason"] = "; ".join(filter(None, [str(existing.get("reason", "")), str(group.get("reason", ""))]))
    return [*merged.values(), *passthrough]


def _infer_supplier_id(group: Dict) -> str:
    product_ids = list(group.get("product_ids", []))
    if not product_ids:
        return ""
    return str(product_ids[0]).rsplit("-", 1)[0]


def _normalize_suspect_groups(products: List[Product], groups: List[Dict]) -> List[Dict]:
    expected = {product.product_id for product in products}
    product_by_id = {product.product_id: product for product in products}
    normalized = []
    seen_keys = set()
    for group in groups:
        product_ids = sorted({str(product_id) for product_id in group.get("product_ids", []) if str(product_id) in expected})
        if len(product_ids) < 2:
            continue
        if not _is_same_major_suspect_group(product_ids, product_by_id):
            continue
        key = tuple(product_ids)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        normalized.append({"product_ids": product_ids, "reason": str(group.get("reason", ""))})
    return normalized


def _is_same_major_suspect_group(product_ids: List[str], product_by_id: Dict[str, Product]) -> bool:
    majors = {product_by_id[product_id].category_lv1 for product_id in product_ids if product_by_id[product_id].category_lv1 != "其他"}
    return len(majors) <= 1


def _split_group_safely(product_ids: List[str], representative_id: str, product_by_id: Dict[str, Product]) -> List[List[str]]:
    if not product_ids:
        return []
    if representative_id not in product_ids:
        representative_id = product_ids[0]
    candidate_roles = {product_id: classify_image_role(product_by_id[product_id].primary_image) for product_id in product_ids}
    non_detail_ids = [product_id for product_id in product_ids if candidate_roles[product_id] != "detail"]
    if len(non_detail_ids) != 1:
        return [[product_id] for product_id in product_ids]

    representative_id = non_detail_ids[0]
    representative = product_by_id[representative_id]
    main_group = [representative_id]
    remaining = []
    for product_id in product_ids:
        if product_id == representative_id:
            continue
        product = product_by_id[product_id]
        clear_detail_same_major = product.supplier_id == representative.supplier_id and product.category_lv1 == representative.category_lv1
        if clear_detail_same_major and candidate_roles[product_id] == "detail":
            main_group.append(product_id)
        else:
            remaining.append(product_id)

    grouped = defaultdict(list)
    for product_id in remaining:
        product = product_by_id[product_id]
        grouped[(product.supplier_id, product.category_lv1, product.category_lv2)].append(product_id)
    return [main_group, *grouped.values()] if main_group else list(grouped.values())


def _best_group_representative(product_ids: List[str], preferred_id: str, product_by_id: Dict[str, Product]) -> str:
    if preferred_id in product_ids and classify_image_role(product_by_id[preferred_id].primary_image) != "detail":
        return preferred_id
    return max(
        product_ids,
        key=lambda product_id: (
            {"full": 2, "unknown": 1, "detail": 0}.get(classify_image_role(product_by_id[product_id].primary_image), 0),
            product_by_id[product_id].confidence,
            product_id == preferred_id,
        ),
    )


def _by_supplier(products: List[Product]) -> Dict[str, List[Product]]:
    grouped = defaultdict(list)
    for product in sorted(products, key=lambda item: (item.supplier_id, item.received_at, item.product_id)):
        grouped[product.supplier_id].append(product)
    return grouped


def _attach_suspects(cards: List[StyleCard], card_by_product_id: Dict[str, StyleCard], suspect_groups: List[Dict]) -> None:
    for group in suspect_groups:
        product_ids = [product_id for product_id in group.get("product_ids", []) if product_id in card_by_product_id]
        for product_id in product_ids:
            card = card_by_product_id[product_id]
            for other_id in product_ids:
                other = card_by_product_id[other_id]
                if other is card:
                    continue
                card.suspect_products.append(other.product)


def _attach_local_suspects(cards: List[StyleCard]) -> None:
    histograms = {
        card.product.product_id: [_color_histogram(product.primary_image) for product in _card_products(card)]
        for card in cards
    }
    dominant_buckets = {
        card.product.product_id: [_dominant_saturated_bucket(product.primary_image) for product in _card_products(card)]
        for card in cards
    }
    for index, left in enumerate(cards):
        for right in cards[index + 1 :]:
            if not _can_be_suspect_pair(left.product, right.product):
                continue
            similarity = _max_histogram_intersection(histograms[left.product.product_id], histograms[right.product.product_id])
            grouped_color_signal = _shares_dominant_bucket(dominant_buckets[left.product.product_id], dominant_buckets[right.product.product_id])
            close_to_confirmed_group = left.image_count > 1 or right.image_count > 1
            is_suspect = similarity >= SUSPECT_STYLE_SIMILARITY_THRESHOLD
            is_suspect = is_suspect or (close_to_confirmed_group and grouped_color_signal and similarity >= SUSPECT_GROUP_SIMILARITY_THRESHOLD)
            if not is_suspect:
                continue
            left.suspect_products.append(right.product)
            right.suspect_products.append(left.product)


def _can_be_suspect_pair(left: Product, right: Product) -> bool:
    return left.supplier_id == right.supplier_id and left.category_lv1 == right.category_lv1


def _color_histogram(image_path: str) -> List[float]:
    from PIL import Image

    try:
        with Image.open(image_path) as raw:
            img = raw.convert("RGB").resize((64, 64), Image.Resampling.LANCZOS)
    except Exception:
        return [0.0] * 64

    buckets = [0] * 64
    for red, green, blue in img.getdata():
        if red > 245 and green > 245 and blue > 245:
            continue
        index = (red // 64) * 16 + (green // 64) * 4 + (blue // 64)
        buckets[index] += 1
    total = sum(buckets) or 1
    return [bucket / total for bucket in buckets]


def _histogram_intersection(left: List[float], right: List[float]) -> float:
    return sum(min(a, b) for a, b in zip(left, right))


def _max_histogram_intersection(left: List[List[float]], right: List[List[float]]) -> float:
    return max((_histogram_intersection(a, b) for a in left for b in right), default=0.0)


def _shares_dominant_bucket(left: List[Optional[int]], right: List[Optional[int]]) -> bool:
    left_buckets = {bucket for bucket in left if bucket is not None}
    right_buckets = {bucket for bucket in right if bucket is not None}
    return bool(left_buckets & right_buckets)


def _card_products(card: StyleCard) -> List[Product]:
    return [card.product, *card.detail_products]


def _dominant_saturated_bucket(image_path: str) -> Optional[int]:
    from PIL import Image

    try:
        with Image.open(image_path) as raw:
            img = raw.convert("RGB").resize((64, 64), Image.Resampling.LANCZOS)
    except Exception:
        return None

    buckets = [0] * 64
    for red, green, blue in img.getdata():
        if red > 245 and green > 245 and blue > 245:
            continue
        if max(red, green, blue) - min(red, green, blue) < 45:
            continue
        index = (red // 64) * 16 + (green // 64) * 4 + (blue // 64)
        buckets[index] += 1
    total = sum(buckets)
    if total < 160:
        return None
    dominant = max(range(len(buckets)), key=lambda index: buckets[index])
    return dominant if buckets[dominant] / total >= 0.34 else None


def _read_cache(path: Path) -> Dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _cache_key(products: List[Product]) -> List[Dict[str, str]]:
    return [
        {
            "product_id": product.product_id,
            "primary_image": product.primary_image,
            "category": f"{product.category_lv1}/{product.category_lv2}",
            "phash": product.phash,
        }
        for product in sorted(products, key=lambda item: item.product_id)
    ]


def _extract_gemini_text(payload: Dict) -> str:
    for candidate in payload.get("candidates", []):
        parts = candidate.get("content", {}).get("parts", [])
        for part in parts:
            if "text" in part:
                return part["text"]
    raise ValueError("No text output in Gemini response")


def _google_api_proxies() -> Optional[Dict[str, str]]:
    proxy = os.getenv("GOOGLE_API_PROXY") or os.getenv("GOOGLE_HTTPS_PROXY")
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _style_request_timeout() -> int:
    return int(os.getenv("STYLE_REQUEST_TIMEOUT_SECONDS", "25") or "25")
