"""Cliente de WhatsApp: Meta Cloud API o YCloud, solo texto en v1."""
import logging

import httpx
from app import config

log = logging.getLogger("whatsapp_client")

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


async def send_image(to_wa_id: str, image_url: str, caption: str = "") -> dict:
    """Envía un mensaje con imagen (foto de producto) por WhatsApp."""
    if config.WHATSAPP_PROVIDER == "ycloud":
        return await _send_ycloud_image(to_wa_id, image_url, caption)
    return await _send_meta_image(to_wa_id, image_url, caption)


def _es_webp(image_url: str) -> bool:
    """True si la URL apunta a una imagen webp (no soportada por WhatsApp Cloud API)."""
    return image_url.lower().split("?")[0].endswith(".webp")


async def _convert_webp_to_jpg_bytes(image_url: str) -> bytes | None:
    """Descarga una imagen webp y la convierte a JPG en memoria.

    Devuelve los bytes del JPG, o None si la descarga/conversión falla.
    WhatsApp Cloud API NO soporta webp (errorCode 131053), por eso se convierte.
    """
    from io import BytesIO

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(image_url)
        if resp.status_code != 200:
            log.warning("Descarga webp fallida: HTTP %s para %s", resp.status_code, image_url)
            return None
        from PIL import Image

        img = Image.open(BytesIO(resp.content))
        # Asegurar modo RGB (JPG no soporta canal alfa/transparencia)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")
        out = BytesIO()
        img.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()
    except Exception as exc:
        log.warning("Conversión webp→jpg fallida para %s: %s", image_url, exc)
        return None


async def _send_ycloud_image(to_wa_id: str, image_url: str, caption: str) -> dict:
    """Envía imagen por yCloud. Si es webp, la convierte a JPG y la sube como media."""
    image_payload: dict
    if _es_webp(image_url):
        # WhatsApp no soporta webp: convertir a JPG y subir como binario
        jpg_bytes = await _convert_webp_to_jpg_bytes(image_url)
        if jpg_bytes:
            media_id = await _upload_ycloud_media(jpg_bytes)
            if media_id:
                image_payload = {"id": media_id}
            else:
                # Subida falló: intentar URL original como último recurso
                image_payload = {"link": image_url}
        else:
            image_payload = {"link": image_url}
    else:
        image_payload = {"link": image_url}

    if caption:
        image_payload["caption"] = caption[:1024]

    url = f"{config.YCLOUD_API_BASE_URL.rstrip('/')}/v2/whatsapp/messages/sendDirectly"
    headers = {"X-API-Key": config.YCLOUD_API_KEY, "Content-Type": "application/json"}
    payload = {
        "from": config.YCLOUD_WHATSAPP_FROM,
        "to": _ensure_e164(to_wa_id),
        "type": "image",
        "image": image_payload,
    }
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()


async def _upload_ycloud_media(file_bytes: bytes, mime_type: str = "image/jpeg") -> str | None:
    """Sube un binario de imagen a yCloud y devuelve el mediaId, o None si falla.

    Endpoint: POST /v2/whatsapp/media/{phoneNumber}/upload (multipart/form-data).
    El número (from) va en la ruta en formato E.164.
    """
    phone = config.YCLOUD_WHATSAPP_FROM.lstrip("+")
    url = (
        f"{config.YCLOUD_API_BASE_URL.rstrip('/')}"
        f"/v2/whatsapp/media/{phone}/upload"
    )
    headers = {"X-API-Key": config.YCLOUD_API_KEY}
    files = {"file": ("image.jpg", file_bytes, mime_type)}
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(url, headers=headers, files=files)
        if r.status_code not in (200, 201):
            log.warning("Upload media yCloud fallido: HTTP %s - %s", r.status_code, r.text[:200])
            return None
        resp = r.json()
        return resp.get("id") or resp.get("mediaId")
    except Exception as exc:
        log.warning("Excepción subiendo media a yCloud: %s", exc)
        return None


async def _send_meta_image(to_wa_id: str, image_url: str, caption: str) -> dict:
    """Envía imagen por Meta Cloud API. Si es webp, la convierte a JPG y la sube."""
    image_payload: dict
    if _es_webp(image_url):
        jpg_bytes = await _convert_webp_to_jpg_bytes(image_url)
        if jpg_bytes:
            media_id = await _upload_meta_media(jpg_bytes)
            if media_id:
                image_payload = {"id": media_id}
            else:
                image_payload = {"link": image_url}
        else:
            image_payload = {"link": image_url}
    else:
        image_payload = {"link": image_url}

    if caption:
        image_payload["caption"] = caption[:1024]

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
        "type": "image",
        "image": image_payload,
    }
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()


async def _upload_meta_media(file_bytes: bytes, mime_type: str = "image/jpeg") -> str | None:
    """Sube un binario de imagen a Meta Cloud API y devuelve el mediaId."""
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{config.WHATSAPP_PHONE_NUMBER_ID}/media"
    headers = {"Authorization": f"Bearer {config.WHATSAPP_API_TOKEN}"}
    files = {"file": ("image.jpg", file_bytes, mime_type)}
    data = {"messaging_product": "whatsapp"}
    try:
        async with httpx.AsyncClient(timeout=25) as c:
            r = await c.post(url, headers=headers, files=files, data=data)
        if r.status_code not in (200, 201):
            log.warning("Upload media Meta fallido: HTTP %s - %s", r.status_code, r.text[:200])
            return None
        return r.json().get("id")
    except Exception as exc:
        log.warning("Excepción subiendo media a Meta: %s", exc)
        return None


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
            "image_url": None,
            "audio_url": None,
        }
        if mtype == "text":
            out["text"] = msg.get("text", {}).get("body", "")
        elif mtype in ("image", "video", "audio", "document", "sticker", "voice"):
            media = msg.get(mtype, {}) or {}
            out["media_id"] = media.get("id")
            out["media_mime"] = media.get("mime_type")
            m_url = media.get("url") or media.get("link")
            out["image_url"] = m_url if mtype == "image" else None
            out["audio_url"] = m_url if mtype in ("audio", "voice") else None
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
            "image_url": None,
            "audio_url": None,
        }
        if mtype == "text":
            out["text"] = msg.get("text", {}).get("body", "")
        elif mtype in ("image", "video", "audio", "document", "sticker", "voice"):
            media = msg.get(mtype, {}) or {}
            out["media_id"] = media.get("id") or media.get("mediaId")
            out["media_mime"] = media.get("mime_type") or media.get("mimeType")
            m_url = (
                media.get("url") or media.get("link")
                or media.get("mediaUrl") or media.get("fileUrl")
            )
            out["image_url"] = m_url if mtype == "image" else None
            out["audio_url"] = m_url if mtype in ("audio", "voice") else None
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
