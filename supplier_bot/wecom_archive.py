from __future__ import annotations

import base64
import ctypes
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Protocol

from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

from .config import Config
from .contact_roles import (
    ROLE_SELECTOR,
    ROLE_SUPPLIER,
    ContactRole,
    auto_bind_contacts_from_unknown_archive,
    contacts_to_suppliers,
    load_contact_roles,
    match_unbound_supplier_from_text,
    write_contact_roles,
)
from .desktop_outbox import STATUS_SENT, load_outbox
from .inbox_events import InboxEvent, queue_inbox_event
from .models import Supplier
from .storage import Store
from .workflow_state import load_daily_workflow


@dataclass
class ArchiveMessage:
    seq: int
    msgid: str
    sender: str
    recipients: List[str]
    msgtype: str
    msgtime: datetime
    text: str = ""
    sdkfileid: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class ArchivePollResult:
    checked: int = 0
    queued_events: int = 0
    downloaded_media: int = 0
    unknown_events: int = 0
    new_seq: int = 0
    errors: List[str] = field(default_factory=list)


class ArchiveAdapter(Protocol):
    def get_chat_data(self, seq: int, limit: int) -> List[dict]:
        ...

    def download_media(self, sdkfileid: str) -> bytes:
        ...


class WeComArchiveError(RuntimeError):
    pass


class Slice(ctypes.Structure):
    _fields_ = [("buf", ctypes.c_char_p), ("len", ctypes.c_int)]


class MediaData(ctypes.Structure):
    _fields_ = [
        ("outindexbuf", ctypes.c_char_p),
        ("out_len", ctypes.c_int),
        ("data", ctypes.c_char_p),
        ("data_len", ctypes.c_int),
        ("is_finish", ctypes.c_int),
    ]


