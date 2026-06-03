import os
from pathlib import Path


def load_env_file(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Config:
    def __init__(self) -> None:
        load_env_file()
        self.data_dir = Path(os.getenv("BOT_DATA_DIR", "data"))
        self.db_path = Path(os.getenv("BOT_DB_PATH", str(self.data_dir / "bot.sqlite3")))
        self.wecom_dry_run = os.getenv("WECOM_DRY_RUN", "1") != "0"
        self.wecom_webhook_url = os.getenv("WECOM_WEBHOOK_URL", "")
        self.wecom_corp_id = os.getenv("WECOM_CORP_ID", "")
        self.wecom_agent_id = os.getenv("WECOM_AGENT_ID", "")
        self.wecom_agent_secret = os.getenv("WECOM_AGENT_SECRET", "")
        self.wecom_msg_audit_secret = os.getenv("WECOM_MSG_AUDIT_SECRET", "")
        self.wecom_msg_audit_private_key = os.getenv("WECOM_MSG_AUDIT_PRIVATE_KEY", "")
        self.wecom_msg_audit_private_key_path = os.getenv("WECOM_MSG_AUDIT_PRIVATE_KEY_PATH", "")
        self.wecom_msg_audit_sdk_lib = os.getenv("WECOM_MSG_AUDIT_SDK_LIB", "")
        self.wecom_msg_audit_start_seq = int(os.getenv("WECOM_MSG_AUDIT_START_SEQ", "0") or "0")
        self.wecom_msg_audit_limit = int(os.getenv("WECOM_MSG_AUDIT_LIMIT", "100") or "100")
        self.wecom_msg_audit_timeout = int(os.getenv("WECOM_MSG_AUDIT_TIMEOUT", "30") or "30")
        self.wecom_msg_audit_proxy = os.getenv("WECOM_MSG_AUDIT_PROXY", "")
        self.wecom_msg_audit_proxy_password = os.getenv("WECOM_MSG_AUDIT_PROXY_PASSWORD", "")
        self.wecom_callback_token = os.getenv("WECOM_CALLBACK_TOKEN", "")
        self.wecom_callback_encoding_aes_key = os.getenv("WECOM_CALLBACK_ENCODING_AES_KEY", "")
        self.wecom_trusted_ip = os.getenv("WECOM_TRUSTED_IP", "")
        self.runtime_mode = os.getenv("RUNTIME_MODE", "desktop")
        self.vision_provider = os.getenv("VISION_PROVIDER", "auto")
        self.google_api_key = os.getenv("GOOGLE_API_KEY", "")
        self.google_vision_model = os.getenv("GOOGLE_VISION_MODEL", "gemini-2.5-flash")
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "")
        self.openai_vision_model = os.getenv("OPENAI_VISION_MODEL", "gpt-5-mini")
        self.supplier_reminder_time = os.getenv("SUPPLIER_REMINDER_TIME", os.getenv("REPORT_CUTOFF_TIME", "14:00"))
        self.report_finalize_time = os.getenv("REPORT_FINALIZE_TIME", "15:00")
        self.report_cutoff_time = self.supplier_reminder_time
        self.ops_table_cutoff_time = os.getenv("OPS_TABLE_CUTOFF_TIME", "18:00")
        self.supplier_image_quiet_minutes = int(os.getenv("SUPPLIER_IMAGE_QUIET_MINUTES", "10") or "10")
        self.selector_selection_quiet_minutes = int(os.getenv("SELECTOR_SELECTION_QUIET_MINUTES", "10") or "10")
        self.server_sync_target = os.getenv("SERVER_SYNC_TARGET", "")
        self.server_sync_ssh_key = os.getenv("SERVER_SYNC_SSH_KEY", "")
        self.alert_email_enabled = os.getenv("ALERT_EMAIL_ENABLED", "1") != "0"
        self.alert_email_to = os.getenv("ALERT_EMAIL_TO", "")
        self.alert_email_from = os.getenv("ALERT_EMAIL_FROM", os.getenv("SMTP_USERNAME", ""))
        self.smtp_host = os.getenv("SMTP_HOST", "")
        self.smtp_port = int(os.getenv("SMTP_PORT", "465") or "465")
        self.smtp_username = os.getenv("SMTP_USERNAME", "")
        self.smtp_password = os.getenv("SMTP_PASSWORD", "")
        self.smtp_use_ssl = os.getenv("SMTP_USE_SSL", "1") != "0"
        self.smtp_starttls = os.getenv("SMTP_STARTTLS", "0") == "1"
        self.smtp_timeout = int(os.getenv("SMTP_TIMEOUT", "15") or "15")
        self.alert_email_cooldown_seconds = int(os.getenv("ALERT_EMAIL_COOLDOWN_SECONDS", "3600") or "3600")


config = Config()
