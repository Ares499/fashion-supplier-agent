from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List

from .config import Config
from .contact_roles import (
    ROLE_CONFIRMER,
    ROLE_OPERATOR,
    ROLE_SELECTOR,
    ROLE_SUPPLIER,
    contacts_by_role,
    load_contact_roles,
    supplier_contacts_missing_external_ids,
)
from .desktop_receiver import check_desktop_capture
from .desktop_sender import check_desktop_automation
from .wecom import WeComClient


@dataclass
class HealthCheck:
    name: str
    ok: bool
    detail: str


def run_health_checks(config: Config, project_root: Path | None = None, live_api: bool = False) -> List[HealthCheck]:
    root = project_root or Path.cwd()
    client = WeComClient(config)
    contacts = load_contact_roles(config.data_dir / "wecom_contacts.json")
    suppliers = contacts_by_role(contacts, ROLE_SUPPLIER)
    selectors = contacts_by_role(contacts, ROLE_SELECTOR)
    operators = contacts_by_role(contacts, ROLE_OPERATOR)
    confirmers = contacts_by_role(contacts, ROLE_CONFIRMER)
    active_names = [contact.display_name for contact in contacts if contact.enabled]
    supplier_names = [contact.display_name for contact in suppliers]
    selector_names = [contact.display_name for contact in selectors]
    operator_names = [contact.display_name for contact in operators]
    confirmer_names = [contact.display_name for contact in confirmers]
    missing_supplier_external_ids = supplier_contacts_missing_external_ids(contacts)
    fuzzy_title_pairs = _fuzzy_title_pairs(active_names)
    checks = [
        HealthCheck("data_dir", config.data_dir.exists(), str(config.data_dir)),
        HealthCheck("database_path", config.db_path.parent.exists(), str(config.db_path)),
        HealthCheck("start_script", os.access(root / "start_supplier_bot.command", os.X_OK), str(root / "start_supplier_bot.command")),
        HealthCheck("runtime_mode", config.runtime_mode in {"desktop", "official", "hybrid"}, config.runtime_mode),
        HealthCheck("official_api", client.official_api_configured(), "企业微信自建应用参数已配置" if client.official_api_configured() else "缺 WECOM_CORP_ID/WECOM_AGENT_SECRET"),
        HealthCheck("callback_config", bool(config.wecom_callback_token and config.wecom_callback_encoding_aes_key), "接收消息服务器 Token/AESKey 已配置" if config.wecom_callback_token and config.wecom_callback_encoding_aes_key else "未配置接收消息服务器 Token/AESKey"),
        HealthCheck("trusted_ip_hint", bool(config.wecom_trusted_ip), config.wecom_trusted_ip or "未记录企业可信 IP"),
        HealthCheck("message_archive", client.message_archive_configured(), "会话内容存档 SDK 可用" if client.message_archive_configured() else "会话内容存档不可用：缺 corp/secret/private key/SDK 文件之一"),
        HealthCheck("message_archive_sdk", bool(config.wecom_msg_audit_sdk_lib and Path(config.wecom_msg_audit_sdk_lib).exists()), f"SDK: {config.wecom_msg_audit_sdk_lib}" if config.wecom_msg_audit_sdk_lib else "未配置 WECOM_MSG_AUDIT_SDK_LIB"),
        HealthCheck("contacts", bool(contacts), f"{len(contacts)} contacts"),
        HealthCheck("supplier_roles", bool(suppliers), f"{len(suppliers)} suppliers: {', '.join(supplier_names) or 'none'}"),
        HealthCheck(
            "supplier_external_ids",
            not missing_supplier_external_ids,
            "所有启用供应商已绑定官方 external_userid"
            if not missing_supplier_external_ids
            else "缺少官方 external_userid: " + ", ".join(contact.display_name for contact in missing_supplier_external_ids),
        ),
        HealthCheck("selector_roles", bool(selectors), f"{len(selectors)} selectors: {', '.join(selector_names) or 'none'}"),
        HealthCheck("operator_roles", bool(operators), f"{len(operators)} operators: {', '.join(operator_names) or 'none'}"),
        HealthCheck("confirmer_roles", bool(confirmers or operators), f"{len(confirmers)} confirmers: {', '.join(confirmer_names) or 'none'}"),
        HealthCheck(
            "desktop_title_guard",
            True,
            "严格校验当前会话标题；相近名称：" + "; ".join(f"{a}/{b}" for a, b in fuzzy_title_pairs)
            if fuzzy_title_pairs
            else "严格校验当前会话标题；未发现相近联系人名",
        ),
    ]
    if config.runtime_mode == "desktop":
        desktop = check_desktop_automation()
        checks.append(HealthCheck("desktop_automation", desktop.ok, desktop.detail))
        capture = check_desktop_capture()
        checks.append(HealthCheck("desktop_capture", capture.ok, capture.detail))
    if live_api and client.official_api_configured():
        try:
            agent = client.get_agent_info()
            checks.append(HealthCheck("official_api_live", True, f"agent: {agent.get('name') or config.wecom_agent_id}"))
        except Exception as exc:
            checks.append(HealthCheck("official_api_live", False, str(exc)))
    return checks


def _fuzzy_title_pairs(names: List[str]) -> List[tuple[str, str]]:
    pairs = []
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            if not left or not right:
                continue
            if left in right or right in left:
                pairs.append((left, right))
    return pairs


def health_payload(checks: List[HealthCheck]) -> dict:
    runtime_mode = next((check.detail for check in checks if check.name == "runtime_mode"), "desktop")
    advisory = {"trusted_ip_hint", "callback_config", "official_api_live"}
    if runtime_mode == "desktop":
        advisory.update({"official_api", "message_archive", "message_archive_sdk"})
    return {
        "ok": all(check.ok for check in checks if check.name not in advisory),
        "checks": [asdict(check) for check in checks],
    }