class CtypesFinanceArchiveAdapter:
    """Thin wrapper around Tencent's 会话内容存档 C SDK.

    The official SDK is normally deployed on Linux with libWeWorkFinanceSdk_C.so.
    macOS development can still use the higher-level code paths through tests.
    """

    def __init__(self, config: Config) -> None:
        if not config.wecom_msg_audit_sdk_lib:
            raise WeComArchiveError("missing WECOM_MSG_AUDIT_SDK_LIB")
        lib_path = Path(config.wecom_msg_audit_sdk_lib)
        if not lib_path.exists():
            raise WeComArchiveError(f"WECOM_MSG_AUDIT_SDK_LIB not found: {lib_path}")
        self.config = config
        self.lib = ctypes.cdll.LoadLibrary(str(lib_path))
        self._configure_signatures()
        self.sdk = self.lib.NewSdk()
        ret = self.lib.Init(
            self.sdk,
            config.wecom_corp_id.encode("utf-8"),
            config.wecom_msg_audit_secret.encode("utf-8"),
        )
        if ret != 0:
            raise WeComArchiveError(f"WeCom archive Init failed: {ret}")

    def _configure_signatures(self) -> None:
        self.lib.NewSdk.restype = ctypes.c_void_p
        self.lib.Init.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        self.lib.Init.restype = ctypes.c_int
        self.lib.GetChatData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulonglong,
            ctypes.c_uint,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.POINTER(Slice),
        ]
        self.lib.GetChatData.restype = ctypes.c_int
        self.lib.DecryptData.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.POINTER(Slice)]
        self.lib.DecryptData.restype = ctypes.c_int
        self.lib.GetMediaData.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        self.lib.GetMediaData.restype = ctypes.c_int
        self.lib.NewMediaData.restype = ctypes.c_void_p
        self.lib.FreeMediaData.argtypes = [ctypes.c_void_p]
        self.lib.GetOutIndexBuf.argtypes = [ctypes.c_void_p]
        self.lib.GetOutIndexBuf.restype = ctypes.c_void_p
        self.lib.GetData.argtypes = [ctypes.c_void_p]
        self.lib.GetData.restype = ctypes.c_void_p
        self.lib.GetIndexLen.argtypes = [ctypes.c_void_p]
        self.lib.GetIndexLen.restype = ctypes.c_int
        self.lib.GetDataLen.argtypes = [ctypes.c_void_p]
        self.lib.GetDataLen.restype = ctypes.c_int
        self.lib.IsMediaDataFinish.argtypes = [ctypes.c_void_p]
        self.lib.IsMediaDataFinish.restype = ctypes.c_int

    def get_chat_data(self, seq: int, limit: int) -> List[dict]:
        encrypted = Slice()
        ret = self.lib.GetChatData(
            self.sdk,
            seq,
            limit,
            self.config.wecom_msg_audit_proxy.encode("utf-8"),
            self.config.wecom_msg_audit_proxy_password.encode("utf-8"),
            self.config.wecom_msg_audit_timeout,
            ctypes.byref(encrypted),
        )
        if ret != 0:
            raise WeComArchiveError(f"WeCom archive GetChatData failed: {ret}")
        payload = _slice_text(encrypted)
        data = json.loads(payload or "{}")
        if data.get("errcode") not in (None, 0):
            raise WeComArchiveError(f"WeCom archive GetChatData error: {data}")
        messages = []
        for item in data.get("chatdata", []):
            messages.append(self._decrypt_chat_item(item))
        return messages

    def _decrypt_chat_item(self, item: dict) -> dict:
        encrypt_key = decrypt_random_key(
            item.get("encrypt_random_key", ""),
            load_private_key_pem(self.config),
        )
        decrypted = Slice()
        ret = self.lib.DecryptData(
            encrypt_key.encode("utf-8"),
            item.get("encrypt_chat_msg", "").encode("utf-8"),
            ctypes.byref(decrypted),
        )
        if ret != 0:
            raise WeComArchiveError(f"WeCom archive DecryptData failed: {ret}")
        payload = json.loads(_slice_text(decrypted))
        payload.setdefault("seq", item.get("seq"))
        return payload

    def download_media(self, sdkfileid: str) -> bytes:
        index_buf = b""
        chunks: List[bytes] = []
        last_index_buf = None
        while True:
            media = self.lib.NewMediaData()
            try:
                ret = self.lib.GetMediaData(
                    self.sdk,
                    index_buf,
                    sdkfileid.encode("utf-8"),
                    self.config.wecom_msg_audit_proxy.encode("utf-8"),
                    self.config.wecom_msg_audit_proxy_password.encode("utf-8"),
                    self.config.wecom_msg_audit_timeout,
                    media,
                )
                if ret != 0:
                    raise WeComArchiveError(f"WeCom archive GetMediaData failed: {ret}")
                data_len = self.lib.GetDataLen(media)
                data = self.lib.GetData(media)
                if data and data_len:
                    chunks.append(ctypes.string_at(data, data_len))
                if self.lib.IsMediaDataFinish(media):
                    break
                index_len = self.lib.GetIndexLen(media)
                out_index = self.lib.GetOutIndexBuf(media)
                index_buf = ctypes.string_at(out_index, index_len) if out_index and index_len else b""
            finally:
                self.lib.FreeMediaData(media)
            if index_buf == last_index_buf:
                raise WeComArchiveError("WeCom archive GetMediaData made no progress")
            last_index_buf = index_buf
            if len(chunks) > 200:
                raise WeComArchiveError("WeCom archive GetMediaData exceeded chunk limit")
        return b"".join(chunks)


def _slice_text(value: Slice) -> str:
    if not value.buf or value.len <= 0:
        return ""
    return ctypes.string_at(value.buf, value.len).decode("utf-8")


def load_private_key_pem(config: Config) -> str:
    if config.wecom_msg_audit_private_key:
        return config.wecom_msg_audit_private_key.replace("\\n", "\n")
    if config.wecom_msg_audit_private_key_path:
        return Path(config.wecom_msg_audit_private_key_path).read_text(encoding="utf-8")
    raise WeComArchiveError("missing WECOM_MSG_AUDIT_PRIVATE_KEY or WECOM_MSG_AUDIT_PRIVATE_KEY_PATH")


def decrypt_random_key(encrypt_random_key: str, private_key_pem: str) -> str:
    if not encrypt_random_key:
        raise WeComArchiveError("empty encrypt_random_key")
    key = RSA.import_key(private_key_pem)
    cipher = PKCS1_v1_5.new(key)
    encrypted = base64.b64decode(encrypt_random_key)
    decrypted = cipher.decrypt(encrypted, None)
    if not decrypted:
        raise WeComArchiveError("RSA decrypt encrypt_random_key failed")
    return decrypted.decode("utf-8")


