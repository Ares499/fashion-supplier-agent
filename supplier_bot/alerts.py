from __future__ import annotations

import hashlib
import json
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from .config import Config


@dataclass
class AlertResult:
    sent: bool
    skipped: bool
    detail: str


def _state_path(config: Config) -> Path:
    return config.data_dir / "runtime" / "alert_email_state.json"


def _load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _fingerprint(category: str, subject: str, body: str) -> str:
    digest = hashlib.sha256(f"{category}\n{subject}\n{body}".encode("utf-8")).hexdigest()
    return digest[:24]


def _email_config_missing(config: Config) -> list[str]:
    missing = []
    if not config.alert_email_to:
        missing.append("ALERT_EMAIL_TO")
    if not config.alert_email_from:
        missing.append("ALERT_EMAIL_FROM/SMTP_USERNAME")
    if not config.smtp_host:
        missing.append("SMTP_HOST")
    if not config.smtp_username:
        missing.append("SMTP_USERNAME")
    if not config.smtp_password:
        missing.append("SMTP_PASSWORD")
    return missing


def send_alert_email(
    config: Config,
    subject: str,
    body: str,
    category: str = "general",
    now: datetime | None = None,
) -> AlertResult:
    if not config.alert_email_enabled:
        return AlertResult(sent=False, skipped=True, detail="邮件报警已关闭")

    missing = _email_config_missing(config)
    if missing:
        return AlertResult(sent=False, skipped=True, detail="邮件报警未配置：" + ", ".join(missing))

    now = now or datetime.now()
    path = _state_path(config)
    state = _load_state(path)
    key = _fingerprint(category, subject, body)
    last_sent_raw = state.get(key, {}).get("sent_at")
    if last_sent_raw:
        try:
            last_sent_at = datetime.fromisoformat(last_sent_raw)
            if (now - last_sent_at).total_seconds() < config.alert_email_cooldown_seconds:
                return AlertResult(sent=False, skipped=True, detail="同类报警冷却中，已跳过重复邮件")
        except Exception:
            pass

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.alert_email_from
    message["To"] = config.alert_email_to
    message.set_content(body)

    try:
        if config.smtp_use_ssl:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=config.smtp_timeout, context=context) as smtp:
                smtp.login(config.smtp_username, config.smtp_password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=config.smtp_timeout) as smtp:
                if config.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                smtp.login(config.smtp_username, config.smtp_password)
                smtp.send_message(message)
    except Exception as exc:
        return AlertResult(sent=False, skipped=False, detail=f"邮件发送失败：{exc}")

    state[key] = {
        "category": category,
        "subject": subject,
        "sent_at": now.isoformat(timespec="seconds"),
    }
    _write_state(path, state)
    return AlertResult(sent=True, skipped=False, detail=f"邮件报警已发送到 {config.alert_email_to}")


def send_receive_channel_failure_alert(
    config: Config,
    detail: str,
    checked_at: datetime | None = None,
    diagnostics: dict | None = None,
) -> AlertResult:
    checked_at = checked_at or datetime.now()
    diagnostics_lines = []
    if diagnostics:
        causes = diagnostics.get("suspected_causes") or []
        if causes:
            diagnostics_lines.extend(["", "自动诊断：", *[f"- {cause}" for cause in causes]])
        doctor = diagnostics.get("doctor", {})
        failed_checks = [item for item in doctor.get("checks", []) if not item.get("ok")]
        if failed_checks:
            diagnostics_lines.extend(
                ["", "未通过检查："]
                + [f"- {item.get('name')}: {item.get('detail')}" for item in failed_checks[:8]]
            )
    subject = "服装选款AI助手已暂停：官方收图通道异常"
    body = "\n".join(
        [
            "服装选款AI助手已自动暂停。",
            "",
            f"原因：官方会话内容存档 SDK/收图通道异常",
            f"错误：{detail}",
            f"时间：{checked_at.isoformat(timespec='seconds')}",
            "",
            "系统已停止自动对外动作：",
            "- 不再问供应商",
            "- 不再催供应商",
            "- 不再生成或发送当天报表",
            "- 不再推进样品请求",
            *diagnostics_lines,
            "",
            "请检查服务器 SDK 配置和收图任务，修复后重新启动桌面 agent。",
        ]
    )
    return send_alert_email(config, subject, body, category="receive_channel_failure", now=checked_at)
