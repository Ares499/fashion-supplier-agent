from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import List, Optional


class ProductStatus(str, Enum):
    COLLECTED = "已采集"
    SELECTED = "已入选"
    SAMPLE_REQUESTED = "已请求寄样"
    SUPPLIER_CONFIRMED = "供应商已确认"
    SAMPLE_RECEIVED = "样品已到"
    LISTING_DRAFTED = "已生成上架草稿"
    LISTED = "已上架"
    KEPT = "已留下"
    RETURNED = "已退回"


@dataclass
class Supplier:
    supplier_id: str
    name: str
    contact_name: str
    external_user_id: str
    main_categories: List[str]
    sample_address: str
    send_frequency: str = "daily"
    paused: bool = False


@dataclass
class Product:
    product_id: str
    supplier_id: str
    received_at: datetime
    primary_image: str
    related_images: List[str]
    category_lv1: str
    category_lv2: str
    phash: str
    status: ProductStatus = ProductStatus.COLLECTED
    confidence: float = 0.0
    report_box: Optional[List[int]] = None


@dataclass
class Selection:
    product_id: str
    confidence: float
    reason: str
