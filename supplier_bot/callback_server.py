from __future__ import annotations

import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET

from .config import Config
from .wecom_crypto import WeComCryptoError, decrypt_callback_xml, verify_and_decrypt_url


def serve_wecom_callback(config: Config, host: str = "127.0.0.1", port: int = 8787) -> None:
    server = make_wecom_callback_server(config, host=host, port=port)
    print(f"WeCom callback server: http://{host}:{port}/wecom/callback")
    server.serve_forever()


def make_wecom_callback_server(config: Config, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    if not config.wecom_callback_token or not config.wecom_callback_encoding_aes_key:
        raise RuntimeError("WECOM_CALLBACK_TOKEN and WECOM_CALLBACK_ENCODING_AES_KEY are required")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            query = parse_qs(urlparse(self.path).query)
            try:
                echo = verify_and_decrypt_url(
                    config.wecom_callback_token,
                    config.wecom_callback_encoding_aes_key,
                    _one(query, "msg_signature"),
                    _one(query, "timestamp"),
                    _one(query, "nonce"),
                    _one(query, "echostr"),
                )
                self._write_text(200, echo)
            except Exception as exc:
                self._write_text(403, str(exc))

        def do_POST(self) -> None:  # noqa: N802
            query = parse_qs(urlparse(self.path).query)
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            try:
                decrypted = decrypt_callback_xml(
                    config.wecom_callback_token,
                    config.wecom_callback_encoding_aes_key,
                    _one(query, "msg_signature"),
                    _one(query, "timestamp"),
                    _one(query, "nonce"),
                    body,
                )
                event = {
                    "received_at": datetime.now().isoformat(timespec="seconds"),
                    "path": self.path,
                    "query": {key: values[0] if values else "" for key, values in query.items()},
                    "message": _xml_to_dict(decrypted),
                }
                _write_callback_event(config.data_dir / "wecom_callback_events", body, decrypted, event)
                self._write_text(200, "success")
            except Exception as exc:
                self._write_text(403, str(exc))

        def log_message(self, format: str, *args) -> None:
            return

        def _write_text(self, status: int, text: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(text.encode("utf-8"))

    return ThreadingHTTPServer((host, port), Handler)


def write_callback_env_template(path: Path, token: str, encoding_aes_key: str) -> None:
    path.write_text(
        json.dumps(
            {
                "WECOM_CALLBACK_TOKEN": token,
                "WECOM_CALLBACK_ENCODING_AES_KEY": encoding_aes_key,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _one(query: dict, key: str) -> str:
    values = query.get(key)
    if not values:
        raise WeComCryptoError(f"missing query parameter: {key}")
    return values[0]


def _write_callback_event(event_dir: Path, raw_xml: str, decrypted_xml: str, event: dict) -> None:
    event_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    (event_dir / "latest.encrypted.xml").write_text(raw_xml, encoding="utf-8")
    (event_dir / "latest.xml").write_text(decrypted_xml, encoding="utf-8")
    (event_dir / "latest.json").write_text(json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8")
    (event_dir / f"{stamp}.encrypted.xml").write_text(raw_xml, encoding="utf-8")
    (event_dir / f"{stamp}.xml").write_text(decrypted_xml, encoding="utf-8")
    (event_dir / f"{stamp}.json").write_text(json.dumps(event, ensure_ascii=False, indent=2), encoding="utf-8")


def _xml_to_dict(raw_xml: str) -> dict:
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError:
        return {"raw_xml": raw_xml}
    return {child.tag: child.text or "" for child in root}