def poll_message_archive_into_inbox(
    config: Config,
    store: Store,
    data_dir: Path,
    run_date: datetime | str,
    adapter: Optional[ArchiveAdapter] = None,
    limit: Optional[int] = None,
) -> ArchivePollResult:
    result = ArchivePollResult()
    state_path = data_dir / "runtime" / "wecom_archive_state.json"
    state = _load_archive_state(state_path, config.wecom_msg_audit_start_seq)
    contacts_path = data_dir / "wecom_contacts.json"
    auto_bound = auto_bind_contacts_from_unknown_archive(contacts_path, data_dir)
    contacts = load_contact_roles(contacts_path)
    contact_suppliers = contacts_to_suppliers(contacts)
    contacts_by_external = _contact_lookup(contacts)
    for supplier in contact_suppliers:
        store.upsert_supplier(supplier)
    suppliers = contact_suppliers or store.list_suppliers()
    by_external = _supplier_lookup(suppliers)
    if auto_bound:
        contacts_by_external = _contact_lookup(contacts)
    active_date = run_date if isinstance(run_date, str) else run_date.date().isoformat()
    outbox_bound = _resolve_unknown_outbound_from_sent_outbox(data_dir, contacts_path, active_date)
    if outbox_bound:
        contacts = load_contact_roles(contacts_path)
        contact_suppliers = contacts_to_suppliers(contacts)
        contacts_by_external = _contact_lookup(contacts)
        for bound_supplier in contacts_to_suppliers(outbox_bound):
            store.upsert_supplier(bound_supplier)
        suppliers = contact_suppliers or store.list_suppliers()
        by_external = _supplier_lookup(suppliers)
    recovered_unknown_events = recover_bound_unknown_archive_events(data_dir, contacts, suppliers, active_date=active_date)
    result.queued_events += recovered_unknown_events
    client = adapter or CtypesFinanceArchiveAdapter(config)
    messages = sorted(
        client.get_chat_data(int(state["seq"]), limit or config.wecom_msg_audit_limit),
        key=lambda item: int(item.get("seq") or 0),
    )
    seen = set(state.get("seen_msgids", []))
    safe_seq = int(state["seq"])
    blocked_by_error = False
    outgoing_binder = _OutgoingDesktopBinder(data_dir, contacts, active_date)

    for raw in messages:
        try:
            message = normalize_archive_message(raw)
            result.checked += 1
            if message.msgid in seen:
                if not blocked_by_error:
                    safe_seq = max(safe_seq, message.seq)
                continue
            if message.msgtime.date().isoformat() != active_date:
                seen.add(message.msgid)
                if not blocked_by_error:
                    safe_seq = max(safe_seq, message.seq)
                continue
            outbound_bound = outgoing_binder.bind_message(contacts_path, message)
            if outbound_bound:
                contacts = load_contact_roles(contacts_path)
                contacts_by_external = _contact_lookup(contacts)
                for bound_supplier in contacts_to_suppliers([outbound_bound]):
                    store.upsert_supplier(bound_supplier)
                suppliers = store.list_suppliers()
                by_external = _supplier_lookup(suppliers)
            contact = contacts_by_external.get(message.sender)
            supplier = by_external.get(message.sender)
            event_role = _archive_message_role(data_dir, active_date, contact, supplier, message)
            if event_role == "selector":
                image_paths = []
                if message.sdkfileid:
                    image_path = _save_archive_media(
                        data_dir,
                        f"SELECTOR_{_safe_path_part(contact.contact_id if contact else message.sender)}",
                        message.msgtime,
                        message.msgid,
                        client.download_media(message.sdkfileid),
                    )
                    image_paths.append(str(image_path))
                    result.downloaded_media += 1
                if image_paths:
                    queue_inbox_event(
                        data_dir,
                        InboxEvent(
                            event_id=f"wecom-archive-{message.msgid}",
                            supplier_id=contact.contact_id if contact else message.sender,
                            contact_id=contact.contact_id if contact else message.sender,
                            contact_name=contact.display_name if contact else message.sender,
                            role="selector",
                            received_at=message.msgtime.isoformat(timespec="seconds"),
                            image_paths=image_paths,
                            text=message.text,
                            source="wecom_archive",
                        ),
                    )
                    result.queued_events += 1
                seen.add(message.msgid)
                if not blocked_by_error:
                    safe_seq = max(safe_seq, message.seq)
                continue
            if not supplier:
                bound_contact = _bind_unknown_sender_from_text(contacts_path, contacts, message)
                if bound_contact:
                    contact = bound_contact
                    contacts_by_external = _contact_lookup(contacts)
                    for bound_supplier in contacts_to_suppliers([bound_contact]):
                        store.upsert_supplier(bound_supplier)
                    suppliers = contacts_to_suppliers(contacts) or store.list_suppliers()
                    by_external = _supplier_lookup(suppliers)
                    supplier = by_external.get(message.sender)
                    event_role = _archive_message_role(data_dir, active_date, contact, supplier, message)
                if supplier:
                    # Continue into the normal supplier handling below for this same message.
                    pass
            if not supplier:
                image_paths = []
                if message.sdkfileid:
                    image_path = _save_archive_media(
                        data_dir,
                        f"UNKNOWN_{_safe_path_part(message.sender)}",
                        message.msgtime,
                        message.msgid,
                        client.download_media(message.sdkfileid),
                    )
                    image_paths.append(str(image_path))
                    result.downloaded_media += 1
                if image_paths or message.text:
                    _write_unknown_archive_message(data_dir, message, image_paths)
                    result.unknown_events += 1
                seen.add(message.msgid)
                if not blocked_by_error:
                    safe_seq = max(safe_seq, message.seq)
                continue
            image_paths = []
            if message.sdkfileid:
                image_path = _save_archive_media(
                    data_dir,
                    supplier.supplier_id,
                    message.msgtime,
                    message.msgid,
                    client.download_media(message.sdkfileid),
                )
                image_paths.append(str(image_path))
                result.downloaded_media += 1
            if image_paths or message.text:
                queue_inbox_event(
                    data_dir,
                    InboxEvent(
                        event_id=f"wecom-archive-{message.msgid}",
                        supplier_id=supplier.supplier_id,
                        received_at=message.msgtime.isoformat(timespec="seconds"),
                        image_paths=image_paths,
                        text=message.text,
                        source="wecom_archive",
                        role="supplier",
                    ),
                )
                result.queued_events += 1
            seen.add(message.msgid)
            if not blocked_by_error:
                safe_seq = max(safe_seq, message.seq)
        except Exception as exc:
            blocked_by_error = True
            result.errors.append(f"{raw.get('msgid', 'unknown')}: {exc}")

    result.new_seq = safe_seq
    state["seq"] = safe_seq
    state["seen_msgids"] = sorted(seen)[-2000:]
    _write_archive_state(state_path, state)
    contacts = load_contact_roles(contacts_path)
    outbox_bound = _resolve_unknown_outbound_from_sent_outbox(data_dir, contacts_path, active_date)
    if outbox_bound:
        contacts = load_contact_roles(contacts_path)
    result.queued_events += recover_bound_unknown_archive_events(
        data_dir,
        contacts,
        contacts_to_suppliers(contacts) or store.list_suppliers(),
        active_date=active_date,
    )
    return result


