"""Cliente de WhatsApp: Meta Cloud API o YCloud, solo texto en v1."""
import httpx
from app import config

GRAPH_VERSION = "v21.0"


async def send_text(to_wa_id: str, body: str) -> dict:
    if config.WHATSAPP_PROVIDER == "ycloud":
        return await _send_ycloud_text(to_wa_id, body)
    return await _send_meta_text(to_wa_id, body)


async def send_location(
    to_wa_id: str, latitude: float, longitude: float,
    name: str = "", address: str = "",
) -> dict:
    """Envía una ubicación (mensaje tipo location) por WhatsApp.

    Se usa para compartir la sede física de Tu Deseo cuando un cliente la pide
    o cuando el bot recomienda la sede más cercana a su barrio.
    """
    if config.WHATSAPP_PROVIDER == "ycloud":
        return await _send_ycloud_location(to_wa_id, latitude, longitude, name, address)
    return await _send_meta_location(to_wa_id, latitude, longitude, name, address)


async def _send_ycloud_location(
    to_wa_id: str, latitude: float, longitude: float,
    name: str, address: str,
) -> dict:
    url = f"{config.YCLOUD_API_BASE_URL.rstrip('/')}/v2/whatsapp/messages/sendDirectly"
    headers = {"X-API-Key": config.YCLOUD_API_KEY, "Content-Type": "application/json"}
    payload = {
        "from": config.YCLOUD_WHATSAPP_FROM,
        "to": _ensure_e164(to_wa_id),
        "type": "location",
        "location": {
            "latitude": latitude,
            "longitude": longitude,
            "name": name[:256] if name else None,
            "address": address[:256] if address else None,
        },
    }
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()


async def _send_meta_location(
    to_wa_id: str, latitude: float, longitude: float,
    name: str, address: str,
) -> dict:
    url = (
        f"https://graph.facebook.com/{GRAPH_VERSION}/"
        f"{config.WHATSAPP_PHONE_NUMBER_ID}/messages"
    )
    headers = {
        "Authorization": f"Bearer {config.WHATSAPP_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_wa_id,
        "type": "location",
        "location": {
            "latitude": latitude,
            "longitude": longitude,
            "name": name or None,
            "address": address or None,
        },
    }
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()



async def _send_meta_text(to_wa_id: str, body: str) -> dict:
    url = (
        f"https://graph.facebook.com/{GRAPH_VERSION}/"
        f"{config.WHATSAPP_PHONE_NUMBER_ID}/messages"
    )
    headers = {
        "Authorization": f"Bearer {config.WHATSAPP_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_wa_id,
        "type": "text",
        "text": {"body": body[:4096]},  # WhatsApp limita a 4096 chars
    }
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()


async def _send_ycloud_text(to_wa_id: str, body: str) -> dict:
    url = f"{config.YCLOUD_API_BASE_URL.rstrip('/')}/v2/whatsapp/messages/sendDirectly"
    headers = {
        "X-API-Key": config.YCLOUD_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "from": config.YCLOUD_WHATSAPP_FROM,
        "to": _ensure_e164(to_wa_id),
        "type": "text",
        "text": {"body": body[:4096]},
    }
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()


def _ensure_e164(value: str) -> str:
    clean = (value or "").strip()
    return clean if clean.startswith("+") else f"+{clean}"


def extract_message(payload: dict) -> dict | None:
    """
    Extrae info estructurada del payload de WhatsApp. Soporta todos los tipos
    (text, image, video, audio, document, sticker, etc.) devolviendo:

        {wa_id, message_id, type, text, media_id, media_mime, audio_url}

    - 'text' solo está lleno si type=='text'.
    - 'media_id', 'media_mime' y 'audio_url' solo están llenos si es media.
    - 'audio_url' disponible cuando el proveedor lo incluye en el payload.
    - Devuelve None para payloads de 'statuses' (delivered/read) o malformados.
    """
    if config.WHATSAPP_PROVIDER == "ycloud":
        return _extract_ycloud_message(payload)
    return _extract_meta_message(payload)


