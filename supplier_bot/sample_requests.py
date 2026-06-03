from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from PIL import Image

from .task_state import SupplierReplyTask, write_tasks


def build_sample_request_tasks(selection_path: Path, output_path: Path) -> Path:
    selections = load_enriched_selections(selection_path)
    manifest = _load_manifest(selection_path.parent / "manifest.json")
    manifest_page_width = int(manifest.get("page_width") or 0)
    grouped: Dict[str, List[dict]] = {}
    for item in selections:
        grouped.setdefault(item["supplier_id"], []).append(item)

    tasks: List[SupplierReplyTask] = []
    for supplier_id, items in sorted(grouped.items()):
        supplier_name = items[0]["supplier_name"]
        message = _sample_request_message(items)
        attachments, blocked_reasons = _selection_attachments(
            items,
            manifest_page_width,
            selection_path.parent / "supplier_selection_attachments",
        )
        status = "pending" if attachments else "needs_selection_screenshot"
        notes = "" if attachments else "；".join(blocked_reasons) or "缺少该供应商专属的选款圈选截图，已阻止自动寄样请求。"
        tasks.append(
            SupplierReplyTask(
                supplier_id=supplier_id,
                supplier_name=supplier_name,
                contact_name=supplier_name,
                search_text=supplier_name,
                message=message,
                status=status,
                attachments=attachments,
                notes=notes,
            )
        )
    write_tasks(tasks, output_path)
    return output_path


def load_enriched_selections(selection_path: Path) -> List[dict]:
    selections = json.loads(selection_path.read_text(encoding="utf-8"))
    manifest_path = selection_path.parent / "manifest.json"
    manifest = _load_manifest(manifest_path)
    manifest_items = {item["product_id"]: item for item in manifest.get("products", [])}

    enriched = []
    for item in selections:
        merged = dict(manifest_items.get(item["product_id"], {}))
        merged.update(item)
        if "supplier_id" not in merged:
            raise ValueError(f"selection missing supplier_id and manifest entry: {item['product_id']}")
        enriched.append(merged)
    return enriched


def _load_manifest(manifest_path: Path) -> dict:
    if not manifest_path.exists():
        return {}
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _sample_request_message(items: List[dict]) -> str:
    return "\n".join(
        [
            "这几款选中了，麻烦按图发样，并把商品信息发我：",
            "货号/款号、颜色、尺码、面料/材质、价格、库存或起订量、发货周期。",
            "",
            "我下面把对应图片发你。",
        ]
    )


def _selection_attachments(
    items: List[dict],
    manifest_page_width: int,
    output_dir: Path,
) -> tuple[List[str], List[str]]:
    attachments = []
    blocked_reasons = []
    seen = set()
    supplier_id = items[0]["supplier_id"]
    by_screenshot: Dict[str, List[dict]] = {}
    for item in items:
        screenshot_path = item.get("screenshot_path")
        if not screenshot_path:
            blocked_reasons.append("缺少选款圈选截图")
            continue
        if not _selection_source_is_safe_for_auto_send(item):
            blocked_reasons.append("选款截图来源不是官方存档或今天报表消息之后的桌面采集，已阻止自动发送")
            continue
        by_screenshot.setdefault(screenshot_path, []).append(item)

    for screenshot_path, screenshot_items in by_screenshot.items():
        safe_attachment = _supplier_card_attachment(screenshot_path, supplier_id, screenshot_items, manifest_page_width, output_dir)
        if not safe_attachment:
            blocked_reasons.append("无法生成供应商专属完整卡片截图，已阻止自动发送")
            continue
        if safe_attachment not in seen:
            attachments.append(safe_attachment)
            seen.add(safe_attachment)
    return attachments, sorted(set(blocked_reasons))


def _selection_source_is_safe_for_auto_send(item: dict) -> bool:
    source = item.get("source", "")
    if source in {"", "manual", "wecom_archive"}:
        return True
    if source != "desktop_selector_capture":
        return False
    return item.get("capture_mode") == "after_report_message_boundary" and item.get("capture_stop_reason") == "found_daily_ask_message"


def _supplier_card_attachment(
    screenshot_path: str,
    supplier_id: str,
    items: List[dict],
    manifest_page_width: int,
    output_dir: Path,
) -> str | None:
    source_path = Path(screenshot_path)
    if not source_path.exists():
        return None

    with Image.open(source_path) as raw:
        image = raw.convert("RGB")
        scale = (manifest_page_width or image.width) / max(image.width, 1)
        crops = []
        for item in items:
            if item.get("supplier_id") != supplier_id:
                continue
            box = _scaled_box(item.get("box"), scale, image.width, image.height)
            if not box:
                continue
            crops.append(image.crop(box))
        if not crops:
            return screenshot_path
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{source_path.stem}.{supplier_id}.supplier_cards.jpg"
        _stack_card_crops(crops).save(output_path, quality=94)
        return str(output_path)


def _scaled_box(raw_box, scale: float, width: int, height: int) -> tuple[int, int, int, int] | None:
    if not isinstance(raw_box, list) or len(raw_box) != 4:
        return None
    pad = 14
    left = max(0, int(raw_box[0] / scale) - pad)
    top = max(0, int(raw_box[1] / scale) - pad)
    right = min(width, int(raw_box[2] / scale) + pad)
    bottom = min(height, int(raw_box[3] / scale) + pad)
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def _stack_card_crops(crops: List[Image.Image]) -> Image.Image:
    margin = 18
    gap = 16
    width = max(crop.width for crop in crops) + margin * 2
    height = sum(crop.height for crop in crops) + margin * 2 + gap * (len(crops) - 1)
    canvas = Image.new("RGB", (width, height), "#ffffff")
    y = margin
    for crop in crops:
        x = (width - crop.width) // 2
        canvas.paste(crop, (x, y))
        y += crop.height + gap
    return canvas
