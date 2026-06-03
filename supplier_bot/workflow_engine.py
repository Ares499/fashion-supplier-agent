from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path
from typing import List, Sequence

from .config import Config
from .contact_roles import ROLE_CONFIRMER, ROLE_OPERATOR, ROLE_SELECTOR, contacts_by_role, contacts_to_suppliers, load_contact_roles
from .desktop_plan import build_daily_question_tasks, daily_question_text, write_desktop_plan
from .desktop_outbox import STATUS_NEEDS_APPROVAL, DesktopOutboxTask, load_outbox, upsert_outbox_tasks, write_outbox
from .models import Product, ProductStatus
from .ops_table import build_ops_table
from .report import build_daily_report
from .reply_parser import reply_item_ready
from .sample_requests import build_sample_request_tasks, load_enriched_selections
from .scheduler import should_ask_supplier
from .storage import Store
from .wecom import WeComClient
from .workflow_state import (
    STATUS_ASK_SENT,
    STATUS_IMAGES_RECEIVED,
    STATUS_INFO_RECEIVED,
    STATUS_OPS_TABLE_SENT,
    STATUS_PENDING_ASK,
    STATUS_REPORT_READY,
    STATUS_REPORT_SENT,
    STATUS_SAMPLE_REQUESTED,
    STATUS_SELECTION_RECEIVED,
    STATUS_WAITING_IMAGES,
    DailyWorkflow,
    SupplierFlow,
    advance_supplier_status,
    initialize_daily_workflow,
    load_daily_workflow,
    suppliers_needing_ask,
    workflow_summary,
    write_daily_workflow,
)


@dataclass
class WorkflowRunResult:
    workflow_path: Path
    actions: List[str]
    summary: dict


