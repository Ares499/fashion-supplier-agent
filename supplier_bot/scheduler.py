from datetime import date
from typing import Iterable, List

from .models import Supplier


def is_supplier_rest_day(today: date) -> bool:
    return today.weekday() == 6


def build_batches(suppliers: Iterable[Supplier], batch_size: int = 10) -> List[List[Supplier]]:
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    active = [supplier for supplier in suppliers if not supplier.paused]
    return [active[index : index + batch_size] for index in range(0, len(active), batch_size)]


def should_ask_supplier(supplier: Supplier, today: date) -> bool:
    if is_supplier_rest_day(today):
        return False
    if supplier.paused:
        return False
    if supplier.send_frequency == "daily":
        return True
    if supplier.send_frequency == "weekdays":
        return today.weekday() < 5
    return True
