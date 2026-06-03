from __future__ import annotations

import base64
import json
import mimetypes
import os
import multiprocessing
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PIL import Image


CATEGORIES = {
    "上衣": ["T恤", "衬衫", "卫衣", "针织衫", "外套", "背心", "吊带"],
    "下装": ["裤子", "半裙", "短裤"],
    "连体": ["连衣裙", "套装", "连体裤"],
    "鞋履": ["单鞋", "运动鞋", "凉鞋", "靴子", "拖鞋"],
    "箱包": ["手提包", "斜挎包", "双肩包", "钱包"],
    "配饰": ["帽子", "围巾", "腰带", "首饰", "眼镜", "其他"],
    "其他": ["需人工复核"],
}

LOW_CONFIDENCE_THRESHOLD = 0.7


@dataclass
class Classification:
    category_lv1: str
    category_lv2: str
    confidence: float
    source: str
    needs_review: bool
    attributes: Dict[str, str]
    reason: str


def classify_image(image_path: str, supplier_categories=None) -> Tuple[str, str, float]:
    result = classify_image_detail(image_path, supplier_categories)
    return result.category_lv1, result.category_lv2, result.confidence


def classify_image_detail_with_deadline(image_path: str, supplier_categories=None, timeout_seconds: int | None = None) -> Classification:
    """Run visual classification in a child process so model/network hangs cannot block the workflow."""
    timeout = timeout_seconds or int(os.getenv("CLASSIFICATION_DEADLINE_SECONDS", "45") or "45")
    if timeout <= 0 or os.getenv("CLASSIFIER_CHILD_PROCESS") == "1":
        return classify_image_detail(image_path, supplier_categories)

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue(maxsize=1)
    process = ctx.Process(target=_classify_worker, args=(image_path, supplier_categories or [], queue))
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(5)
        fallback = _classify_locally(image_path)
        fallback.reason = f"{fallback.reason}; vision_child_timeout={timeout}s"
        fallback.confidence = min(fallback.confidence, 0.4)
        fallback.needs_review = True
        return fallback
    if process.exitcode != 0:
        fallback = _classify_locally(image_path)
        fallback.reason = f"{fallback.reason}; vision_child_exit={process.exitcode}"
        fallback.confidence = min(fallback.confidence, 0.45)
        fallback.needs_review = True
        return fallback
    if queue.empty():
        fallback = _classify_locally(image_path)
        fallback.reason = f"{fallback.reason}; vision_child_empty_result"
        fallback.confidence = min(fallback.confidence, 0.45)
        fallback.needs_review = True
        return fallback
    status, payload = queue.get()
    if status == "ok":
        return payload
    fallback = _classify_locally(image_path)
    fallback.reason = f"{fallback.reason}; vision_child_failed={payload}"
    fallback.confidence = min(fallback.confidence, 0.45)
    fallback.needs_review = True
    return fallback


def _classify_worker(image_path: str, supplier_categories, queue) -> None:
    os.environ["CLASSIFIER_CHILD_PROCESS"] = "1"
    try:
        queue.put(("ok", classify_image_detail(image_path, supplier_categories)))
    except Exception as exc:  # pragma: no cover - exercised by parent fallback
        queue.put(("error", f"{exc.__class__.__name__}: {exc}"))


def classify_image_detail(image_path: str, supplier_categories=None) -> Classification:
    """Classify one product image.

    Supplier categories are weak context only. The image itself is the source
    of truth, because real suppliers can send clothing, shoes, bags, and
    accessories in the same conversation.
    """
    provider = os.getenv("VISION_PROVIDER", "auto").lower()
    suppliers = supplier_categories or []
    candidates = []
    if provider in ("auto", "google", "gemini") and os.getenv("GOOGLE_API_KEY", ""):
        candidates.append(("gemini_vision", _classify_with_gemini, os.getenv("GOOGLE_API_KEY", "")))
    if provider in ("auto", "openai") and os.getenv("OPENAI_API_KEY", ""):
        candidates.append(("openai_vision", _classify_with_openai, os.getenv("OPENAI_API_KEY", "")))

    last_error = ""
    for source, classifier, api_key in candidates:
        try:
            return classifier(image_path, suppliers, api_key)
        except Exception as exc:
            last_error = f"{source}_failed={exc.__class__.__name__}"

    fallback = _classify_locally(image_path)
    if last_error:
        fallback.reason = f"{fallback.reason}; {last_error}"
        fallback.confidence = min(fallback.confidence, 0.55)
    fallback.needs_review = True
    return fallback


def _classify_with_openai(image_path: str, supplier_categories: List[str], api_key: str) -> Classification:
    import requests

    image_url = _image_data_url(Path(image_path))
    prompt = _classification_prompt(supplier_categories)
    payload = {
        "model": os.getenv("OPENAI_VISION_MODEL", "gpt-5-mini"),
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": image_url, "detail": "low"},
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "product_classification",
                "strict": True,
                    "schema": _classification_schema(),
            }
        },
    }
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=_vision_request_timeout(),
    )
    response.raise_for_status()
    raw = _extract_response_text(response.json())
    parsed = json.loads(raw)
    return _normalize_classification(parsed, source="openai_vision")


