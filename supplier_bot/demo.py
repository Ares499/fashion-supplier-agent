from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .models import Supplier
from .storage import Store


DEMO_SUPPLIERS = [
    Supplier("S01", "杭州初白服饰", "张姐", "wm_external_s01", ["上衣/T恤", "上衣/衬衫"], "浙江省杭州市滨江区样品仓 1 号"),
    Supplier("S02", "广州织夏档口", "陈生", "wm_external_s02", ["连体/连衣裙", "下装/裙子"], "广东省广州市白云区样品仓 2 号"),
    Supplier("S03", "濮院针织工厂", "李姐", "wm_external_s03", ["上衣/针织衫", "上衣/外套"], "浙江省嘉兴市濮院镇样品仓 3 号"),
]


def seed_demo(store: Store, suppliers_path: Path) -> None:
    for supplier in DEMO_SUPPLIERS:
        store.upsert_supplier(supplier)
    store.export_suppliers_json(suppliers_path)


def make_demo_images(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    specs = [
        ("tee_white.jpg", "#f2f0e8", "#355c7d", "T恤"),
        ("shirt_blue.jpg", "#dcecf5", "#1f4e79", "衬衫"),
        ("dress_red.jpg", "#f8dfdf", "#b91c1c", "连衣裙"),
        ("skirt_black.jpg", "#f4f4f4", "#222222", "半裙"),
        ("coat_green.jpg", "#e8f1e7", "#426b4f", "外套"),
        ("pants_gray.jpg", "#eeeeee", "#5f6368", "裤子"),
    ]
    for name, bg, fg, label in specs:
        path = output_dir / name
        image = Image.new("RGB", (900, 1200), bg)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((240, 180, 660, 980), radius=80, fill=fg)
        draw.ellipse((330, 70, 570, 300), fill="#f5c7a9")
        font = _font(82)
        draw.text((320, 1030), label, fill="#111111", font=font)
        image.save(path, quality=92)


def demo_received_at(date: str) -> datetime:
    return datetime.fromisoformat(f"{date}T10:30:00")


def _font(size: int):
    for path in ["/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Light.ttc"]:
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()

