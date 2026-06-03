from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable, List, Sequence
from urllib.parse import urlparse

from .models import Supplier


ROLE_SUPPLIER = "supplier"
ROLE_SELECTOR = "selector"
ROLE_OPERATOR = "operator"
ROLE_CONFIRMER = "confirmer"
KNOWN_ROLES = (ROLE_SUPPLIER, ROLE_SELECTOR, ROLE_OPERATOR, ROLE_CONFIRMER)


@dataclass
class ContactRole:
    contact_id: str
    display_name: str
    source: str = "manual"
    external_user_id: str = ""
    owner_userid: str = ""
    corp_name: str = ""
    roles: List[str] = field(default_factory=list)
    enabled: bool = True
    search_text: str = ""
    sample_address: str = ""
    main_categories: List[str] = field(default_factory=list)
    notes: str = ""

    def normalized_roles(self) -> List[str]:
        return sorted({role for role in self.roles if role in KNOWN_ROLES})


def contact_id_from_external(external_user_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", external_user_id).strip("_")
    return cleaned or "CONTACT"


def load_contact_roles(path: Path) -> List[ContactRole]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_contacts = payload["contacts"] if isinstance(payload, dict) else payload
    contacts = []
    for item in raw_contacts:
        contact = ContactRole(**item)
        contact.roles = contact.normalized_roles()
        contacts.append(contact)
    return contacts


def write_contact_roles(path: Path, contacts: Sequence[ContactRole]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "contacts": [asdict(contact) | {"roles": contact.normalized_roles()} for contact in contacts],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def merge_contact_roles(existing: Sequence[ContactRole], incoming: Iterable[ContactRole]) -> List[ContactRole]:
    merged = {contact.contact_id: contact for contact in existing}
    for contact in incoming:
        previous = merged.get(contact.contact_id) or _match_existing_contact_by_name(merged.values(), contact)
        if previous:
            contact.contact_id = previous.contact_id
            contact.roles = previous.roles
            contact.enabled = previous.enabled
            contact.search_text = previous.search_text or contact.search_text
            contact.sample_address = previous.sample_address
            contact.main_categories = previous.main_categories
            contact.notes = previous.notes
        merged[contact.contact_id] = contact
    return sorted(merged.values(), key=lambda item: (item.display_name, item.contact_id))


def contacts_to_suppliers(contacts: Sequence[ContactRole]) -> List[Supplier]:
    suppliers = []
    for contact in contacts:
        if not contact.enabled or ROLE_SUPPLIER not in contact.roles:
            continue
        supplier_id = contact.contact_id
        suppliers.append(
            Supplier(
                supplier_id=supplier_id,
                name=contact.display_name,
                contact_name=contact.display_name,
                external_user_id=contact.external_user_id or contact.contact_id,
                main_categories=contact.main_categories,
                sample_address=contact.sample_address,
                send_frequency="daily",
                paused=False,
            )
        )
    return suppliers


def _match_existing_contact_by_name(existing: Iterable[ContactRole], incoming: ContactRole) -> ContactRole | None:
    incoming_aliases = {_normalize_match_text(alias) for alias in _contact_aliases(incoming)}
    incoming_aliases.add(_normalize_match_text(incoming.display_name))
    incoming_aliases.add(_normalize_match_text(incoming.search_text))
    incoming_aliases.discard("")
    matches = []
    for contact in existing:
        aliases = {_normalize_match_text(alias) for alias in _contact_aliases(contact)}
        aliases.add(_normalize_match_text(contact.display_name))
        aliases.add(_normalize_match_text(contact.search_text))
        aliases.discard("")
        if aliases & incoming_aliases:
            matches.append(contact)
    return matches[0] if len(matches) == 1 else None


def contacts_by_role(contacts: Sequence[ContactRole], role: str) -> List[ContactRole]:
    return [contact for contact in contacts if contact.enabled and role in contact.roles]


def supplier_contacts_missing_external_ids(contacts: Sequence[ContactRole]) -> List[ContactRole]:
    return [
        contact
        for contact in contacts
        if contact.enabled and ROLE_SUPPLIER in contact.roles and not contact.external_user_id.strip()
    ]


def auto_bind_contacts_from_unknown_archive(contacts_path: Path, data_dir: Path) -> List[ContactRole]:
    contacts = load_contact_roles(contacts_path)
    changed = auto_bind_contacts_from_unknown_messages(contacts, _load_unknown_archive_messages(data_dir))
    if changed:
        write_contact_roles(contacts_path, contacts)
    return changed


def auto_bind_contacts_from_unknown_messages(contacts: Sequence[ContactRole], messages: Iterable[dict]) -> List[ContactRole]:
    changed: List[ContactRole] = []
    for message in messages:
        sender = str(message.get("sender") or "").strip()
        text = str(message.get("text") or "")
        if not sender or not text:
            continue
        contact = match_unbound_supplier_from_text(contacts, text)
        if not contact or contact.external_user_id:
            continue
        contact.external_user_id = sender
        contact.notes = _append_note(contact.notes, f"自动绑定官方 external_userid: {sender}")
        changed.append(contact)
    return changed


def match_unbound_supplier_from_text(contacts: Sequence[ContactRole], text: str) -> ContactRole | None:
    candidates = supplier_contacts_missing_external_ids(contacts)
    matches = [contact for contact in candidates if _contact_mentions(contact, text)]
    return matches[0] if len(matches) == 1 else None


def contact_from_external_payload(item: dict, owner_userid: str = "") -> ContactRole:
    external = item.get("external_contact", item)
    follow_info = item.get("follow_info", {})
    external_user_id = external.get("external_userid", "")
    display_name = follow_info.get("remark") or external.get("name") or external_user_id
    corp_name = follow_info.get("remark_corp_name") or external.get("corp_full_name") or external.get("corp_name") or ""
    return ContactRole(
        contact_id=contact_id_from_external(external_user_id),
        display_name=display_name,
        source="wecom_external_contact",
        external_user_id=external_user_id,
        owner_userid=owner_userid or follow_info.get("userid", ""),
        corp_name=corp_name,
        search_text=display_name,
    )


def render_role_manager_html(contacts: Sequence[ContactRole]) -> str:
    rows = []
    for index, contact in enumerate(contacts):
        checked = {role: "checked" if role in contact.roles else "" for role in KNOWN_ROLES}
        enabled = "checked" if contact.enabled else ""
        rows.append(
            f"""
            <tr data-contact-id="{contact.contact_id}">
              <td><input type="checkbox" data-field="enabled" {enabled}></td>
              <td>
                <strong>{_escape(contact.display_name)}</strong>
                <div class="muted">{_escape(contact.corp_name or contact.external_user_id or contact.contact_id)}</div>
              </td>
              <td><input type="checkbox" data-role="supplier" {checked[ROLE_SUPPLIER]}></td>
              <td><input type="checkbox" data-role="selector" {checked[ROLE_SELECTOR]}></td>
              <td><input type="checkbox" data-role="operator" {checked[ROLE_OPERATOR]}></td>
              <td><input type="checkbox" data-role="confirmer" {checked[ROLE_CONFIRMER]}></td>
              <td><input data-field="search_text" value="{_escape(contact.search_text)}"></td>
              <td><input data-field="main_categories" value="{_escape(','.join(contact.main_categories))}"></td>
              <td><input data-field="sample_address" value="{_escape(contact.sample_address)}"></td>
              <td><input data-field="notes" value="{_escape(contact.notes)}"></td>
            </tr>
            """
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>企业微信联系人角色配置</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif; margin: 24px; color: #1f2937; }}
    h1 {{ font-size: 22px; margin: 0 0 12px; }}
    .bar {{ display: flex; gap: 12px; align-items: center; margin-bottom: 16px; }}
    button {{ background: #2563eb; color: white; border: 0; border-radius: 6px; padding: 9px 14px; font-size: 14px; cursor: pointer; }}
    button.secondary {{ background: #4b5563; }}
    table {{ border-collapse: collapse; width: 100%; table-layout: fixed; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px; vertical-align: middle; }}
    th {{ background: #eff6ff; font-weight: 700; }}
    input[type="text"], input:not([type]) {{ width: 100%; box-sizing: border-box; border: 1px solid #cbd5e1; border-radius: 4px; padding: 6px; }}
    .muted {{ color: #6b7280; font-size: 12px; margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    #status {{ color: #166534; }}
  </style>
</head>
<body>
  <h1>企业微信联系人角色配置</h1>
  <div class="bar">
    <button onclick="save()">保存角色</button>
    <button class="secondary" onclick="location.reload()">重新加载</button>
    <span id="status"></span>
  </div>
  <table>
    <thead>
      <tr>
        <th style="width:52px;">启用</th>
        <th style="width:210px;">联系人</th>
        <th style="width:72px;">供应商</th>
        <th style="width:72px;">选款人</th>
        <th style="width:72px;">运营</th>
        <th style="width:90px;">人工确认</th>
        <th>搜索名</th>
        <th>主营类目</th>
        <th>寄样地址</th>
        <th>备注</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <script>
    async function save() {{
      const contacts = [...document.querySelectorAll('tr[data-contact-id]')].map(row => {{
        const roles = [...row.querySelectorAll('input[data-role]:checked')].map(input => input.dataset.role);
        const get = field => row.querySelector(`[data-field="${{field}}"]`);
        return {{
          contact_id: row.dataset.contactId,
          enabled: get('enabled').checked,
          roles,
          search_text: get('search_text').value.trim(),
          main_categories: get('main_categories').value.split(',').map(x => x.trim()).filter(Boolean),
          sample_address: get('sample_address').value.trim(),
          notes: get('notes').value.trim()
        }};
      }});
      const response = await fetch('/save', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{contacts}}) }});
      document.getElementById('status').textContent = await response.text();
    }}
  </script>
</body>
</html>"""


def serve_role_manager(path: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    contacts = load_contact_roles(path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if urlparse(self.path).path != "/":
                self.send_error(404)
                return
            html = render_role_manager_html(load_contact_roles(path)).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/save":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            updates = {item["contact_id"]: item for item in payload.get("contacts", [])}
            current = load_contact_roles(path)
            for contact in current:
                update = updates.get(contact.contact_id)
                if not update:
                    continue
                contact.enabled = bool(update.get("enabled"))
                contact.roles = [role for role in update.get("roles", []) if role in KNOWN_ROLES]
                contact.search_text = update.get("search_text", "")
                contact.main_categories = update.get("main_categories", [])
                contact.sample_address = update.get("sample_address", "")
                contact.notes = update.get("notes", "")
            write_contact_roles(path, current)
            body = "已保存".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            return

    if not contacts:
        write_contact_roles(path, [])
    print(f"联系人角色配置页：http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def _escape(value: object) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _load_unknown_archive_messages(data_dir: Path) -> List[dict]:
    unknown_dir = data_dir / "archive_unknown"
    if not unknown_dir.exists():
        return []
    messages = []
    for path in sorted(unknown_dir.glob("*/*.json")):
        try:
            messages.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return messages


def _contact_mentions(contact: ContactRole, text: str) -> bool:
    haystack = _normalize_match_text(text)
    for alias in _contact_aliases(contact):
        if _normalize_match_text(alias) in haystack:
            return True
    return False


def _contact_aliases(contact: ContactRole) -> List[str]:
    aliases = []
    for value in [contact.display_name, contact.search_text, contact.corp_name]:
        value = value.strip()
        if not value:
            continue
        aliases.append(value)
        aliases.extend(part for part in re.split(r"[-—–|｜/\\（）()，,；;\s]+", value) if _useful_alias(part))
        if "-" in value:
            aliases.append(value.split("-", 1)[0])
    unique = []
    seen = set()
    for alias in aliases:
        normalized = _normalize_match_text(alias)
        if normalized and normalized not in seen and _useful_alias(alias):
            unique.append(alias)
            seen.add(normalized)
    return unique


def _normalize_match_text(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


def _useful_alias(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    if re.fullmatch(r"\d+", stripped):
        return len(stripped) >= 7
    return len(stripped) >= 4


def _append_note(notes: str, note: str) -> str:
    return notes if note in notes else "; ".join(part for part in [notes, note] if part)
