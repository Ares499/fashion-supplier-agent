from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .config import Config
from .contact_roles import ROLE_SUPPLIER, contacts_by_role, load_contact_roles, supplier_contacts_missing_external_ids
from .health import health_payload, run_health_checks
from .storage import Store


FAILURE_STATE = "receive_channel_failure.json"
RECOVERY_STATE = "receive_channel_recovery.json"


@dataclass
class ReceiveReconciliation:
    ok: bool
    detail: str
    counts: dict[str, int] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)


def diagnose_receive_channel(config: Config, run_date: date, detail: str, project_root: Path | None = None) -> dict[str, Any]:
    checks = health_payload(run_health_checks(config, project_root=project_root, live_api=False))
    health_file = _read_json(config.data_dir / "runtime" / "wecom_archive_health.json")
    causes = _infer_causes(detail, checks)
    return {
        "diagnosed_at": datetime.now().isoformat(timespec="seconds"),
        "date": run_date.isoformat(),
        "detail": detail,
        "suspected_causes": causes,
        "receive_health": health_file,
        "doctor": checks,
    }


def record_receive_channel_failure(
    config: Config,
    run_date: date,
    detail: str,
    diagnostics: dict[str, Any] | None = None,
) -> Path:
    path = _failure_path(config)
    payload = {
        "status": "failed",
        "date": run_date.isoformat(),
        "failed_at": datetime.now().isoformat(timespec="seconds"),
        "detail": detail,
        "diagnostics": diagnostics or diagnose_receive_channel(config, run_date, detail),
        "recovery_required": True,
        "recovered_at": "",
    }
    _write_json(path, payload)
    return path


def receive_recovery_required(config: Config, run_date: date) -> bool:
    payload = _read_json(_failure_path(config))
    if not payload:
        return False
    if payload.get("date") != run_date.isoformat():
        return False
    return bool(payload.get("recovery_required")) and not payload.get("recovered_at")


def reconcile_receive_recovery(config: Config, store: Store, run_date: date) -> ReceiveReconciliation:
    pending_events = _event_files_for_date(config.data_dir / "inbox_events" / "pending", run_date)
    failed_events = _event_files_for_date(config.data_dir / "inbox_events" / "failed", run_date)
    unknown_events = list((config.data_dir / "archive_unknown" / run_date.isoformat()).glob("*.json"))
    contacts = load_contact_roles(config.data_dir / "wecom_contacts.json")
    active_suppliers = contacts_by_role(contacts, ROLE_SUPPLIER)
    missing_external = supplier_contacts_missing_external_ids(contacts)
    products = store.list_products_for_date(run_date.isoformat())
    counts = {
        "pending_inbox_events": len(pending_events),
        "failed_inbox_events": len(failed_events),
        "unknown_archive_events": len(unknown_events),
        "active_suppliers": len(active_suppliers),
        "suppliers_missing_external_id": len(missing_external),
        "products_for_date": len(products),
    }
    blockers = []
    if pending_events:
        blockers.append(f"还有未处理收件事件 {len(pending_events)} 条")
    if failed_events:
        blockers.append(f"还有处理失败收件事件 {len(failed_events)} 条")
    if unknown_events:
        blockers.append(f"还有未绑定官方发送人的存档消息 {len(unknown_events)} 条")
    if missing_external:
        blockers.append(
            "供应商缺少官方 external_userid："
            + "、".join(contact.display_name for contact in missing_external)
        )
    if not active_suppliers:
        blockers.append("没有启用供应商角色")
    ok = not blockers
    detail = "恢复前对账通过" if ok else "恢复前对账未通过：" + "；".join(blockers)
    return ReceiveReconciliation(ok=ok, detail=detail, counts=counts, blockers=blockers)


def record_receive_channel_recovery(
    config: Config,
    run_date: date,
    detail: str,
    catchup_summary: dict[str, Any],
    reconciliation: ReceiveReconciliation,
) -> Path:
    now = datetime.now().isoformat(timespec="seconds")
    recovery_payload = {
        "status": "recovered",
        "date": run_date.isoformat(),
        "recovered_at": now,
        "detail": detail,
        "catchup_summary": catchup_summary,
        "reconciliation": asdict(reconciliation),
    }
    path = _recovery_path(config)
    _write_json(path, recovery_payload)

    failure = _read_json(_failure_path(config))
    if failure and failure.get("date") == run_date.isoformat():
        failure["status"] = "recovered"
        failure["recovery_required"] = False
        failure["recovered_at"] = now
        failure["recovery"] = recovery_payload
        _write_json(_failure_path(config), failure)
    return path


def _failure_path(config: Config) -> Path:
    return config.data_dir / "runtime" / FAILURE_STATE


def _recovery_path(config: Config) -> Path:
    return config.data_dir / "runtime" / RECOVERY_STATE


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"unreadable": str(exc), "path": str(path)}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _event_files_for_date(root: Path, run_date: date) -> list[Path]:
    if not root.exists():
        return []
    matched = []
    fallback = []
    for path in root.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            received_at = str(payload.get("received_at") or "")
            if received_at.startswith(run_date.isoformat()):
                matched.append(path)
        except Exception:
            fallback.append(path)
    return matched + fallback


def _infer_causes(detail: str, checks: dict[str, Any]) -> list[str]:
    content = detail.lower()
    causes = []
    if "wecom_msg_audit_sdk_lib" in content or "sdk" in content:
        causes.append("SDK 路径或 SDK 文件不可用")
    if "private key" in content or "private_key" in content or "私钥" in detail:
        causes.append("会话内容存档私钥配置异常")
    if "secret" in content:
        causes.append("会话内容存档 secret 或应用 secret 配置异常")
    if "过期" in detail or "stale" in content:
        causes.append("服务器定时收图任务没有按时刷新健康状态")
    if "没有找到官方收图健康状态" in detail:
        causes.append("服务器尚未生成官方收图健康状态")

    for check in checks.get("checks", []):
        if not check.get("ok"):
            name = check.get("name", "")
            if name == "message_archive_sdk":
                causes.append("SDK 文件检查未通过")
            elif name == "message_archive":
                causes.append("会话内容存档关键参数不完整")
            elif name == "supplier_external_ids":
                causes.append("供应商官方 external_userid 绑定不完整")
    seen = set()
    unique = []
    for cause in causes or ["官方收图通道异常，需查看健康状态和 doctor 结果"]:
        if cause not in seen:
            unique.append(cause)
            seen.add(cause)
    return unique
