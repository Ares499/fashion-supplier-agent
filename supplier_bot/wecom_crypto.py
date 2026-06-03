from __future__ import annotations

import base64
import hashlib
import struct
import xml.etree.ElementTree as ET

from Crypto.Cipher import AES


class WeComCryptoError(ValueError):
    pass


def verify_signature(token: str, timestamp: str, nonce: str, encrypted: str, msg_signature: str) -> bool:
    raw = "".join(sorted([token, timestamp, nonce, encrypted]))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest() == msg_signature


def decrypt_callback_payload(encoding_aes_key: str, encrypted: str) -> str:
    key = _decode_aes_key(encoding_aes_key)
    cipher = AES.new(key, AES.MODE_CBC, key[:16])
    try:
        decrypted = cipher.decrypt(base64.b64decode(encrypted))
    except Exception as exc:
        raise WeComCryptoError(f"decrypt failed: {exc}") from exc

    plain = _strip_pkcs7_padding(decrypted)
    if len(plain) < 20:
        raise WeComCryptoError("decrypted payload too short")
    msg_len = struct.unpack("!I", plain[16:20])[0]
    msg = plain[20 : 20 + msg_len]
    try:
        return msg.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WeComCryptoError("decrypted payload is not utf-8") from exc


def decrypt_callback_xml(
    token: str,
    encoding_aes_key: str,
    msg_signature: str,
    timestamp: str,
    nonce: str,
    raw_xml: str,
) -> str:
    encrypted = extract_encrypt_from_xml(raw_xml)
    if not verify_signature(token, timestamp, nonce, encrypted, msg_signature):
        raise WeComCryptoError("invalid msg_signature")
    return decrypt_callback_payload(encoding_aes_key, encrypted)


def verify_and_decrypt_url(
    token: str,
    encoding_aes_key: str,
    msg_signature: str,
    timestamp: str,
    nonce: str,
    echostr: str,
) -> str:
    if not verify_signature(token, timestamp, nonce, echostr, msg_signature):
        raise WeComCryptoError("invalid msg_signature")
    return decrypt_callback_payload(encoding_aes_key, echostr)


def extract_encrypt_from_xml(raw_xml: str) -> str:
    try:
        root = ET.fromstring(raw_xml)
    except ET.ParseError as exc:
        raise WeComCryptoError(f"invalid callback xml: {exc}") from exc
    node = root.find("Encrypt")
    if node is None or not (node.text or "").strip():
        raise WeComCryptoError("missing Encrypt in callback xml")
    return (node.text or "").strip()


def encrypt_callback_payload(encoding_aes_key: str, plaintext: str, receive_id: str = "") -> str:
    key = _decode_aes_key(encoding_aes_key)
    body = plaintext.encode("utf-8")
    packed = b"0" * 16 + struct.pack("!I", len(body)) + body + receive_id.encode("utf-8")
    padded = _add_pkcs7_padding(packed)
    cipher = AES.new(key, AES.MODE_CBC, key[:16])
    return base64.b64encode(cipher.encrypt(padded)).decode("utf-8")


def _decode_aes_key(encoding_aes_key: str) -> bytes:
    if len(encoding_aes_key) != 43:
        raise WeComCryptoError("EncodingAESKey must be 43 characters")
    try:
        return base64.b64decode(encoding_aes_key + "=")
    except Exception as exc:
        raise WeComCryptoError(f"invalid EncodingAESKey: {exc}") from exc


def _strip_pkcs7_padding(data: bytes) -> bytes:
    if not data:
        raise WeComCryptoError("empty decrypted payload")
    pad = data[-1]
    if pad < 1 or pad > 32:
        raise WeComCryptoError("invalid padding")
    if data[-pad:] != bytes([pad]) * pad:
        raise WeComCryptoError("bad padding bytes")
    return data[:-pad]


def _add_pkcs7_padding(data: bytes) -> bytes:
    amount_to_pad = 32 - (len(data) % 32)
    if amount_to_pad == 0:
        amount_to_pad = 32
    return data + bytes([amount_to_pad]) * amount_to_pad