def _extract_meta_message(payload: dict) -> dict | None:
    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]
        value = change["value"]
        messages = value.get("messages")
        if not messages:
            return None
        msg = messages[0]
        mtype = msg.get("type", "unknown")
        # Ignorar reacciones: no son mensajes reales, son emojis sobre mensajes existentes
        if mtype == "reaction":
            return None
        out = {
            "wa_id": msg["from"],
            "message_id": msg["id"],
            "type": mtype,
            "text": "",
            "media_id": None,
            "media_mime": None,
            "audio_url": None,
        }
        if mtype == "text":
            out["text"] = msg.get("text", {}).get("body", "")
        elif mtype in ("image", "video", "audio", "document", "sticker", "voice"):
            media = msg.get(mtype, {}) or {}
            out["media_id"] = media.get("id")
            out["media_mime"] = media.get("mime_type")
            out["audio_url"] = media.get("url") or media.get("link")
            # El caption es texto opcional que viene junto con la imagen/video
            out["text"] = media.get("caption", "")
        elif mtype == "interactive":
            interactive = msg.get("interactive", {}) or {}
            reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
            out["text"] = reply.get("title", "")
        elif mtype == "location":
            loc = msg.get("location", {}) or {}
            out["text"] = f"[ubicación lat={loc.get('latitude')} lon={loc.get('longitude')}]"
        return out
    except (KeyError, IndexError, TypeError):
        return None


def _extract_ycloud_message(payload: dict) -> dict | None:
    try:
        if payload.get("type") not in (
            "whatsapp.inbound.message",
            "whatsapp.inbound_message.received",
        ):
            return None
        msg = payload.get("whatsappInboundMessage") or payload.get("whatsappMessage") or {}
        mtype = msg.get("type", "unknown")
        # Ignorar reacciones: no son mensajes reales, son emojis sobre mensajes existentes
        if mtype == "reaction":
            return None
        message_id = msg.get("id") or payload["id"]
        out = {
            "wa_id": (msg.get("from") or "").lstrip("+"),
            "message_id": message_id,
            "type": mtype,
            "text": "",
            "media_id": None,
            "media_mime": None,
            "audio_url": None,
        }
        if mtype == "text":
            out["text"] = msg.get("text", {}).get("body", "")
        elif mtype in ("image", "video", "audio", "document", "sticker", "voice"):
            media = msg.get(mtype, {}) or {}
            out["media_id"] = media.get("id") or media.get("mediaId")
            out["media_mime"] = media.get("mime_type") or media.get("mimeType")
            out["audio_url"] = (
                media.get("url") or media.get("link")
                or media.get("mediaUrl") or media.get("fileUrl")
            )
            out["text"] = media.get("caption", "")
        elif mtype == "interactive":
            interactive = msg.get("interactive", {}) or {}
            reply = interactive.get("button_reply") or interactive.get("buttonReply")
            reply = reply or interactive.get("list_reply") or interactive.get("listReply") or {}
            out["text"] = reply.get("title", "")
        elif mtype == "location":
            loc = msg.get("location", {}) or {}
            out["text"] = f"[ubicacion lat={loc.get('latitude')} lon={loc.get('longitude')}]"
        return out if out["wa_id"] else None
    except (KeyError, TypeError):
        return None


# Alias retrocompatible — solo devuelve mensajes de texto
def extract_text_message(payload: dict) -> dict | None:
    msg = extract_message(payload)
    if msg and msg["type"] == "text":
        return {"wa_id": msg["wa_id"], "message_id": msg["message_id"], "text": msg["text"]}
    return None


async def send_typing_indicator(message_id: str) -> None:
    """Envía typing indicator a YCloud para mostrar 'escribiendo...' al usuario."""
    if config.WHATSAPP_PROVIDER != "ycloud":
        return
    url = (
        f"{config.YCLOUD_API_BASE_URL.rstrip('/')}"
        f"/v2/whatsapp/inboundMessages/{message_id}/typingIndicator"
    )
    headers = {
        "X-API-Key": config.YCLOUD_API_KEY,
        "accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(url, headers=headers)
    except Exception:
        pass
