import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable, List, Optional

from .config import Config
from .contact_roles import ContactRole, contact_from_external_payload, contact_id_from_external
from .desktop_plan import daily_question_text
from .models import Product, ProductStatus, Supplier
from .storage import Store


class WeComClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._access_token: Optional[str] = None
        self._access_token_expires_at = 0.0

    def official_api_configured(self) -> bool:
        return bool(self.config.wecom_corp_id and self.config.wecom_agent_secret)

    def message_archive_configured(self) -> bool:
        return bool(
            self.config.wecom_corp_id
            and self.config.wecom_msg_audit_secret
            and (
                self.config.wecom_msg_audit_private_key
                or self.config.wecom_msg_audit_private_key_path
            )
            and self.config.wecom_msg_audit_sdk_lib
            and Path(self.config.wecom_msg_audit_sdk_lib).exists()
        )

    def get_access_token(self, force_refresh: bool = False) -> str:
        """Return a cached WeCom access_token for official server APIs."""
        if self.config.wecom_dry_run:
            return "DRY_RUN_ACCESS_TOKEN"
        if not self.official_api_configured():
            raise RuntimeError("WECOM_CORP_ID and WECOM_AGENT_SECRET are required for official WeCom APIs")
        if not force_refresh and self._access_token and time.time() < self._access_token_expires_at:
            return self._access_token

        import requests

        response = requests.get(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={"corpid": self.config.wecom_corp_id, "corpsecret": self.config.wecom_agent_secret},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errcode") != 0:
            raise RuntimeError(f"WeCom gettoken error: {payload}")
        self._access_token = payload["access_token"]
        self._access_token_expires_at = time.time() + max(int(payload.get("expires_in", 7200)) - 300, 60)
        return self._access_token

    def post_official_api(self, path: str, payload: dict, label: str = "企业微信官方接口") -> Optional[dict]:
        if self.config.wecom_dry_run:
            print(f"[DRY-RUN] -> {label}: {json.dumps(payload, ensure_ascii=False)}")
            return None

        import requests

        token = self.get_access_token()
        response = requests.post(
            f"https://qyapi.weixin.qq.com{path}",
            params={"access_token": token},
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("errcode") != 0:
            raise RuntimeError(f"WeCom API error: {result}")
        return result

    def get_official_api(self, path: str, params: Optional[dict] = None) -> dict:
        if self.config.wecom_dry_run:
            return {"errcode": 0, "errmsg": "dry-run"}

        import requests

        token = self.get_access_token()
        query = {"access_token": token}
        if params:
            query.update(params)
        response = requests.get(
            f"https://qyapi.weixin.qq.com{path}",
            params=query,
            timeout=15,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("errcode") != 0:
            raise RuntimeError(f"WeCom API error: {result}")
        return result

    def get_agent_info(self) -> dict:
        if not self.config.wecom_agent_id:
            raise RuntimeError("WECOM_AGENT_ID is required for app info")
        return self.get_official_api("/cgi-bin/agent/get", {"agentid": self.config.wecom_agent_id})

    def send_app_text(self, touser: str, text: str) -> Optional[dict]:
        if not self.config.wecom_agent_id:
            raise RuntimeError("WECOM_AGENT_ID is required for app messages")
        return self.post_official_api(
            "/cgi-bin/message/send",
            {
                "touser": touser,
                "msgtype": "text",
                "agentid": int(self.config.wecom_agent_id),
                "text": {"content": text},
                "safe": 0,
            },
            label=f"应用消息({touser})",
        )

    def upload_media(self, path: Path, media_type: str = "file") -> Optional[str]:
        if self.config.wecom_dry_run:
            print(f"[DRY-RUN] upload {media_type}: {path}")
            return "DRY_RUN_MEDIA_ID"

        import requests

        token = self.get_access_token()
        with path.open("rb") as file_obj:
            response = requests.post(
                "https://qyapi.weixin.qq.com/cgi-bin/media/upload",
                params={"access_token": token, "type": media_type},
                files={"media": (path.name, file_obj)},
                timeout=30,
            )
        response.raise_for_status()
        result = response.json()
        if result.get("errcode") != 0:
            raise RuntimeError(f"WeCom media upload error: {result}")
        return result["media_id"]

    def send_app_file(self, touser: str, path: Path) -> Optional[dict]:
        if not self.config.wecom_agent_id:
            raise RuntimeError("WECOM_AGENT_ID is required for app messages")
        media_id = self.upload_media(path, media_type="file")
        return self.post_official_api(
            "/cgi-bin/message/send",
            {
                "touser": touser,
                "msgtype": "file",
                "agentid": int(self.config.wecom_agent_id),
                "file": {"media_id": media_id},
                "safe": 0,
            },
            label=f"应用文件({touser})",
        )

    def get_customer_contact_follow_users(self) -> List[str]:
        if self.config.wecom_dry_run:
            return []

        import requests

        token = self.get_access_token()
        response = requests.get(
            "https://qyapi.weixin.qq.com/cgi-bin/externalcontact/get_follow_user_list",
            params={"access_token": token},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errcode") != 0:
            raise RuntimeError(f"WeCom externalcontact follow user error: {payload}")
        return payload.get("follow_user", [])

    def list_external_contact_ids(self, owner_userid: str) -> List[str]:
        if self.config.wecom_dry_run:
            return []

        import requests

        token = self.get_access_token()
        response = requests.get(
            "https://qyapi.weixin.qq.com/cgi-bin/externalcontact/list",
            params={"access_token": token, "userid": owner_userid},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errcode") != 0:
            raise RuntimeError(f"WeCom externalcontact list error: {payload}")
        return payload.get("external_userid", [])

    def get_external_contact(self, external_userid: str, owner_userid: str = "") -> ContactRole:
        if self.config.wecom_dry_run:
            return ContactRole(
                contact_id=contact_id_from_external(external_userid),
                display_name=external_userid,
                source="wecom_external_contact",
                external_user_id=external_userid,
                owner_userid=owner_userid,
            )

        import requests

        token = self.get_access_token()
        response = requests.get(
            "https://qyapi.weixin.qq.com/cgi-bin/externalcontact/get",
            params={"access_token": token, "external_userid": external_userid},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("errcode") != 0:
            raise RuntimeError(f"WeCom externalcontact get error: {payload}")
        return contact_from_external_payload(payload, owner_userid=owner_userid)

    def sync_external_contacts(self, owner_userids: Optional[List[str]] = None) -> List[ContactRole]:
        owners = owner_userids or self.get_customer_contact_follow_users()
        contacts = {}
        for owner_userid in owners:
            for external_userid in self.list_external_contact_ids(owner_userid):
                contact = self.get_external_contact(external_userid, owner_userid=owner_userid)
                contacts[contact.contact_id] = contact
        return sorted(contacts.values(), key=lambda item: (item.display_name, item.contact_id))

    def send_text_to_supplier(self, supplier: Supplier, text: str) -> None:
        self.send_text(text, label=f"{supplier.name}({supplier.external_user_id})", prefix=f"@{supplier.name}\n")

    def send_text(self, text: str, label: str = "企业微信群机器人", prefix: str = "") -> Optional[dict]:
        if self.config.wecom_dry_run:
            print(f"[DRY-RUN] -> {label}: {text}")
            return None
        if self.config.wecom_webhook_url:
            import requests

            response = requests.post(
                self.config.wecom_webhook_url,
                json={"msgtype": "text", "text": {"content": f"{prefix}{text}"}},
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("errcode") != 0:
                raise RuntimeError(f"WeCom API error: {payload}")
            print(f"[WECOM] -> {label}: sent")
            return payload
        raise RuntimeError("No WeCom sending channel configured")

    def send_test_message(self, text: str) -> Optional[dict]:
        return self.send_text(text, label="企业微信群机器人测试")

    def send_daily_question(self, suppliers: Iterable[Supplier], date: str) -> None:
        for supplier in suppliers:
            self.send_text_to_supplier(supplier, daily_question_text(supplier, date))

    def request_samples(self, store: Store, product_ids: List[str]) -> None:
        grouped = defaultdict(list)
        for product_id in product_ids:
            product = store.get_product(product_id)
            if product:
                grouped[product.supplier_id].append(product)

        for supplier_id, products in grouped.items():
            supplier = store.get_supplier(supplier_id)
            if not supplier:
                continue
            lines = [
                "这几款选中了，麻烦按图发样，并把商品信息发我：",
                "货号/款号、颜色、尺码、面料/材质、价格、库存或起订量、发货周期。",
                "",
                "我下面把对应图片发你。",
            ]
            self.send_text_to_supplier(supplier, "\n".join(lines))
            store.update_status([product.product_id for product in products], ProductStatus.SAMPLE_REQUESTED)

    @staticmethod
    def parse_image_message(raw_payload: str):
        """Placeholder for real WeCom callback parsing.

        Keep payload handling explicit because enterprise WeChat deployments may
        use customer contact events, app messages, or group bots differently.
        """
        return json.loads(raw_payload)
