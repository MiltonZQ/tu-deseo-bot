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


# Patrones que indican frustración, molestia del cliente o solicitud explícita de un asesor humano
FRUSTRATION_PATTERNS = [
    r"\b(asesor|humano|persona|agente)\b",
    r"\b(hablar|pasar|comunicar)\s+con\s+(un\s+)?(asesor|humano|persona|agente)\b",
    r"\b(me\s+estresa|estresad[oa]|frustrad[oa]|molest[oa]|fastidiad[oa])\b",
    r"\b(p[eé]simo|terrible|malo|horrible)\s+servicio\b",
    r"\b(no\s+entiendes|ya\s+te\s+dije|ya\s+te\s+indicad[oa]|siempre\s+falla)\b",
    r"\b(malparid[oa]|mierda|puta|est[uú]pid[oa]|inepto|no\s+sea\s+tan)\b",
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

    if _match(FRUSTRATION_PATTERNS, ut):
        return (
            "cliente_frustrado",
            "Cliente expresa molestia, insatisfacción o solicita un asesor humano.",
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


async def registrar(wa_id: str, reason: str, reason_detail: str,
                    issue_summary: str, history: list[dict], bot_reply: str,
                    media_type: str | None = None) -> int:
    """Crea o actualiza la escalation pendiente de un contacto.

    Sin pasar por `detect_reason`. Ese camino decide mirando si la respuesta
    del bot contiene frases como "especialista te responderá", y el handoff por
    falta de stock no contiene ninguna: pausaba el bot y no dejaba rastro en el
    panel, así que el cliente quedaba esperando a nadie. Cuando el sistema YA
    sabe que está escalando no tiene que averiguarlo leyéndose a sí mismo.
    """
    customer = _extract_customer_data(
        history + [{"role": "user", "content": issue_summary}])
    data = {
        "wa_id": wa_id,
        **customer,
        "issue_summary": (issue_summary[:300] + "...") if len(issue_summary) > 300
                         else issue_summary,
        "reason": reason,
        "reason_detail": reason_detail,
        "last_media_type": media_type,
        "conversation_excerpt": _build_excerpt(history, issue_summary, bot_reply),
    }
    existing = await db.find_pending_escalation(wa_id)
    if existing:
        await db.bump_escalation(
            existing["id"], {**data, "media_count_delta": 1 if media_type else 0})
        log.info("Escalation %d actualizada (wa_id=%s, reason=%s)",
                 existing["id"], wa_id, reason)
        return existing["id"]
    data["media_count"] = 1 if media_type else 0
    new_id = await db.create_escalation(data)
    log.info("Escalation %d CREADA (wa_id=%s, reason=%s)", new_id, wa_id, reason)
    return new_id


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
    return await registrar(
        wa_id=wa_id, reason=reason_code, reason_detail=reason_detail,
        issue_summary=user_text, history=history, bot_reply=bot_reply,
        media_type=media_type)