def normalize_archive_message(raw: dict) -> ArchiveMessage:
    msgtype = raw.get("msgtype", "")
    text = raw.get("text", {}).get("content", "") if isinstance(raw.get("text"), dict) else ""
    sdkfileid = ""
    if msgtype == "image":
        image = raw.get("image") or {}
        sdkfileid = image.get("sdkfileid", "")
    return ArchiveMessage(
        seq=int(raw.get("seq") or 0),
        msgid=str(raw.get("msgid") or raw.get("msg_id") or ""),
        sender=str(raw.get("from") or raw.get("from_user") or ""),
        recipients=[str(item) for item in raw.get("tolist", [])],
        msgtype=msgtype,
        msgtime=parse_msgtime(raw.get("msgtime")),
        text=text,
        sdkfileid=sdkfileid,
        raw=raw,
    )


def parse_msgtime(value: object) -> datetime:
    if value is None:
        return datetime.now()
    number = int(value)
    if number > 10_000_000_000:
        number = number // 1000
    return datetime.fromtimestamp(number)


def _bind_unknown_sender_from_text(
    contacts_path: Path,
    contacts: List[ContactRole],
    message: ArchiveMessage,
) -> ContactRole | None:
    if not message.text:
        return None
    contact = match_unbound_supplier_from_text(contacts, message.text)
    if not contact:
        return None
    contact.external_user_id = message.sender
    contact.notes = _append_binding_note(contact.notes, f"自动绑定官方 external_userid: {message.sender}")
    write_contact_roles(contacts_path, contacts)
    return contact


