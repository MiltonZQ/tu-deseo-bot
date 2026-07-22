"""Verificacion de firmas HMAC de proveedores de WhatsApp."""
import hmac
import hashlib
from app import config


def verify_meta(header_value: str | None, body: bytes) -> bool:
    """Devuelve True si la firma es válida o si META_APP_SECRET no está configurado."""
    if not config.META_APP_SECRET:
        return True  # opt-in: sin secret configurado, no validamos
    if not header_value or not header_value.startswith("sha256="):
        return False
    expected = hmac.new(
        config.META_APP_SECRET.encode("utf-8"),
        body,
        hashlib.sha256,
    ).hexdigest()
    received = header_value.removeprefix("sha256=")
    return hmac.compare_digest(expected, received)


def verify_ycloud(header_value: str | None, body: bytes) -> bool:
    """Verifica YCloud-Signature: t={timestamp},s={hex_hmac_sha256}."""
    if not config.YCLOUD_WEBHOOK_SECRET:
        return True
    if not header_value:
        return False
    parts = {}
    for item in header_value.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        parts[key.strip()] = value.strip()
    timestamp = parts.get("t")
    received = parts.get("s")
    if not timestamp or not received:
        return False
    signed_payload = timestamp.encode("utf-8") + b"." + body
    expected = hmac.new(
        config.YCLOUD_WEBHOOK_SECRET.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, received)


def verify(header_value: str | None, body: bytes) -> bool:
    if config.WHATSAPP_PROVIDER == "ycloud":
        return verify_ycloud(header_value, body)
    return verify_meta(header_value, body)