def _classify_with_gemini(image_path: str, supplier_categories: List[str], api_key: str) -> Classification:
    import requests

    path = Path(image_path)
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    schema = _gemini_classification_schema()
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": mime, "data": encoded}},
                    {"text": _classification_prompt(supplier_categories)},
                ],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    model = os.getenv("GOOGLE_VISION_MODEL", "gemini-2.5-flash")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    response = requests.post(
        url,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json=payload,
        proxies=_google_api_proxies(),
        timeout=_vision_request_timeout(),
    )
    response.raise_for_status()
    raw = _extract_gemini_text(response.json())
    parsed = json.loads(raw)
    return _normalize_classification(parsed, source="gemini_vision")


def _classify_locally(image_path: str) -> Classification:
    name = Path(image_path).name.lower()
    hints = [
        ("tshirt", "上衣", "T恤"),
        ("t-shirt", "上衣", "T恤"),
        ("tee", "上衣", "T恤"),
        ("shirt", "上衣", "衬衫"),
        ("top", "上衣", "T恤"),
        ("dress", "连体", "连衣裙"),
        ("skirt", "下装", "半裙"),
        ("pants", "下装", "裤子"),
        ("shorts", "下装", "短裤"),
        ("coat", "上衣", "外套"),
        ("jacket", "上衣", "外套"),
        ("sneaker", "鞋履", "运动鞋"),
        ("shoe", "鞋履", "单鞋"),
        ("shoes", "鞋履", "单鞋"),
        ("sandal", "鞋履", "凉鞋"),
        ("boot", "鞋履", "靴子"),
        ("bag", "箱包", "手提包"),
        ("hat", "配饰", "帽子"),
    ]
    for keyword, lv1, lv2 in hints:
        if keyword in name:
            return Classification(lv1, lv2, 0.82, "filename_fallback", False, {}, f"文件名包含 {keyword}")

    with Image.open(image_path) as img:
        width, height = img.size
    ratio = height / max(width, 1)
    if ratio > 1.55:
        return Classification("连体", "连衣裙", 0.46, "heuristic_fallback", True, {}, "竖图比例像连衣裙，但未做视觉识别")
    if ratio < 0.75:
        return Classification("其他", "需人工复核", 0.35, "heuristic_fallback", True, {}, "横图比例无法可靠判断")
    return Classification("其他", "需人工复核", 0.3, "heuristic_fallback", True, {}, "未启用视觉模型且本地规则没有可靠线索")


def _normalize_classification(payload: Dict, source: str) -> Classification:
    lv1 = str(payload.get("category_lv1", "其他"))
    lv2 = str(payload.get("category_lv2", "需人工复核"))
    confidence = float(payload.get("confidence", 0))
    if lv1 not in CATEGORIES or lv2 not in CATEGORIES.get(lv1, []):
        lv1, lv2 = "其他", "需人工复核"
        confidence = min(confidence, 0.4)
    confidence = max(0.0, min(1.0, confidence))
    return Classification(
        category_lv1=lv1,
        category_lv2=lv2,
        confidence=round(confidence, 3),
        source=source,
        needs_review=confidence < LOW_CONFIDENCE_THRESHOLD or lv1 == "其他",
        attributes={key: str(value) for key, value in dict(payload.get("attributes", {})).items()},
        reason=str(payload.get("reason", "")),
    )


def _classification_prompt(supplier_categories: List[str]) -> str:
    categories = "; ".join(f"{lv1}: {', '.join(lv2s)}" for lv1, lv2s in CATEGORIES.items())
    weak_context = "、".join(supplier_categories) if supplier_categories else "无"
    return (
        "你是服装电商选款图片分类器。请只根据图片内容给单张商品图分类，"
        "供应商主营类目只能作为弱参考，不能覆盖图片本身。"
        f"可选类目如下：{categories}。"
        f"供应商弱参考类目：{weak_context}。"
        "如果图片不是服装/鞋包/配饰商品，或无法判断，请输出 其他/需人工复核。"
    )


def _classification_schema() -> Dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["category_lv1", "category_lv2", "confidence", "attributes", "reason"],
        "properties": {
            "category_lv1": {"type": "string", "enum": list(CATEGORIES.keys())},
            "category_lv2": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "attributes": {
                "type": "object",
                "additionalProperties": False,
                "required": ["color", "style", "material", "pattern"],
                "properties": {
                    "color": {"type": "string"},
                    "style": {"type": "string"},
                    "material": {"type": "string"},
                    "pattern": {"type": "string"},
                },
            },
            "reason": {"type": "string"},
        },
    }


def _gemini_classification_schema() -> Dict:
    schema = _classification_schema()
    schema.pop("additionalProperties", None)
    schema["properties"]["attributes"].pop("additionalProperties", None)
    return schema


def _google_api_proxies() -> Optional[Dict[str, str]]:
    proxy = os.getenv("GOOGLE_API_PROXY") or os.getenv("GOOGLE_HTTPS_PROXY")
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _vision_request_timeout() -> int:
    return int(os.getenv("VISION_REQUEST_TIMEOUT_SECONDS", "25") or "25")


def _image_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _extract_response_text(payload: Dict) -> str:
    if payload.get("output_text"):
        return payload["output_text"]
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if "text" in content:
                return content["text"]
    raise ValueError("No text output in OpenAI response")


def _extract_gemini_text(payload: Dict) -> str:
    for candidate in payload.get("candidates", []):
        content = candidate.get("content", {})
        for part in content.get("parts", []):
            if "text" in part:
                return part["text"]
    raise ValueError("No text output in Gemini response")
