from __future__ import annotations

import json
import os
import signal
import shutil
import fcntl
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import List, Sequence

from .collector import ingest_supplier_images
from .images import list_images
from .reply_parser import parse_supplier_reply_text
from .sample_requests import load_enriched_selections
from .selection import detect_selections, write_selection_json
from .storage import Store
from .workflow_state import STATUS_SAMPLE_REQUESTED, load_daily_workflow, write_daily_workflow


@dataclass
class InboxEvent:
    event_id: str
    supplier_id: str
    received_at: str
    image_paths: List[str] = field(default_factory=list)
    text: str = ""
    source: str = "manual"
    role: str = "supplier"
    contact_id: str = ""
    contact_name: str = ""


@dataclass
class InboxProcessResult:
    processed: int = 0
    failed: int = 0
    created_product_ids: List[str] = field(default_factory=list)
    reply_product_ids: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class InboxEventTimeout(TimeoutError):
    pass


class InboxProcessAlreadyRunning(RuntimeError):
    pass


def queue_inbox_event(data_dir: Path, event: InboxEvent) -> Path:
    pending_dir = data_dir / "inbox_events" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    path = pending_dir / f"{event.event_id}.json"
    path.write_text(json.dumps(asdict(event), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_inbox_event(path: Path) -> InboxEvent:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return InboxEvent(**payload)


def process_pending_inbox_events(
    store: Store,
    data_dir: Path,
    root_dir: Path | None = None,
    report_finalize_time: str = "15:00",
) -> InboxProcessResult:
    root = root_dir or Path.cwd()
    pending_dir = data_dir / "inbox_events" / "pending"
    processed_dir = data_dir / "inbox_events" / "processed"
    failed_dir = data_dir / "inbox_events" / "failed"
    result = InboxProcessResult()

    try:
        lock_file = _acquire_process_lock(data_dir)
    except InboxProcessAlreadyRunning as exc:
        result.errors.append(str(exc))
        return result
    try:
        return _process_pending_inbox_events_locked(
            store,
            data_dir,
            root,
            report_finalize_time,
            pending_dir,
            processed_dir,
            failed_dir,
            result,
        )
    finally:
        lock_file.close()


def _process_pending_inbox_events_locked(
    store: Store,
    data_dir: Path,
    root: Path,
    report_finalize_time: str,
    pending_dir: Path,
    processed_dir: Path,
    failed_dir: Path,
    result: InboxProcessResult,
) -> InboxProcessResult:
    if not pending_dir.exists():
        return result

    timeout_seconds = int(os.getenv("INBOX_EVENT_TIMEOUT_SECONDS", "90") or "90")
    for event_path in sorted(pending_dir.glob("*.json")):
        if not event_path.exists():
            continue
        try:
            with _event_time_limit(timeout_seconds, event_path.name):
                event = load_inbox_event(event_path)
                images = _resolve_image_paths(event.image_paths, root, data_dir)
                if event.role == "selector":
                    result.reply_product_ids.extend(_process_selector_image_event(data_dir, event, images))
                else:
                    if images:
                        if _is_after_sample_request(data_dir, event):
                            result.reply_product_ids.extend(_process_supplier_reply_image_event(data_dir, event, images))
                        else:
                            products = ingest_supplier_images(
                                store,
                                data_dir,
                                event.supplier_id,
                                images,
                                _effective_product_received_at(datetime.fromisoformat(event.received_at), report_finalize_time),
                            )
                            result.created_product_ids.extend(product.product_id for product in products)
                    if event.text.strip():
                        result.reply_product_ids.extend(_process_supplier_text_reply_event(data_dir, event))
            _move_event(event_path, processed_dir)
            result.processed += 1
        except Exception as exc:  # pragma: no cover - exercised through result path
            result.failed += 1
            result.errors.append(f"{event_path.name}: {exc}")
            _move_event(event_path, failed_dir)

    return result


def _acquire_process_lock(data_dir: Path):
    lock_dir = data_dir / "runtime"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = (lock_dir / "process_inbox_events.lock").open("w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_file.close()
        raise InboxProcessAlreadyRunning("另一个 process-inbox-events 正在运行，本轮跳过以避免重复入库和文件竞争") from exc
    return lock_file


@contextmanager
def _event_time_limit(seconds: int, event_name: str):
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    previous_handler = signal.getsignal(signal.SIGALRM)

    def _timeout_handler(_signum, _frame):
        raise InboxEventTimeout(f"处理 {event_name} 超过 {seconds} 秒，已跳过并移入 failed，避免卡住整条收图链路")

    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)


def _process_supplier_text_reply_event(data_dir: Path, event: InboxEvent) -> List[str]:
    received_at = datetime.fromisoformat(event.received_at)
    run_date = received_at.date().isoformat()
    workflow = load_daily_workflow(data_dir / "tasks" / run_date / "daily_workflow.json")
    if not workflow:
        return []
    supplier_flow = next((item for item in workflow.suppliers if item.supplier_id == event.supplier_id), None)
    if not supplier_flow or supplier_flow.status != STATUS_SAMPLE_REQUESTED:
        if supplier_flow:
            supplier_flow.last_text_reply_at = event.received_at
            supplier_flow.last_text_reply = event.text.strip()
            supplier_flow.morning_reply_kind = classify_morning_supplier_reply(event.text)
            if not supplier_flow.first_reply_at:
                supplier_flow.first_reply_at = event.received_at
            write_daily_workflow(data_dir / "tasks" / run_date / "daily_workflow.json", workflow)
        return []
    if not supplier_flow.sample_requested_at:
        return []
    try:
        sample_requested_at = datetime.fromisoformat(supplier_flow.sample_requested_at)
    except ValueError:
        return []
    if received_at <= sample_requested_at:
        return []

    report_dir = data_dir / "reports" / run_date
    selection_path = report_dir / "selection.json"
    if not selection_path.exists():
        return []
    selections = [item for item in load_enriched_selections(selection_path) if item["supplier_id"] == event.supplier_id]
    parsed = parse_supplier_reply_text(event.text, selections)
    if not parsed:
        return []

    replies_path = report_dir / "supplier_replies.json"
    replies = _load_supplier_replies(replies_path)
    by_product_id = {item["product_id"]: item for item in replies}
    for item in parsed:
        product_id = item["product_id"]
        by_product_id[product_id] = by_product_id.get(product_id, {}) | item | {
            "supplier_id": event.supplier_id,
            "source": event.source,
            "received_at": event.received_at,
        }
    replies_path.write_text(
        json.dumps({"version": 1, "items": list(by_product_id.values())}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return [item["product_id"] for item in parsed]


def classify_morning_supplier_reply(text: str) -> str:
    content = text.strip().lower()
    no_new_markers = ("没有新款", "没新款", "暂无新款", "无新款", "没有上新", "今天没有", "暂时没有")
    waiting_markers = ("稍后", "晚点", "等下", "一会", "待会", "整理", "马上", "发你", "发给你")
    ack_markers = ("收到", "好的", "好嘞", "ok", "嗯", "可以")
    if any(marker in content for marker in no_new_markers):
        return "no_new"
    if any(marker in content for marker in waiting_markers):
        return "will_send_later"
    if any(marker in content for marker in ack_markers):
        return "ack"
    return "unclear"


def _parse_time(value: str) -> time:
    hour, minute = [int(part) for part in value.split(":", 1)]
    return time(hour, minute)


def _effective_product_received_at(received_at: datetime, report_finalize_time: str) -> datetime:
    if received_at.time() < _parse_time(report_finalize_time):
        return received_at
    next_day = received_at.date() + timedelta(days=1)
    return datetime.combine(next_day, time(0, 0, 0))


def _is_after_sample_request(data_dir: Path, event: InboxEvent) -> bool:
    received_at = datetime.fromisoformat(event.received_at)
    workflow = load_daily_workflow(data_dir / "tasks" / received_at.date().isoformat() / "daily_workflow.json")
    if not workflow:
        return False
    supplier_flow = next((item for item in workflow.suppliers if item.supplier_id == event.supplier_id), None)
    if not supplier_flow or supplier_flow.status != STATUS_SAMPLE_REQUESTED or not supplier_flow.sample_requested_at:
        return False
    try:
        return received_at > datetime.fromisoformat(supplier_flow.sample_requested_at)
    except ValueError:
        return False


def _process_supplier_reply_image_event(data_dir: Path, event: InboxEvent, images: Sequence[Path]) -> List[str]:
    received_at = datetime.fromisoformat(event.received_at)
    run_date = received_at.date().isoformat()
    report_dir = data_dir / "reports" / run_date
    selection_path = report_dir / "selection.json"
    if not selection_path.exists():
        return []
    selections = [item for item in load_enriched_selections(selection_path) if item["supplier_id"] == event.supplier_id]
    if not selections:
        return []
    replies_path = report_dir / "supplier_replies.json"
    replies = _load_supplier_replies(replies_path)
    by_product_id = {item["product_id"]: item for item in replies}
    image_paths = [str(path) for path in images]
    touched = []
    for item in selections:
        product_id = item["product_id"]
        current = by_product_id.get(product_id, {})
        existing_images = list(current.get("info_image_paths", []))
        for image_path in image_paths:
            if image_path not in existing_images:
                existing_images.append(image_path)
        by_product_id[product_id] = current | {
            "product_id": product_id,
            "supplier_id": event.supplier_id,
            "status": current.get("status") or "waiting_product_info",
            "raw_reply": current.get("raw_reply") or "供应商补充了图片资料，未做文字 OCR。",
            "info_image_paths": existing_images,
            "source": event.source,
            "received_at": event.received_at,
        }
        touched.append(product_id)
    replies_path.write_text(
        json.dumps({"version": 1, "items": list(by_product_id.values())}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return touched


def _process_selector_image_event(data_dir: Path, event: InboxEvent, images: Sequence[Path]) -> List[str]:
    received_at = datetime.fromisoformat(event.received_at)
    run_date = received_at.date().isoformat()
    report_dir = data_dir / "reports" / run_date
    manifest_path = report_dir / "manifest.json"
    if not manifest_path.exists() or not images:
        return []
    selection_path = report_dir / "selection.json"
    existing = []
    existing_ids = set()
    if selection_path.exists():
        existing = json.loads(selection_path.read_text(encoding="utf-8"))
        existing_ids = {item.get("product_id") for item in existing}
    selected = []
    for image_path in images:
        for selection in detect_selections(manifest_path, image_path, min_confidence=0.12):
            if selection.product_id in existing_ids:
                continue
            payload = selection.__dict__ | {
                "source": event.source,
                "selector_id": event.contact_id or event.supplier_id,
                "selector_name": event.contact_name,
                "screenshot_path": str(image_path),
                "selected_at": event.received_at,
            }
            existing.append(payload)
            existing_ids.add(selection.product_id)
            selected.append(selection.product_id)
    if selected:
        write_selection_json([_selection_from_dict(item) for item in existing], selection_path)
        selection_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return selected


def _resolve_image_paths(paths: Sequence[str], root: Path, data_dir: Path) -> List[Path]:
    resolved = []
    for raw_path in paths:
        path = Path(raw_path)
        candidates = [path]
        if not path.is_absolute():
            candidates = [root / path, data_dir / path]
        for candidate in candidates:
            if candidate.exists():
                resolved.extend(list_images([candidate]))
                break
    return resolved


def _load_supplier_replies(path: Path) -> List[dict]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("items", payload if isinstance(payload, list) else [])


def _selection_from_dict(item: dict):
    from .models import Selection

    return Selection(
        product_id=item["product_id"],
        confidence=float(item.get("confidence", 1.0)),
        reason=item.get("reason", "选款人截图圈选"),
    )


def _move_event(path: Path, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    if target.exists():
        target = target_dir / f"{path.stem}-{datetime.now().strftime('%H%M%S')}{path.suffix}"
    shutil.move(str(path), target)
    return target
