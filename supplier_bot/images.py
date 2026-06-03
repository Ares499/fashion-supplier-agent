import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable, List

from PIL import Image


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXTENSIONS


def archive_image(source: Path, data_dir: Path, supplier_id: str, received_at: datetime) -> Path:
    target_dir = data_dir / "inbox" / received_at.strftime("%Y-%m-%d") / supplier_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    return target


def phash(image_path: str, size: int = 12) -> str:
    with Image.open(image_path) as img:
        rgb = img.convert("RGB").resize((1, 1), Image.Resampling.LANCZOS)
        avg_rgb = rgb.getpixel((0, 0))
        img = img.convert("L").resize((size, size), Image.Resampling.LANCZOS)
        pixels = list(img.getdata())
    avg = sum(pixels) / len(pixels)
    bits = ["1" if pixel > avg else "0" for pixel in pixels]
    color_bits = [format(channel // 32, "03b") for channel in avg_rgb]
    return "".join(bits + color_bits)


def hamming_distance(left: str, right: str) -> int:
    return sum(a != b for a, b in zip(left, right))


def similar_hashes(left: str, right: str, threshold: int = 3) -> bool:
    if len(left) >= 9 and len(right) >= 9:
        shape_distance = hamming_distance(left[:-9], right[:-9])
        color_distance = hamming_distance(left[-9:], right[-9:])
        return shape_distance <= threshold and color_distance == 0
    return hamming_distance(left, right) <= threshold


def list_images(paths: Iterable[Path]) -> List[Path]:
    found = []
    for path in paths:
        if path.is_dir():
            found.extend(child for child in sorted(path.rglob("*")) if is_image(child))
        elif path.exists() and is_image(path):
            found.append(path)
    return found