def _supplier_lookup(suppliers: Iterable[Supplier]) -> dict[str, Supplier]:
    lookup = {}
    for supplier in suppliers:
        for key in {supplier.external_user_id, supplier.supplier_id, supplier.name, supplier.contact_name}:
            if key:
                lookup[key] = supplier
    return lookup


def _contact_lookup(contacts: Iterable[ContactRole]) -> dict[str, ContactRole]:
    lookup = {}
    for contact in contacts:
        if not contact.enabled:
            continue
        for key in {contact.external_user_id, contact.contact_id, contact.display_name, contact.search_text}:
            if key:
                lookup[key] = contact
    return lookup


def _archive_message_role(
    data_dir: Path,
    active_date: str,
    contact: ContactRole | None,
    supplier: Supplier | None,
    message: ArchiveMessage,
) -> str:
    if not contact:
        return "supplier" if supplier else "unknown"
    roles = set(contact.roles)
    workflow = load_daily_workflow(data_dir / "tasks" / active_date / "daily_workflow.json")
    supplier_flow = None
    if workflow:
        supplier_flow = next((item for item in workflow.suppliers if item.supplier_id == contact.contact_id), None)
    if ROLE_SUPPLIER in roles and supplier_flow and supplier_flow.sample_requested_at:
        try:
            if message.msgtime > datetime.fromisoformat(supplier_flow.sample_requested_at):
                return "supplier"
        except ValueError:
            return "supplier"
    if ROLE_SELECTOR in roles and workflow and workflow.report_path and message.sdkfileid:
        return "selector"
    if ROLE_SUPPLIER in roles or supplier:
        return "supplier"
    if ROLE_SELECTOR in roles and message.sdkfileid:
        return "selector"
    return "unknown"


def _save_archive_media(data_dir: Path, supplier_id: str, received_at: datetime, msgid: str, content: bytes) -> Path:
    target_dir = data_dir / "archive_media" / received_at.strftime("%Y-%m-%d") / supplier_id
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_msgid = _safe_path_part(msgid)
    path = target_dir / f"{safe_msgid or received_at.strftime('%H%M%S')}.jpg"
    path.write_bytes(content)
    return path


