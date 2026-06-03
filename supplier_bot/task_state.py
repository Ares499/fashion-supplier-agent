import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional


@dataclass
class SupplierReplyTask:
    supplier_id: str
    supplier_name: str
    contact_name: str
    search_text: str
    message: str
    status: str = "pending"
    sent_at: Optional[str] = None
    received_at: Optional[str] = None
    ingested_product_ids: Optional[List[str]] = None
    attachments: List[str] = None
    notes: str = ""

    def __post_init__(self) -> None:
        if self.attachments is None:
            self.attachments = []


def load_tasks(path: Path) -> List[SupplierReplyTask]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [SupplierReplyTask(**item) for item in payload]


def write_tasks(tasks: List[SupplierReplyTask], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(task) for task in tasks], ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def mark_tasks_sent(tasks: List[SupplierReplyTask], sent_at: Optional[datetime] = None) -> List[SupplierReplyTask]:
    timestamp = (sent_at or datetime.now()).isoformat(timespec="seconds")
    for task in tasks:
        task.status = "waiting_reply"
        task.sent_at = timestamp
    return tasks


def pending_reply_tasks(tasks: List[SupplierReplyTask]) -> List[SupplierReplyTask]:
    return [task for task in tasks if task.status in {"sent", "waiting_reply", "overdue"}]
