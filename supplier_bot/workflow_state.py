from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

from .contact_roles import ContactRole
from .models import Supplier


STATUS_PENDING_ASK = "pending_ask"
STATUS_ASK_SENT = "ask_sent"
STATUS_WAITING_IMAGES = "waiting_images"
STATUS_IMAGES_RECEIVED = "images_received"
STATUS_REPORT_READY = "report_ready"
STATUS_REPORT_SENT = "report_sent"
STATUS_SELECTION_RECEIVED = "selection_received"
STATUS_SAMPLE_REQUESTED = "sample_requested"
STATUS_INFO_RECEIVED = "supplier_info_received"
STATUS_OPS_TABLE_SENT = "ops_table_sent"
STATUS_DONE = "done"

STATUS_ORDER = [
    STATUS_PENDING_ASK,
    STATUS_ASK_SENT,
    STATUS_WAITING_IMAGES,
    STATUS_IMAGES_RECEIVED,
    STATUS_REPORT_READY,
    STATUS_REPORT_SENT,
    STATUS_SELECTION_RECEIVED,
    STATUS_SAMPLE_REQUESTED,
    STATUS_INFO_RECEIVED,
    STATUS_OPS_TABLE_SENT,
    STATUS_DONE,
]


@dataclass
class ParticipantRef:
    contact_id: str
    display_name: str
    external_user_id: str = ""
    search_text: str = ""


@dataclass
class SupplierFlow:
    supplier_id: str
    supplier_name: str
    contact_name: str
    search_text: str
    status: str = STATUS_PENDING_ASK
    sent_at: str = ""
    first_reply_at: str = ""
    image_count: int = 0
    product_ids: List[str] = field(default_factory=list)
    sample_requested_at: str = ""
    reminder_sent_at: str = ""
    last_text_reply_at: str = ""
    last_text_reply: str = ""
    morning_reply_kind: str = ""
    notes: str = ""


@dataclass
class DailyWorkflow:
    date: str
    suppliers: List[SupplierFlow]
    selectors: List[ParticipantRef] = field(default_factory=list)
    operators: List[ParticipantRef] = field(default_factory=list)
    confirmers: List[ParticipantRef] = field(default_factory=list)
    report_path: str = ""
    report_signature: str = ""
    report_version: int = 0
    selection_path: str = ""
    ops_table_path: str = ""


def participant_from_contact(contact: ContactRole) -> ParticipantRef:
    return ParticipantRef(
        contact_id=contact.contact_id,
        display_name=contact.display_name,
        external_user_id=contact.external_user_id,
        search_text=contact.search_text or contact.display_name,
    )


def supplier_flow_from_supplier(supplier: Supplier) -> SupplierFlow:
    return SupplierFlow(
        supplier_id=supplier.supplier_id,
        supplier_name=supplier.name,
        contact_name=supplier.contact_name,
        search_text=supplier.name,
    )


def load_daily_workflow(path: Path) -> DailyWorkflow | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return DailyWorkflow(
        date=payload["date"],
        suppliers=[SupplierFlow(**item) for item in payload.get("suppliers", [])],
        selectors=[ParticipantRef(**item) for item in payload.get("selectors", [])],
        operators=[ParticipantRef(**item) for item in payload.get("operators", [])],
        confirmers=[ParticipantRef(**item) for item in payload.get("confirmers", [])],
        report_path=payload.get("report_path", ""),
        report_signature=payload.get("report_signature", ""),
        report_version=int(payload.get("report_version", 0) or 0),
        selection_path=payload.get("selection_path", ""),
        ops_table_path=payload.get("ops_table_path", ""),
    )


def write_daily_workflow(path: Path, workflow: DailyWorkflow) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(workflow), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def initialize_daily_workflow(
    run_date: str,
    suppliers: Sequence[Supplier],
    selectors: Sequence[ContactRole],
    operators: Sequence[ContactRole],
    confirmers: Sequence[ContactRole] = (),
    existing: DailyWorkflow | None = None,
) -> DailyWorkflow:
    previous: Dict[str, SupplierFlow] = {}
    if existing and existing.date == run_date:
        previous = {item.supplier_id: item for item in existing.suppliers}

    supplier_flows = []
    for supplier in suppliers:
        current = supplier_flow_from_supplier(supplier)
        old = previous.get(supplier.supplier_id)
        if old:
            current.status = old.status
            current.sent_at = old.sent_at
            current.first_reply_at = old.first_reply_at
            current.image_count = old.image_count
            current.product_ids = old.product_ids
            current.sample_requested_at = old.sample_requested_at
            current.reminder_sent_at = old.reminder_sent_at
            current.last_text_reply_at = old.last_text_reply_at
            current.last_text_reply = old.last_text_reply
            current.morning_reply_kind = old.morning_reply_kind
            current.notes = old.notes
        supplier_flows.append(current)

    return DailyWorkflow(
        date=run_date,
        suppliers=sorted(supplier_flows, key=lambda item: (item.supplier_name, item.supplier_id)),
        selectors=[participant_from_contact(contact) for contact in selectors],
        operators=[participant_from_contact(contact) for contact in operators],
        confirmers=[participant_from_contact(contact) for contact in confirmers],
        report_path=existing.report_path if existing and existing.date == run_date else "",
        report_signature=existing.report_signature if existing and existing.date == run_date else "",
        report_version=existing.report_version if existing and existing.date == run_date else 0,
        selection_path=existing.selection_path if existing and existing.date == run_date else "",
        ops_table_path=existing.ops_table_path if existing and existing.date == run_date else "",
    )


def advance_supplier_status(workflow: DailyWorkflow, supplier_id: str, status: str, **updates) -> DailyWorkflow:
    if status not in STATUS_ORDER:
        raise ValueError(f"unknown workflow status: {status}")
    for supplier in workflow.suppliers:
        if supplier.supplier_id != supplier_id:
            continue
        if STATUS_ORDER.index(status) >= STATUS_ORDER.index(supplier.status):
            supplier.status = status
        for key, value in updates.items():
            if hasattr(supplier, key):
                setattr(supplier, key, value)
        return workflow
    raise KeyError(f"supplier not in workflow: {supplier_id}")


def workflow_summary(workflow: DailyWorkflow) -> Dict[str, int]:
    summary = {status: 0 for status in STATUS_ORDER}
    for supplier in workflow.suppliers:
        summary[supplier.status] = summary.get(supplier.status, 0) + 1
    return summary


def suppliers_needing_ask(workflow: DailyWorkflow) -> List[SupplierFlow]:
    return [supplier for supplier in workflow.suppliers if supplier.status == STATUS_PENDING_ASK]


def supplier_ids(flows: Iterable[SupplierFlow]) -> List[str]:
    return [flow.supplier_id for flow in flows]