def _write_unknown_archive_message(data_dir: Path, message: ArchiveMessage, image_paths: List[str]) -> Path:
    unknown_dir = data_dir / "archive_unknown" / message.msgtime.strftime("%Y-%m-%d")
    unknown_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "seq": message.seq,
        "msgid": message.msgid,
        "sender": message.sender,
        "recipients": message.recipients,
        "msgtype": message.msgtype,
        "msgtime": message.msgtime.isoformat(timespec="seconds"),
        "text": message.text,
        "image_paths": image_paths,
    }
    path = unknown_dir / f"{_safe_path_part(message.msgid) or message.seq}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def recover_bound_unknown_archive_events(
    data_dir: Path,
    contacts: Iterable[ContactRole],
    suppliers: Iterable[Supplier],
    active_date: str | None = None,
) -> int:
    contacts_by_external = _contact_lookup(contacts)
    suppliers_by_external = _supplier_lookup(suppliers)
    recovered = 0
    unknown_root = data_dir / "archive_unknown"
    if not unknown_root.exists():
        return 0
    for path in sorted(unknown_root.glob("*/*.json")):
        if active_date and path.parent.name != active_date:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        sender = str(payload.get("sender") or "")
        contact = contacts_by_external.get(sender)
        supplier = suppliers_by_external.get(sender)
        if not supplier and contact and ROLE_SUPPLIER in contact.roles:
            supplier = suppliers_by_external.get(contact.external_user_id) or suppliers_by_external.get(contact.contact_id)
        if not supplier:
            continue
        image_paths = [str(item) for item in payload.get("image_paths", []) if item]
        text = str(payload.get("text") or "")
        if not image_paths and not text.strip():
            continue
        msgid = _safe_path_part(str(payload.get("msgid") or path.stem))
        event_id = f"wecom-archive-recovered-{msgid}"
        if _inbox_event_exists(data_dir, event_id):
            _move_resolved_unknown_archive(path, data_dir)
            continue
        queue_inbox_event(
            data_dir,
            InboxEvent(
                event_id=event_id,
                supplier_id=supplier.supplier_id,
                contact_id=contact.contact_id if contact else supplier.supplier_id,
                contact_name=contact.display_name if contact else supplier.name,
                role="supplier",
                received_at=str(payload.get("msgtime") or datetime.now().isoformat(timespec="seconds")),
                image_paths=image_paths,
                text=text,
                source="wecom_archive_recovered",
            ),
        )
        _move_resolved_unknown_archive(path, data_dir)
        recovered += 1
    return recovered


def _inbox_event_exists(data_dir: Path, event_id: str) -> bool:
    for folder in ("pending", "processed", "failed"):
        if (data_dir / "inbox_events" / folder / f"{event_id}.json").exists():
            return True
        if list((data_dir / "inbox_events" / folder).glob(f"{event_id}-*.json")):
            return True
    return False


class _OutgoingDesktopBinder:
    def __init__(self, data_dir: Path, contacts: Iterable[ContactRole], active_date: str) -> None:
        self.contacts = list(contacts)
        self.active_date = active_date
        self.bound_external_ids = {contact.external_user_id for contact in self.contacts if contact.external_user_id}
        self.tasks = self._load_tasks(data_dir)
        self.index = 0

    def _load_tasks(self, data_dir: Path) -> List[dict]:
        return _sent_supplier_outbox_tasks(data_dir, self.contacts, self.active_date, unbound_only=True)

    def bind_message(self, contacts_path: Path, message: ArchiveMessage) -> ContactRole | None:
        if message.msgtype != "text" or not message.text or not message.recipients:
            return None
        recipient = message.recipients[0]
        if recipient in self.bound_external_ids:
            return None
        while self.index < len(self.tasks):
            task = self.tasks[self.index]
            self.index += 1
            if task["message"] != message.text:
                continue
            contacts = load_contact_roles(contacts_path)
            contact = next((item for item in contacts if item.contact_id == task["contact_id"]), None)
            if not contact or contact.external_user_id:
                continue
            contact.external_user_id = recipient
            contact.notes = _append_binding_note(contact.notes, f"根据桌面已发送要款消息自动绑定官方 external_userid: {recipient}")
            write_contact_roles(contacts_path, contacts)
            self.bound_external_ids.add(recipient)
            return contact
        return None


def _resolve_unknown_outbound_from_sent_outbox(data_dir: Path, contacts_path: Path, active_date: str) -> List[ContactRole]:
    contacts = load_contact_roles(contacts_path)
    tasks = _sent_supplier_outbox_tasks(data_dir, contacts, active_date, unbound_only=False)
    messages = _unknown_outbound_messages(data_dir, active_date)
    if not tasks or not messages:
        return []

    contacts_by_id = {contact.contact_id: contact for contact in contacts}
    bound_external_ids = {contact.external_user_id for contact in contacts if contact.external_user_id}
    changed: List[ContactRole] = []
    used_paths: set[Path] = set()

    for task in tasks:
        contact = contacts_by_id.get(task["contact_id"])
        if not contact:
            continue
        candidate = _next_outbound_candidate(task, messages, used_paths, bound_external_ids, contact.external_user_id)
        if not candidate:
            continue
        used_paths.add(candidate["path"])
        recipient = candidate["recipient"]
        if contact.external_user_id:
            _move_resolved_unknown_archive(candidate["path"], data_dir)
            continue
        contact.external_user_id = recipient
        contact.notes = _append_binding_note(
            contact.notes,
            f"根据历史桌面已发送要款消息自动绑定官方 external_userid: {recipient}",
        )
        bound_external_ids.add(recipient)
        changed.append(contact)
        _move_resolved_unknown_archive(candidate["path"], data_dir)

    if changed:
        write_contact_roles(contacts_path, contacts)
    return changed


