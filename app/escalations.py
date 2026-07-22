"""Detección y registro de casos que requieren atención humana."""
import re
import logging
from app import db

log = logging.getLogger("escalations")

# Palabras/frases del cliente que disparan escalamiento URGENTE (seguridad)
URGENT_PATTERNS = [
    r"\bhumo\b",
    r"\bchispa\w*\b",
    r"quemad\w+",
    r"olor a quemado",
    r"se incendi\w+",
    r"explot\w+",
    r"bater[ií]a\s*(inflad|abombad|abultad)",
    r"carcas\w+\s*(inflad|deformad|abultad)",
]

# Patrones que indican daño físico claro
HARDWARE_PATTERNS = [
    r"hdmi\s*(rot|suelt|daniad|dañad|chueco|doblad|hundid)",
    r"pines?\s*doblad",
    r"no\s+reconoce\s+ning\w+\s*(sd|memoria|tarjeta)",
    r"bot[oó]n\s*(rot|trabad|pegad)",
    r"palanca\s*(rot|trabad|pegad)",
    r"acrílic\w+\s*(rot|estrellad|quebrad|grietad|trizad)",
    r"\bchasis\s*(doblad|descuadrad|chuec)",
    r"lleg[oó]?\s+(rot|daniad|dañad|golpe)",
    r"\b(rayaduras?|rayones?)\s+en\s+la\s+pantalla\b",
]

# Frases que el bot usa cuando está escalando
BOT_ESCALATION_PATTERNS = [
    r"especialista\s+te\s+responder[aá]",
    r"especialista\s+te\s+contactar[aá]",
    r"centro\s+de\s+servicio",
    r"reparaci[oó]n\s+t[eé]cnica",
    r"env[ií]o\s+a\s+taller",
    r"bajo\s+garant[ií]a",
    r"equipo\s+t[eé]cnico\s+lo\s+revisar[aá]",
    r"horario\s+h[aá]bil",
]


def _match(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def detect_reason(
    user_text: str,
    bot_reply: str,
    message_type: str,
) -> tuple[str, str] | None:
    """
    Devuelve (reason_code, reason_detail) o None si no hay que escalar.

    reason_code: valor guardado en DB para filtrado
    reason_detail: texto humano-legible para el panel
    """
    ut = user_text or ""
    bt = bot_reply or ""

    # Media entrante tiene prioridad (el bot no la procesa)
    if message_type and message_type != "text":
        return (
            "media_recibida",
            f"El cliente envió un mensaje de tipo '{message_type}' que el bot no puede procesar.",
        )

    if _match(URGENT_PATTERNS, ut):
        return (
            "urgente_seguridad",
            "Cliente reporta síntomas de riesgo (humo, chispa, batería inflada, etc.)",
        )

    if _match(HARDWARE_PATTERNS, ut):
        return (
            "hardware_daniado",
            "Cliente describe daño físico de hardware que requiere garantía/taller.",
        )

    if _match(BOT_ESCALATION_PATTERNS, bt):
        return (
            "bot_escalo",
            "El bot determinó que el caso debe pasar a humano.",
        )

    return None


def _extract_customer_data(history: list[dict]) -> dict:
    """
    Intenta extraer del historial los datos que el cliente dio en el filtro:
    nombre, ciudad, producto, fecha de compra.
    Heurística simple por regex sobre los mensajes 'user'.
    """
    out: dict[str, str | None] = {
        "customer_name": None,
        "city": None,
        "product": None,
        "purchase_date": None,
    }
    joined = "\n".join(m["content"] for m in history if m.get("role") == "user")

    patterns = {
        "customer_name": r"(?:nombre\s*(?:completo)?\s*[:\-]\s*)([^\n]+)",
        "city": r"(?:ciudad\s*[:\-]\s*)([^\n]+)",
        "product": r"(?:producto(?:\s*adquirido)?\s*[:\-]\s*)([^\n]+)",
        "purchase_date": r"(?:fecha\s*(?:de\s*compra)?(?:\s*aproximada)?\s*[:\-]\s*)([^\n]+)",
    }
    for key, pat in patterns.items():
        m = re.search(pat, joined, re.IGNORECASE)
        if m:
            val = m.group(1).strip().strip("*_")[:120]
            out[key] = val or None
    return out


def _build_excerpt(history: list[dict], user_text: str, bot_reply: str) -> str:
    """Últimos 4 mensajes del historial + el turno actual, formateado."""
    last = history[-4:] if len(history) > 4 else history[:]
    lines = []
    for m in last:
        role = "Cliente" if m["role"] == "user" else "Bot"
        lines.append(f"{role}: {m['content'][:400]}")
    lines.append(f"Cliente: {user_text[:400]}")
    lines.append(f"Bot: {bot_reply[:400]}")
    return "\n".join(lines)


async def record_if_escalated(
    wa_id: str,
    user_text: str,
    bot_reply: str,
    message_type: str,
    media_type: str | None,
    history: list[dict],
) -> int | None:
    """
    Si el turno actual requiere escalamiento, crea o actualiza la escalation
    pendiente para ese wa_id. Devuelve el id o None.
    """
    detected = detect_reason(user_text, bot_reply, message_type)
    if not detected:
        return None

    reason_code, reason_detail = detected
    customer = _extract_customer_data(history + [{"role": "user", "content": user_text}])
    excerpt = _build_excerpt(history, user_text, bot_reply)
    summary = (user_text[:300] + "...") if len(user_text) > 300 else user_text

    data = {
        "wa_id": wa_id,
        **customer,
        "issue_summary": summary,
        "reason": reason_code,
        "reason_detail": reason_detail,
        "last_media_type": media_type,
        "conversation_excerpt": excerpt,
    }

    existing = await db.find_pending_escalation(wa_id)
    if existing:
        # Actualiza la escalation pendiente (no creamos duplicados)
        bump = {
            **data,
            "media_count_delta": 1 if media_type else 0,
        }
        # Preservar customer_name/city/etc si la pendiente ya los tenía
        for k in ("customer_name", "city", "product", "purchase_date"):
            if not data.get(k) and existing.get(k):
                bump[k] = None  # deja el existente
            elif data.get(k):
                bump[k] = data[k]
        await db.bump_escalation(existing["id"], bump)
        log.info("Escalation %d actualizada (wa_id=%s, reason=%s)",
                 existing["id"], wa_id, reason_code)
        return existing["id"]

    data["media_count"] = 1 if media_type else 0
    new_id = await db.create_escalation(data)
    log.info("Escalation %d CREADA (wa_id=%s, reason=%s)", new_id, wa_id, reason_code)
    return new_id