class WorkflowEngine:
    def __init__(self, config: Config, store: Store) -> None:
        self.config = config
        self.store = store
        self.client = WeComClient(config)

    def run_once(
        self,
        run_date: date,
        use_ai_style: bool = True,
        send_internal: bool = False,
        allow_ask: bool = True,
        allow_supplier_reminders: bool = True,
        now: datetime | None = None,
    ) -> WorkflowRunResult:
        now = now or datetime.now()
        actions: List[str] = []
        workflow_path = self.workflow_path(run_date)
        workflow = self.ensure_workflow(run_date)
        actions.append("workflow_loaded")

        if allow_ask:
            ask_plan = self.write_ask_plan(run_date, workflow)
            actions.append(f"ask_plan:{ask_plan}")
            ask_outbox = self.write_ask_outbox(run_date, workflow)
            if ask_outbox:
                actions.append(f"ask_outbox:{ask_outbox}")
        else:
            actions.append("ask_paused_until_ask_at")

        if self.apply_sent_outbox(run_date, workflow):
            actions.append("sent_outbox_applied")

        if self.mark_images_received_from_store(run_date, workflow):
            actions.append("images_received")

        if allow_supplier_reminders:
            reminder_outbox = self.write_cutoff_reminder_outbox(run_date, workflow, now)
            if reminder_outbox:
                actions.append(f"cutoff_reminder_outbox:{reminder_outbox}")
        else:
            actions.append("supplier_reminders_blocked_receive_channel_disabled")

        approval_outbox = self.write_attention_outbox(run_date, workflow)
        if approval_outbox:
            actions.append(f"attention_outbox:{approval_outbox}")

        active_supplier_ids = {supplier.supplier_id for supplier in workflow.suppliers}
        products = [
            product
            for product in self.store.list_products_for_date(run_date.isoformat())
            if not active_supplier_ids or product.supplier_id in active_supplier_ids
        ]
        report_dir = self.report_dir(run_date)
        report_signature = _product_signature(products)
        report_stale = bool(products) and workflow.report_signature != report_signature
        if report_stale and workflow.selection_path:
            actions.append("report_stale_after_selection_kept")
        elif report_stale:
            if self.can_build_report(workflow, products, now):
                try:
                    png, _pdf, _manifest = build_daily_report(
                        self.store,
                        run_date.isoformat(),
                        report_dir,
                        use_ai_style=use_ai_style,
                        supplier_ids=active_supplier_ids,
                    )
                except ValueError:
                    png = None
                if png:
                    workflow.report_path = str(png)
                    workflow.report_signature = report_signature
                    workflow.report_version += 1
                    for supplier in workflow.suppliers:
                        if supplier.product_ids and supplier.status in {
                            STATUS_IMAGES_RECEIVED,
                            STATUS_REPORT_READY,
                            STATUS_REPORT_SENT,
                        }:
                            supplier.status = STATUS_REPORT_READY
                    actions.append(f"report_ready:{png}")
            else:
                actions.append("report_waiting_for_more_supplier_images")

        if workflow.report_path:
            report_outbox = self.write_report_outbox(run_date, workflow)
            if report_outbox:
                actions.append(f"report_outbox:{report_outbox}")

        selection_path = report_dir / "selection.json"
        if selection_path.exists() and not workflow.selection_path:
            readiness = _selection_sample_request_readiness(
                selection_path,
                now,
                self.config.selector_selection_quiet_minutes,
                _parse_time(self.config.ops_table_cutoff_time),
            )
            if readiness["ready"]:
                workflow.selection_path = str(selection_path)
                for item in _load_selection_ids(selection_path):
                    advance_supplier_status(workflow, item["supplier_id"], STATUS_SELECTION_RECEIVED)
                request_path = self.sample_request_path(run_date)
                build_sample_request_tasks(selection_path, request_path)
                actions.append(f"sample_request_plan:{request_path}")
            elif readiness["reason"] == "too_late_for_auto_sample_request":
                self._write_pending_approval(
                    run_date,
                    "selection:too_late_for_auto_sample_request",
                    "选款人回传时间已经超过寄样自动推进窗口，系统没有自动联系供应商。请人工确认是否次日再处理。",
                    {"selection_path": str(selection_path), "latest_selected_at": readiness.get("latest_selected_at", "")},
                )
                actions.append("selection_blocked_after_ops_cutoff")
            else:
                actions.append(f"selection_waiting_for_quiet_window:{readiness.get('ready_at', '')}")
        if workflow.selection_path:
            sample_outbox = self.write_sample_request_outbox(run_date, workflow)
            if sample_outbox:
                actions.append(f"sample_outbox:{sample_outbox}")

        replies_path = report_dir / "supplier_replies.json"
        if workflow.selection_path and not workflow.ops_table_path:
            selection_items = _load_selection_ids(Path(workflow.selection_path))
            replies_by_product = _load_ready_replies_by_product(replies_path)
            ready_product_ids = {item["product_id"] for item in selection_items if item["product_id"] in replies_by_product}
            all_selected_ids = {item["product_id"] for item in selection_items}
            ops_cutoff_reached = now.time() >= _parse_time(self.config.ops_table_cutoff_time)
            if ready_product_ids == all_selected_ids or ops_cutoff_reached:
                if ops_cutoff_reached and ready_product_ids != all_selected_ids:
                    _write_missing_supplier_reply_placeholders(replies_path, selection_items)
                for item in selection_items:
                    advance_supplier_status(workflow, item["supplier_id"], STATUS_INFO_RECEIVED)
                self.store.update_status(all_selected_ids, ProductStatus.SUPPLIER_CONFIRMED)
                output = self.ops_table_path(run_date)
                build_ops_table(
                    Path(workflow.selection_path),
                    replies_path,
                    output,
                    root_dir=Path.cwd(),
                    title=f"{run_date.isoformat()} 选款商品信息表",
                )
                workflow.ops_table_path = str(output)
                actions.append(f"ops_table:{output}")
                if ops_cutoff_reached and ready_product_ids != all_selected_ids:
                    actions.append("ops_table_cutoff_with_missing_supplier_info")
            else:
                actions.append("ops_table_waiting_for_supplier_info")
        if workflow.ops_table_path:
            ops_outbox = self.write_ops_table_outbox(run_date, workflow)
            if ops_outbox:
                actions.append(f"ops_outbox:{ops_outbox}")

        if send_internal:
            if workflow.report_path and self.client.official_api_configured():
                self.send_file_to_participants(workflow, Path(workflow.report_path), targets="selectors")
                for supplier in workflow.suppliers:
                    if supplier.status == STATUS_REPORT_READY:
                        advance_supplier_status(workflow, supplier.supplier_id, STATUS_REPORT_SENT)
                actions.append("report_sent_to_selectors")
            if workflow.ops_table_path and self.client.official_api_configured():
                self.send_file_to_participants(workflow, Path(workflow.ops_table_path), targets="operators")
                for supplier in workflow.suppliers:
                    if supplier.status == STATUS_INFO_RECEIVED:
                        advance_supplier_status(workflow, supplier.supplier_id, STATUS_OPS_TABLE_SENT)
                actions.append("ops_table_sent_to_operators")

        write_daily_workflow(workflow_path, workflow)
        return WorkflowRunResult(workflow_path, actions, workflow_summary(workflow))

    def ensure_workflow(self, run_date: date) -> DailyWorkflow:
        contacts = load_contact_roles(self.config.data_dir / "wecom_contacts.json")
        contacts_by_id = {contact.contact_id: contact for contact in contacts}
        selectors = contacts_by_role(contacts, ROLE_SELECTOR)
        operators = contacts_by_role(contacts, ROLE_OPERATOR)
        confirmers = contacts_by_role(contacts, ROLE_CONFIRMER)
        role_suppliers = contacts_to_suppliers(contacts)
        if role_suppliers:
            suppliers = [supplier for supplier in role_suppliers if should_ask_supplier(supplier, run_date)]
            for supplier in suppliers:
                self.store.upsert_supplier(supplier)
        else:
            suppliers = [supplier for supplier in self.store.list_suppliers() if should_ask_supplier(supplier, run_date)]

        path = self.workflow_path(run_date)
        workflow = initialize_daily_workflow(
            run_date.isoformat(),
            suppliers,
            selectors,
            operators,
            confirmers,
            existing=load_daily_workflow(path),
        )
        for supplier_flow in workflow.suppliers:
            contact = contacts_by_id.get(supplier_flow.supplier_id)
            if contact:
                supplier_flow.search_text = contact.search_text or contact.display_name
                supplier_flow.supplier_name = contact.display_name
                supplier_flow.contact_name = contact.display_name
        write_daily_workflow(path, workflow)
        return workflow

    def write_ask_plan(self, run_date: date, workflow: DailyWorkflow) -> Path:
        pending_ids = {item.supplier_id for item in suppliers_needing_ask(workflow)}
        suppliers = [self.store.get_supplier(supplier_id) for supplier_id in pending_ids]
        tasks = build_daily_question_tasks([supplier for supplier in suppliers if supplier], run_date.isoformat())
        return write_desktop_plan(tasks, self.ask_plan_path(run_date))

    def write_ask_outbox(self, run_date: date, workflow: DailyWorkflow) -> Path | None:
        tasks = []
        for flow in suppliers_needing_ask(workflow):
            supplier = self.store.get_supplier(flow.supplier_id)
            if not supplier:
                continue
            text = daily_question_text(supplier, run_date.isoformat())
            tasks.append(
                DesktopOutboxTask(
                    task_id=f"ask:{run_date.isoformat()}:{flow.supplier_id}",
                    kind="ask_supplier",
                    conversation_name=flow.supplier_name,
                    search_text=flow.search_text or flow.supplier_name,
                    message=text,
                    metadata={"supplier_id": flow.supplier_id},
                )
            )
        if not tasks:
            return None
        path = self.outbox_path(run_date)
        upsert_outbox_tasks(path, tasks)
        return path

    def write_report_outbox(self, run_date: date, workflow: DailyWorkflow) -> Path | None:
        if not workflow.selectors:
            return None
        tasks = [
            DesktopOutboxTask(
                task_id=_versioned_task_id("report", run_date, participant.contact_id, workflow.report_version),
                kind="send_report",
                conversation_name=participant.display_name,
                search_text=participant.search_text or participant.display_name,
                message=f"{participant.display_name}，这是今天的选款报表，请截图圈选要的款式后发回这里。",
                attachments=[workflow.report_path],
                metadata={"participant_id": participant.contact_id, "report_version": workflow.report_version},
            )
            for participant in workflow.selectors
        ]
        path = self.outbox_path(run_date)
        upsert_outbox_tasks(path, tasks)
        return path

    def write_sample_request_outbox(self, run_date: date, workflow: DailyWorkflow) -> Path | None:
        request_path = self.sample_request_path(run_date)
        if not request_path.exists():
            return None
        from .task_state import load_tasks

        flow_by_supplier = {flow.supplier_id: flow for flow in workflow.suppliers}
        tasks = []
        for request in load_tasks(request_path):
            if request.status != "pending":
                self._write_pending_approval(
                    run_date,
                    f"sample:{request.supplier_id}:missing_selection_screenshot",
                    f"{request.supplier_name} 的寄样请求缺少该供应商专属的圈选截图，已暂停自动联系供应商。请确认后补图或手动处理。",
                    {"supplier_id": request.supplier_id, "reason": request.notes},
                )
                continue
            flow = flow_by_supplier.get(request.supplier_id)
            tasks.append(
                DesktopOutboxTask(
                    task_id=f"sample:{run_date.isoformat()}:{request.supplier_id}",
                    kind="request_sample",
                    conversation_name=flow.supplier_name if flow else request.supplier_name,
                    search_text=(flow.search_text if flow else request.search_text) or request.supplier_name,
                    message=request.message,
                    attachments=request.attachments,
                    metadata={"supplier_id": request.supplier_id},
                )
            )
        if not tasks:
            return None
        path = self.outbox_path(run_date)
        upsert_outbox_tasks(path, tasks)
        return path

    def write_ops_table_outbox(self, run_date: date, workflow: DailyWorkflow) -> Path | None:
        if not workflow.operators:
            return None
        tasks = [
            DesktopOutboxTask(
                task_id=f"ops:{run_date.isoformat()}:{participant.contact_id}",
                kind="send_ops_table",
                conversation_name=participant.display_name,
                search_text=participant.search_text or participant.display_name,
                message=f"{participant.display_name}，这是选款后的结构化商品信息表，里面有选款图片和供应商回复的款号、颜色、尺码、材质、价格、库存/排单信息。",
                attachments=[workflow.ops_table_path],
                metadata={"participant_id": participant.contact_id},
            )
            for participant in workflow.operators
        ]
        path = self.outbox_path(run_date)
        upsert_outbox_tasks(path, tasks)
        return path

    def apply_sent_outbox(self, run_date: date, workflow: DailyWorkflow) -> bool:
        path = self.outbox_path(run_date)
        if not path.exists():
            return False
        changed = False
        sent_tasks = [task for task in load_outbox(path) if task.status == "sent"]
        sent = {task.task_id for task in sent_tasks}
        sent_at_by_id = {task.task_id: task.sent_at for task in sent_tasks if task.sent_at}
        for supplier in workflow.suppliers:
            ask_task_id = f"ask:{run_date.isoformat()}:{supplier.supplier_id}"
            if ask_task_id in sent:
                updates = {"sent_at": sent_at_by_id[ask_task_id]} if sent_at_by_id.get(ask_task_id) else {}
                advance_supplier_status(workflow, supplier.supplier_id, STATUS_WAITING_IMAGES, **updates)
                changed = True
            if f"sample:{run_date.isoformat()}:{supplier.supplier_id}" in sent:
                sample_task_id = f"sample:{run_date.isoformat()}:{supplier.supplier_id}"
                updates = {"sample_requested_at": sent_at_by_id[sample_task_id]} if sent_at_by_id.get(sample_task_id) else {}
                advance_supplier_status(workflow, supplier.supplier_id, STATUS_SAMPLE_REQUESTED, **updates)
                changed = True
            reminder_task_id = f"reminder:{run_date.isoformat()}:{supplier.supplier_id}"
            if reminder_task_id in sent:
                updates = {"reminder_sent_at": sent_at_by_id[reminder_task_id]} if sent_at_by_id.get(reminder_task_id) else {}
                advance_supplier_status(workflow, supplier.supplier_id, supplier.status, **updates)
                changed = True
        if workflow.selectors and any(
            _versioned_task_id("report", run_date, participant.contact_id, workflow.report_version) in sent
            for participant in workflow.selectors
        ):
            for supplier in workflow.suppliers:
                if supplier.status == STATUS_REPORT_READY:
                    advance_supplier_status(workflow, supplier.supplier_id, STATUS_REPORT_SENT)
                    changed = True
        if workflow.operators and any(f"ops:{run_date.isoformat()}:{participant.contact_id}" in sent for participant in workflow.operators):
            for supplier in workflow.suppliers:
                if supplier.status == STATUS_INFO_RECEIVED:
                    advance_supplier_status(workflow, supplier.supplier_id, STATUS_OPS_TABLE_SENT)
                    changed = True
        return changed

    def write_cutoff_reminder_outbox(self, run_date: date, workflow: DailyWorkflow, now: datetime) -> Path | None:
        if now.time() < _parse_time(self.config.supplier_reminder_time):
            return None
        contacts_by_id = {
            contact.contact_id: contact
            for contact in load_contact_roles(self.config.data_dir / "wecom_contacts.json")
        }
        products_by_supplier = {}
        for product in self.store.list_products_for_date(run_date.isoformat()):
            products_by_supplier.setdefault(product.supplier_id, []).append(product)
        tasks = []
        for flow in workflow.suppliers:
            if flow.status not in {
                STATUS_PENDING_ASK,
                STATUS_ASK_SENT,
                STATUS_WAITING_IMAGES,
                STATUS_IMAGES_RECEIVED,
                STATUS_REPORT_READY,
                STATUS_REPORT_SENT,
            }:
                continue
            if flow.status == STATUS_PENDING_ASK and not flow.sent_at:
                continue
            if flow.supplier_id in products_by_supplier or flow.reminder_sent_at:
                continue
            contact = contacts_by_id.get(flow.supplier_id)
            if not contact or not contact.external_user_id:
                self._write_pending_approval(
                    run_date,
                    f"reminder:{flow.supplier_id}:missing_external_userid",
                    f"{flow.supplier_name} 还没有绑定官方 external_userid，系统无法确认是否已通过 SDK 回图，已禁止自动追问。",
                    {"supplier_id": flow.supplier_id, "reason": "missing_external_userid"},
                )
                continue
            if flow.morning_reply_kind == "no_new":
                continue
            if flow.morning_reply_kind == "unclear":
                self._write_pending_approval(
                    run_date,
                    f"reminder:{flow.supplier_id}:unclear_reply",
                    f"{flow.supplier_name} 今天回复过但语义不明确，系统没有自动催款。原话：{flow.last_text_reply}",
                    {"supplier_id": flow.supplier_id, "raw_reply": flow.last_text_reply},
                )
                continue
            if not flow.first_reply_at and not flow.last_text_reply_at:
                message = "你好，今天有新款麻烦发我一下，图片直接发这里就可以，谢谢。"
            else:
                message = "你好，今天如果有新款图片，方便的话麻烦发我一下，直接发这里就可以，谢谢。"
            tasks.append(
                DesktopOutboxTask(
                    task_id=f"reminder:{run_date.isoformat()}:{flow.supplier_id}",
                    kind="remind_supplier",
                    conversation_name=flow.supplier_name,
                    search_text=flow.search_text or flow.supplier_name,
                    message=message,
                    metadata={"supplier_id": flow.supplier_id, "morning_reply_kind": flow.morning_reply_kind},
                )
            )
        if not tasks:
            return None
        path = self.outbox_path(run_date)
        upsert_outbox_tasks(path, tasks)
        return path

    def write_attention_outbox(self, run_date: date, workflow: DailyWorkflow) -> Path | None:
        tasks = []
        approvals = _load_pending_approvals(self.pending_approvals_path(run_date))
        attention_participants = workflow.confirmers or workflow.operators
        for approval in approvals:
            if approval.get("status", "pending") != "pending" or approval.get("outbox_created"):
                continue
            for participant in attention_participants:
                tasks.append(
                    DesktopOutboxTask(
                        task_id=f"approval:{run_date.isoformat()}:{approval['approval_id']}:{participant.contact_id}",
                        kind="ask_ares",
                        conversation_name=participant.display_name,
                        search_text=participant.search_text or participant.display_name,
                        message=approval["message"],
                        metadata={"approval_id": approval["approval_id"]},
                    )
                )
            approval["outbox_created"] = True
        outbox_path = self.outbox_path(run_date)
        outbox_tasks = load_outbox(outbox_path)
        outbox_changed = False
        for failed in outbox_tasks:
            if failed.status != STATUS_NEEDS_APPROVAL or failed.metadata.get("approval_created"):
                continue
            for participant in attention_participants:
                tasks.append(
                    DesktopOutboxTask(
                        task_id=f"approval:{run_date.isoformat()}:send_failed:{failed.task_id}:{participant.contact_id}",
                        kind="ask_ares",
                        conversation_name=participant.display_name,
                        search_text=participant.search_text or participant.display_name,
                        message=f"桌面端发送任务失败，需要人工确认：{failed.task_id}\n错误：{failed.last_error}",
                        metadata={"failed_task_id": failed.task_id},
                    )
                )
            failed.metadata["approval_created"] = True
            outbox_changed = True
        if approvals:
            _write_pending_approvals(self.pending_approvals_path(run_date), approvals)
        if outbox_changed:
            write_outbox(outbox_path, outbox_tasks)
        if not tasks:
            return None
        upsert_outbox_tasks(outbox_path, tasks)
        return outbox_path

    def _write_pending_approval(self, run_date: date, approval_id: str, message: str, metadata: dict) -> None:
        path = self.pending_approvals_path(run_date)
        approvals = _load_pending_approvals(path)
        if any(item["approval_id"] == approval_id for item in approvals):
            return
        approvals.append(
            {
                "approval_id": approval_id,
                "message": message,
                "metadata": metadata,
                "status": "pending",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        _write_pending_approvals(path, approvals)

    def mark_images_received_from_store(self, run_date: date, workflow: DailyWorkflow) -> bool:
        changed = False
        by_supplier = {}
        active_supplier_ids = {supplier.supplier_id for supplier in workflow.suppliers}
        for product in self.store.list_products_for_date(run_date.isoformat()):
            if active_supplier_ids and product.supplier_id not in active_supplier_ids:
                continue
            by_supplier.setdefault(product.supplier_id, []).append(product)
        for supplier_id, products in by_supplier.items():
            advance_supplier_status(
                workflow,
                supplier_id,
                STATUS_IMAGES_RECEIVED,
                image_count=sum(1 + len(product.related_images) for product in products),
                product_ids=[product.product_id for product in products],
                first_reply_at=min(product.received_at for product in products).isoformat(timespec="seconds"),
            )
            changed = True
        return changed

    def can_build_report(self, workflow: DailyWorkflow, products: Sequence[Product], now: datetime) -> bool:
        if not products:
            return False
        latest_product_at_by_supplier = {}
        for product in products:
            current = latest_product_at_by_supplier.get(product.supplier_id)
            if current is None or product.received_at > current:
                latest_product_at_by_supplier[product.supplier_id] = product.received_at
        quiet_minutes = max(0, self.config.supplier_image_quiet_minutes)
        quiet_after = now - timedelta(minutes=quiet_minutes)
        product_suppliers_are_quiet = all(received_at <= quiet_after for received_at in latest_product_at_by_supplier.values())
        if not product_suppliers_are_quiet:
            return False

        supplier_ids_with_products = set(latest_product_at_by_supplier)
        active_supplier_ids = {supplier.supplier_id for supplier in workflow.suppliers}
        if active_supplier_ids and active_supplier_ids <= supplier_ids_with_products:
            return True
        waiting_suppliers = [
            supplier
            for supplier in workflow.suppliers
            if supplier.supplier_id not in supplier_ids_with_products
            and supplier.status in {STATUS_PENDING_ASK, STATUS_ASK_SENT, STATUS_WAITING_IMAGES}
        ]
        if not waiting_suppliers:
            return True
        finalize_time = _parse_time(self.config.report_finalize_time)
        return now.time() >= finalize_time

    def send_file_to_participants(self, workflow: DailyWorkflow, path: Path, targets: str) -> None:
        participants = workflow.selectors if targets == "selectors" else workflow.operators
        for participant in participants:
            self.client.send_app_file(participant.contact_id, path)

    def workflow_path(self, run_date: date) -> Path:
        return self.config.data_dir / "tasks" / run_date.isoformat() / "daily_workflow.json"

    def ask_plan_path(self, run_date: date) -> Path:
        return self.config.data_dir / "tasks" / run_date.isoformat() / "desktop_ask_batch_1.json"

    def sample_request_path(self, run_date: date) -> Path:
        return self.config.data_dir / "tasks" / run_date.isoformat() / "sample_request_tasks.json"

    def report_dir(self, run_date: date) -> Path:
        return self.config.data_dir / "reports" / run_date.isoformat()

    def ops_table_path(self, run_date: date) -> Path:
        return self.config.data_dir / "reports" / run_date.isoformat() / "ops_selected_product_info.xlsx"

    def outbox_path(self, run_date: date) -> Path:
        return self.config.data_dir / "tasks" / run_date.isoformat() / "desktop_outbox.json"

    def pending_approvals_path(self, run_date: date) -> Path:
        return self.config.data_dir / "tasks" / run_date.isoformat() / "pending_approvals.json"


def _load_selection_ids(selection_path: Path) -> List[dict]:
    return load_enriched_selections(selection_path)


def _selection_sample_request_readiness(
    selection_path: Path,
    now: datetime,
    quiet_minutes: int,
    cutoff_time: day_time,
) -> dict:
    payload = json.loads(selection_path.read_text(encoding="utf-8"))
    if not payload:
        return {"ready": False, "reason": "empty_selection"}

    selected_times = []
    for item in payload:
        selected_at = item.get("selected_at")
        if not selected_at:
            continue
        try:
            selected_times.append(datetime.fromisoformat(selected_at))
        except ValueError:
            continue
    if not selected_times:
        return {"ready": True, "reason": "legacy_selection_without_timestamp"}

    latest_selected_at = max(selected_times)
    ready_at = latest_selected_at + timedelta(minutes=max(0, quiet_minutes))
    cutoff_at = datetime.combine(latest_selected_at.date(), cutoff_time)
    if latest_selected_at >= cutoff_at or ready_at > cutoff_at:
        return {
            "ready": False,
            "reason": "too_late_for_auto_sample_request",
            "latest_selected_at": latest_selected_at.isoformat(timespec="seconds"),
            "ready_at": ready_at.isoformat(timespec="seconds"),
        }
    if now < ready_at:
        return {
            "ready": False,
            "reason": "quiet_window",
            "latest_selected_at": latest_selected_at.isoformat(timespec="seconds"),
            "ready_at": ready_at.isoformat(timespec="seconds"),
        }
    return {
        "ready": True,
        "reason": "quiet_window_elapsed",
        "latest_selected_at": latest_selected_at.isoformat(timespec="seconds"),
        "ready_at": ready_at.isoformat(timespec="seconds"),
    }


def _load_ready_replies_by_product(replies_path: Path) -> dict[str, dict]:
    if not replies_path.exists():
        return {}
    payload = json.loads(replies_path.read_text(encoding="utf-8"))
    items = payload.get("items", payload if isinstance(payload, list) else [])
    return {item["product_id"]: item for item in items if item.get("product_id") and reply_item_ready(item)}


def _load_supplier_replies(replies_path: Path) -> list[dict]:
    if not replies_path.exists():
        return []
    payload = json.loads(replies_path.read_text(encoding="utf-8"))
    return payload.get("items", payload if isinstance(payload, list) else [])


def _write_missing_supplier_reply_placeholders(replies_path: Path, selection_items: Sequence[dict]) -> None:
    replies = _load_supplier_replies(replies_path)
    by_product_id = {item["product_id"]: item for item in replies if item.get("product_id")}
    for item in selection_items:
        product_id = item["product_id"]
        if product_id in by_product_id:
            continue
        by_product_id[product_id] = {
            "product_id": product_id,
            "supplier_id": item["supplier_id"],
            "supplier_name": item.get("supplier_name", ""),
            "status": "supplier_no_reply",
            "raw_reply": "供应商未回复",
        }
    replies_path.parent.mkdir(parents=True, exist_ok=True)
    replies_path.write_text(
        json.dumps({"version": 1, "items": list(by_product_id.values())}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_pending_approvals(path: Path) -> list[dict]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("items", payload if isinstance(payload, list) else [])


def _write_pending_approvals(path: Path, approvals: Sequence[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "items": list(approvals)}, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _product_signature(products: Sequence[Product]) -> str:
    payload = [
        {
            "product_id": product.product_id,
            "primary_image": product.primary_image,
            "related_images": product.related_images,
            "category": f"{product.category_lv1}/{product.category_lv2}",
            "phash": product.phash,
        }
        for product in sorted(products, key=lambda item: item.product_id)
    ]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _parse_time(value: str) -> day_time:
    try:
        hour, minute = [int(part) for part in value.split(":", 1)]
        return day_time(hour, minute)
    except Exception:
        return day_time(21, 0)


def _versioned_task_id(kind: str, run_date: date, participant_id: str, version: int) -> str:
    base = f"{kind}:{run_date.isoformat()}:{participant_id}"
    return base if version <= 1 else f"{base}:v{version}"