def _sent_supplier_outbox_tasks(
    data_dir: Path,
    contacts: Iterable[ContactRole],
    active_date: str,
    unbound_only: bool,
) -> List[dict]:
    path = data_dir / "tasks" / active_date / "desktop_outbox.json"
    if not path.exists():
        return []
    contacts_by_id = {contact.contact_id: contact for contact in contacts}
    tasks = []
    for index, task in enumerate(load_outbox(path)):
        if task.status != STATUS_SENT:
            continue
        if task.kind not in {"ask_supplier", "remind_supplier"}:
            continue
        supplier_id = task.metadata.get("supplier_id")
        contact = contacts_by_id.get(supplier_id)
        if not contact or ROLE_SUPPLIER not in contact.roles:
            continue
        if unbound_only and contact.external_user_id:
            continue
        tasks.append(
            {
                "index": index,
                "message": task.message,
                "contact_id": supplier_id,
                "external_user_id": contact.external_user_id,
                "sent_at": _parse_optional_datetime(task.sent_at),
                "created_at": _parse_optional_datetime(task.created_at),
            }
        )
    return tasks


def _unknown_outbound_messages(data_dir: Path, active_date: str) -> List[dict]:
    unknown_dir = data_dir / "archive_unknown" / active_date
    if not unknown_dir.exists():
        return []
    messages = []
    for path in sorted(unknown_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("msgtype") != "text":
            continue
        text = str(payload.get("text") or "")
        if not text:
            continue
        recipients = [str(item) for item in payload.get("recipients", []) if _looks_like_external_user_id(str(item))]
        if not recipients:
            continue
        messages.append(
            {
                "path": path,
                "seq": int(payload.get("seq") or 0),
                "msgid": str(payload.get("msgid") or path.stem),
                "msgtime": _parse_optional_datetime(str(payload.get("msgtime") or "")),
                "text": text,
                "recipient": recipients[0],
            }
        )
    return sorted(messages, key=lambda item: (item["seq"], item["msgtime"] or datetime.min, item["msgid"]))


def _next_outbound_candidate(
    task: dict,
    messages: List[dict],
    used_paths: set[Path],
    bound_external_ids: set[str],
    expected_external_id: str,
) -> dict | None:
    for message in messages:
        if message["path"] in used_paths:
            continue
        if message["text"] != task["message"]:
            continue
        recipient = message["recipient"]
        if expected_external_id:
            if recipient == expected_external_id:
                return message
            continue
        if recipient in bound_external_ids:
            continue
        if not _outbound_times_close(task, message):
            continue
        return message
    return None


def _outbound_times_close(task: dict, message: dict) -> bool:
    message_time = message.get("msgtime")
    if not message_time:
        return True
    task_time = task.get("sent_at") or task.get("created_at")
    if not task_time:
        return True
    return abs((message_time - task_time).total_seconds()) <= 600


def _parse_optional_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _looks_like_external_user_id(value: str) -> bool:
    return value.startswith(("wm", "wo"))


def _move_resolved_unknown_archive(path: Path, data_dir: Path) -> None:
    if not path.exists():
        return
    try:
        date_dir = path.parent.name
        target_dir = data_dir / "archive_unknown_resolved" / date_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        if target.exists():
            path.unlink()
        else:
            path.rename(target)
    except OSError:
        return


def _append_binding_note(notes: str, note: str) -> str:
    return notes if note in notes else "; ".join(part for part in [notes, note] if part)


def _safe_path_part(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)


def _load_archive_state(path: Path, default_seq: int) -> dict:
    if not path.exists():
        return {"seq": default_seq, "seen_msgids": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.setdefault("seq", default_seq)
    payload.setdefault("seen_msgids", [])
    return payload


def _write_archive_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
