from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Sequence


STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_SKIPPED = "skipped"
STATUS_NEEDS_APPROVAL = "needs_approval"


@dataclass
class DesktopOutboxTask:
    task_id: str
    kind: str
    conversation_name: str
    search_text: str
    message: str
    attachments: List[str] = field(default_factory=list)
    status: str = STATUS_PENDING
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    sent_at: str = ""
    metadata: dict = field(default_factory=dict)
    attempts: int = 0
    last_error: str = ""


def load_outbox(path: Path) -> List[DesktopOutboxTask]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload["tasks"] if isinstance(payload, dict) else payload
    return [DesktopOutboxTask(**item) for item in items]


def write_outbox(path: Path, tasks: Sequence[DesktopOutboxTask]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "tasks": [asdict(task) for task in sorted(tasks, key=lambda item: item.created_at)]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def upsert_outbox_tasks(path: Path, incoming: Iterable[DesktopOutboxTask]) -> List[DesktopOutboxTask]:
    tasks_by_id: Dict[str, DesktopOutboxTask] = {task.task_id: task for task in load_outbox(path)}
    for task in incoming:
        current = tasks_by_id.get(task.task_id)
        if current and current.status != STATUS_PENDING:
            continue
        if current:
            task.created_at = current.created_at
        tasks_by_id[task.task_id] = task
    tasks = sorted(tasks_by_id.values(), key=lambda item: item.created_at)
    write_outbox(path, tasks)
    return tasks


def pending_outbox_tasks(tasks: Sequence[DesktopOutboxTask]) -> List[DesktopOutboxTask]:
    return [task for task in tasks if task.status == STATUS_PENDING]


def mark_outbox_sent(
    path: Path,
    task_ids: Sequence[str],
    sent_at: datetime | None = None,
    metadata_by_task_id: Dict[str, dict] | None = None,
) -> List[DesktopOutboxTask]:
    wanted = set(task_ids)
    timestamp = (sent_at or datetime.now()).isoformat(timespec="seconds")
    tasks = load_outbox(path)
    for task in tasks:
        if task.task_id in wanted:
            task.status = STATUS_SENT
            task.sent_at = timestamp
            extra = (metadata_by_task_id or {}).get(task.task_id)
            if extra:
                task.metadata.update(extra)
    write_outbox(path, tasks)
    return tasks


def mark_outbox_failed_attempt(path: Path, task_id: str, error: str, max_attempts: int = 2) -> List[DesktopOutboxTask]:
    tasks = load_outbox(path)
    for task in tasks:
        if task.task_id != task_id:
            continue
        task.attempts += 1
        task.last_error = error
        if task.attempts >= max_attempts:
            task.status = STATUS_NEEDS_APPROVAL
        break
    write_outbox(path, tasks)
    return tasks


def sent_task_ids(path: Path, kind: str | None = None) -> set[str]:
    return {
        task.task_id
        for task in load_outbox(path)
        if task.status == STATUS_SENT and (kind is None or task.kind == kind)
    }
