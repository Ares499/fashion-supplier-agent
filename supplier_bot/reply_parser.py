from __future__ import annotations

import re
from typing import List, Sequence

OUTBOUND_SAMPLE_REQUEST_MARKERS = (
    "这几款选中了",
    "麻烦按图发样",
    "把产品信息发我",
    "我下面把对应图片发你",
    "货号/款号、颜色、尺码、面料/材质、价格、库存或起订量、发货周期",
)
READY_STATUSES = {"product_info_received", "product_info_received_stock_pending"}
TEXT_REPLY_FIELD_LABELS = (
    "货号/款号",
    "面料/材质",
    "库存/起订量",
    "发货周期",
    "货号",
    "款号",
    "款式",
    "编号",
    "颜色",
    "色号",
    "尺码",
    "码数",
    "尺寸",
    "面料",
    "材质",
    "成分",
    "价格",
    "价钱",
    "单价",
    "批价",
    "拿货价",
    "库存",
    "起订量",
    "起订",
    "MOQ",
    "发货",
    "货期",
    "排单",
)


def parse_supplier_reply_text(text: str, selections: Sequence[dict]) -> List[dict]:
    content = text.strip()
    if not content or any(marker in content for marker in OUTBOUND_SAMPLE_REQUEST_MARKERS):
        return []
    matched = [item for item in selections if item["product_id"] in content]
    if matched:
        targets = matched
    elif len(selections) == 1:
        targets = list(selections)
    else:
        return [
            {
                "product_id": item["product_id"],
                "style_no": "",
                "color": "",
                "sizes": "",
                "material": "",
                "price": "",
                "stock_or_moq": "",
                "lead_time": "",
                "status": "needs_mapping_review",
                "raw_reply": content,
            }
            for item in selections
        ]
    if not targets:
        return []

    fields = _extract_text_reply_fields(content)
    info_count = sum(1 for key in ["style_no", "color", "sizes", "material", "price", "stock_or_moq", "lead_time"] if fields.get(key))
    waiting_markers = ("稍后", "晚点", "等下", "一会", "补资料", "回头", "整理", "待会")
    if any(marker in content for marker in waiting_markers) or info_count < 2:
        status = "waiting_product_info"
    else:
        status = "product_info_received"

    return [
        {
            "product_id": item["product_id"],
            "style_no": fields.get("style_no", ""),
            "color": fields.get("color", ""),
            "sizes": fields.get("sizes", ""),
            "material": fields.get("material", ""),
            "price": fields.get("price", ""),
            "stock_or_moq": fields.get("stock_or_moq", ""),
            "lead_time": fields.get("lead_time", ""),
            "status": status,
            "raw_reply": content,
        }
        for item in targets
    ]


def reply_item_ready(item: dict) -> bool:
    return item.get("status") in READY_STATUSES


def _extract_text_reply_fields(text: str) -> dict:
    return {
        "style_no": _first_field(text, ("货号/款号", "货号", "款号", "款式", "编号")),
        "color": _first_field(text, ("颜色", "色号")),
        "sizes": _first_field(text, ("尺码", "码数", "尺寸")),
        "material": _first_field(text, ("面料/材质", "面料", "材质", "成分")),
        "price": _first_field(text, ("价格", "价钱", "单价", "批价", "拿货价")),
        "stock_or_moq": _first_field(text, ("库存/起订量", "库存", "起订量", "起订", "MOQ")) or ("现货" if "现货" in text else ""),
        "lead_time": _first_field(text, ("发货周期", "发货", "货期", "排单")),
    }


def _first_field(text: str, labels: Sequence[str]) -> str:
    for label in labels:
        pattern = rf"{re.escape(label)}\s*[:：]?\s*"
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            start = match.end()
            end = len(text)
            for next_label in sorted(TEXT_REPLY_FIELD_LABELS, key=len, reverse=True):
                next_match = re.search(rf"\s{re.escape(next_label)}\s*[:：]?", text[start:], flags=re.IGNORECASE)
                if next_match:
                    end = min(end, start + next_match.start())
            raw = text[start:end]
            value = re.split(r"[，,；;\n。]", raw, maxsplit=1)[0].strip(" ：:\t\r\n")
            if value:
                return value[:80]
    return ""
