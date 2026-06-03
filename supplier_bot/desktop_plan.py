import json
import re
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import List, Optional

from .models import Supplier
from .scheduler import build_batches, should_ask_supplier
from .storage import Store
from .task_state import SupplierReplyTask, write_tasks


DesktopMessageTask = SupplierReplyTask


def daily_question_marker(supplier: Supplier, run_date: str) -> str:
    parsed_date = date.fromisoformat(run_date)
    supplier_code = re.sub(r"[^A-Za-z0-9]+", "", supplier.supplier_id).upper()[:16] or "SUP"
    return f"{parsed_date.strftime('%m%d')}-{supplier_code}"


def daily_question_text(supplier: Supplier, run_date: str) -> str:
    marker = daily_question_marker(supplier, run_date)
    return f"你好，今天有新款麻烦发我一下，图片直接发这里就可以，谢谢。\n今日批次：{marker}"



def build_daily_question_plan(
    store: Store,
    run_date: str,
    batch_size: int = 10,
    batch_index: int = 0,
    supplier_ids: Optional[List[str]] = None,
) -> List[DesktopMessageTask]:
    parsed_date = date.fromisoformat(run_date)
    suppliers = [supplier for supplier in store.list_suppliers() if should_ask_supplier(supplier, parsed_date)]
    if supplier_ids:
        wanted = set(supplier_ids)
        suppliers = [supplier for supplier in suppliers if supplier.supplier_id in wanted]
    batches = build_batches(suppliers, batch_size)
    batch = batches[batch_index] if 0 <= batch_index < len(batches) else []
    return build_daily_question_tasks(batch, run_date)


def build_daily_question_tasks(suppliers: List[Supplier], run_date: str) -> List[DesktopMessageTask]:
    return [
        DesktopMessageTask(
            supplier_id=supplier.supplier_id,
            supplier_name=supplier.name,
            contact_name=supplier.contact_name,
            search_text=supplier.name,
            message=daily_question_text(supplier, run_date),
        )
        for supplier in suppliers
    ]


def write_desktop_plan(tasks: List[DesktopMessageTask], output_path: Path) -> Path:
    return write_tasks(tasks, output_path)
