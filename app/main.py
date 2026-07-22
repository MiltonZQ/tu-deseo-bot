"""FastAPI app: /webhook (GET handshake yCloud/Meta + POST mensajes), /health, /reload,
/maintenance/reset-contact y panel /admin.

Bot conversacional de Tu Deseo — Sex Shop & Bienestar Sexual.
Porteado de Demo chat Agentico SIN la lógica de Cal.com/agenda, con hooks para pagos.
"""
import asyncio
import logging
import re
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException, Header, Query
from fastapi.responses import PlainTextResponse
from starlette.middleware.sessions import SessionMiddleware

from app import config, db, openai_client, whatsapp_client, signature
from app import escalations, admin, leads, follow_ups, sedes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("tu-deseo-bot")

_message_buffer: dict[str, list[str]] = {}
_message_buffer_lock = asyncio.Lock()
MESSAGE_GROUP_WAIT = 4.0  # segundos de espera para agrupar mensajes del mismo número


@asynccontextmanager
async def lifespan(_app: FastAPI):
    missing = config.validate()
    if missing:
        log.warning("Config incompleta, faltan: %s", ", ".join(missing))
    await db.init_pool()
    await db.run_migrations()
    # Cargar catálogo de productos automáticamente si la tabla está vacía
    try:
        csv_path = config.PROMPTS_DIR / "knowledge" / "catalogo.csv"
        loaded = await db.seed_catalogo_if_empty(csv_path)
        if loaded:
            log.info("Catálogo cargado: %d productos", loaded)
    except Exception:
        log.exception("No se pudo cargar el catálogo (no bloquea el arranque)")
    deleted = await db.purge_old(config.HISTORY_TTL_DAYS)
    if deleted:
        log.info("Purgados %d mensajes viejos (>%dd)", deleted, config.HISTORY_TTL_DAYS)
    config.load_prompts()
    log.info("Prompts cargados: %d chars", len(config.SYSTEM_PROMPT))
    task = None
    if config.ENABLE_FOLLOW_UPS:
        task = asyncio.create_task(follow_ups.run_loop())
    else:
        cleared = await db.mark_all_follow_ups_sent()
        if cleared:
            log.info("Follow-ups pendientes desactivados al arrancar: %d", cleared)
    yield
    if task:
        task.cancel()
    await db.close_pool()


app = FastAPI(lifespan=lifespan, title="Tu Deseo — WhatsApp Bot")

# Sessions (para /admin). Si SESSION_SECRET está vacío, el panel no funcionará.
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET or "dev-only-do-not-use",
    same_site="lax",
    https_only=True,
    max_age=60 * 60 * 8,  # 8 horas
)

app.include_router(admin.router)


@app.get("/health")
def health():
    return {"status": "ok", "business": config.BUSINESS_NAME}


@app.get("/webhook")
def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """Handshake: yCloud no requiere challenge (responde ok); Meta sí verifica el token."""
    if config.WHATSAPP_PROVIDER == "ycloud":
        return {"status": "ok", "provider": "ycloud"}
    if hub_mode == "subscribe" and hub_verify_token == config.VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge or "")
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook")
async def receive_webhook(request: Request, bg: BackgroundTasks):
    body = await request.body()
    signature_header = (
        request.headers.get("YCloud-Signature")
        if config.WHATSAPP_PROVIDER == "ycloud"
        else request.headers.get("X-Hub-Signature-256")
    )
    if not signature.verify(signature_header, body):
        raise HTTPException(status_code=403, detail="Invalid signature")
    payload = await request.json()
    bg.add_task(_process_message_safe, payload)
    return {"status": "received"}


@app.post("/reload")
def reload_prompts(x_reload_token: str = Header(None)):
    if not config.RELOAD_TOKEN or x_reload_token != config.RELOAD_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    config.load_prompts()
    return {"reloaded": True, "system_prompt_chars": len(config.SYSTEM_PROMPT)}


@app.post("/webhook/bold")
async def bold_webhook(request: Request, bg: BackgroundTasks):
    """Webhook de confirmación de pago Bold (Semana 2)."""
    payload = await request.json()
    from app import bold
    bg.add_task(bold.process_webhook, payload)
    return {"status": "received"}


@app.post("/maintenance/reset-contact")
async def reset_contact_memory(
    wa_id: str,
    x_reload_token: str = Header(None),
):
    if not config.RELOAD_TOKEN or x_reload_token != config.RELOAD_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    deleted = await db.clear_contact_data([wa_id])
    return {"cleared_wa_ids": [wa_id], "deleted": deleted}


async def _process_message_safe(payload: dict) -> None:
    try:
        await _process_message(payload)
    except Exception:
        log.exception("Error procesando mensaje")


# wabaId bloqueado: la automatización ignora esta línea (anti-spam multi-línea).
# Todo lo demás pasa y el bot responde normalmente.
BLOCKED_WABA_IDS: set[str] = {"24683975404567089"}

# Mensaje de marca para media genérica (no comprobante)
MEDIA_REPLY = (
    "¡Hola! 👋 Para asesorarte mejor, cuéntame qué producto buscas o en qué te puedo ayudar. "
    "Si quieres ver nuestro catálogo, dime qué categoría te interesa."
)

HANDOFF_MSG = (
    "Claro, con gusto te paso con un asesor de Tu Deseo. "
    "En breve alguien del equipo te atenderá. Si quieres agregar algo más, puedes escribirlo aquí."
)

_HUMAN_REQUEST_RE = re.compile(
    r"\b(asesor|asesora|agente|humano|persona\s+real|hablar\s+con\s+alguien|"
    r"hablar\s+con\s+una\s+persona|quiero\s+un\s+humano|no\s+quiero\s+el\s+bot|"
    r"comunicarme\s+con|representante|vendedor|vendedora)\b",
    re.IGNORECASE,
)

_BOT_REFUSAL_RE = re.compile(
    r"\b(no\s+puedo\s+ayudarte\s+con\s+eso|fuera\s+de\s+mi\s+[aá]rea|"
    r"no\s+tengo\s+esa\s+informaci[oó]n|no\s+es\s+algo\s+que\s+pueda|"
    r"mi\s+funci[oó]n\s+es|solo\s+puedo\s+ayudarte\s+con|"
    r"ese\s+tema\s+no\s+lo\s+manejo|no\s+cuento\s+con\s+esa\s+info)\b",
    re.IGNORECASE,
)

_BOT_SAYS_HANDOFF_RE = re.compile(
    r"\b(te\s+paso\s+con|paso\s+(a\s+)?un\s+asesor|"
    r"alguien\s+del\s+equipo\s+se\s+comunicar[aá]|"
    r"un\s+asesor\s+te\s+(contactar[aá]|responder[aá]|va\s+a)|"
    r"pasarte\s+con\s+(alguien|un|una)|"
    r"nuestro\s+equipo\s+se\s+pondr[aá]\s+en\s+contacto|"
    r"te\s+contactar[aá]\s+un\s+asesor)\b",
    re.IGNORECASE,
)


def _count_bot_refusals(history: list[dict]) -> int:
    """Cuenta cuántas veces el bot ya rechazó una solicitud fuera de scope en el historial reciente."""
    count = 0
    for msg in history[-10:]:
        if msg.get("role") == "assistant" and _BOT_REFUSAL_RE.search(msg.get("content", "")):
            count += 1
    return count


async def _maybe_handle_payment_image(wa_id: str, msg: dict, history: list[dict]) -> bool:
    """Intenta procesar una imagen como comprobante de pago.

    Devuelve True si la imagen fue tratada como comprobante (haya sido válida o no),
    False si no hay contexto de pago pendiente o el módulo no está disponible.
    """
    if msg["type"] != "image" or not msg.get("audio_url"):
        # audio_url lleva la URL del archivo multimedia (imagen/audio) en yCloud/Meta
        pass
    try:
        from app import payments
    except ImportError:
        return False
    if not config.payment_accounts_configured():
        return False
    return await payments.handle_inbound_image(
        wa_id=wa_id,
        image_url=msg.get("audio_url"),
        caption=msg.get("text"),
        message_id=msg["message_id"],
        history=history,
    )


async def _process_message(payload: dict) -> None:
    # Bloquear wabaIds no autorizados (yCloud)
    if config.WHATSAPP_PROVIDER == "ycloud":
        inner = payload.get("whatsappInboundMessage") or payload.get("whatsappMessage") or {}
        waba_id = payload.get("wabaId") or inner.get("wabaId") or ""
        if waba_id in BLOCKED_WABA_IDS:
            log.info("Webhook ignorado: wabaId=%s bloqueado", waba_id)
            return

    msg = whatsapp_client.extract_message(payload)
    if msg is None:
        log.info("Webhook ignorado: type=%s keys=%s", payload.get("type"), sorted(payload.keys()))
        return

    wa_id = msg["wa_id"]
    if await db.was_processed(msg["message_id"]):
        log.info("Mensaje duplicado, ignorado: %s", msg["message_id"])
        return
    await db.mark_processed(msg["message_id"])

    # Si el bot está pausado para este contacto, ignorar silenciosamente
    if await db.is_bot_paused(wa_id):
        log.info("Bot pausado para %s — mensaje ignorado", wa_id)
        return

    # Typing indicator — mostrar "escribiendo..." inmediatamente
    await whatsapp_client.send_typing_indicator(msg["message_id"])

    mtype = msg["type"]
    user_text = (msg.get("text") or "")[: config.MAX_USER_MESSAGE_CHARS]
    media_type = mtype if mtype != "text" else None

    history = await db.get_history(wa_id, config.HISTORY_WINDOW)

    # ── Caso A: imagen → ¿es comprobante de pago? ──
    if mtype == "image":
        handled_as_payment = await _maybe_handle_payment_image(wa_id, msg, history)
        if handled_as_payment:
            return
        # Si no era comprobante, cae al reply genérico de media abajo.
        reply = MEDIA_REPLY
        saved_user_msg = user_text or "[envió una imagen]"
        await db.save_message(wa_id, "user", saved_user_msg)
        await db.save_message(wa_id, "assistant", reply)
        await whatsapp_client.send_text(wa_id, reply)
        await escalations.record_if_escalated(
            wa_id=wa_id, user_text=saved_user_msg, bot_reply=reply,
            message_type=mtype, media_type=media_type, history=history,
        )
        await follow_ups.schedule(wa_id)
        log.info("Imagen (no comprobante) recibida de %s", wa_id)
        return

    # ── Caso B: audio/voz → transcripción con Whisper ──
    if mtype in ("audio", "voice"):
        audio_url = msg.get("audio_url")
        if audio_url:
            transcribed = await openai_client.transcribe_audio(
                audio_url, msg.get("media_mime")
            )
            if transcribed:
                log.info("Audio transcrito para %s: %r", wa_id, transcribed[:80])
                user_text = transcribed
                media_type = None
            else:
                log.info("Transcripción fallida para %s — respuesta fija", wa_id)
        if media_type:
            reply = MEDIA_REPLY
            saved_user_msg = "[envió un audio de voz]"
            await db.save_message(wa_id, "user", saved_user_msg)
            await db.save_message(wa_id, "assistant", reply)
            await whatsapp_client.send_text(wa_id, reply)
            await follow_ups.schedule(wa_id)
            log.info("Audio sin URL para %s — respuesta fija enviada", wa_id)
            return

    # ── Caso C: otra media (video, documento, sticker) → reply fijo ──
    elif media_type:
        reply = MEDIA_REPLY
        saved_user_msg = user_text or f"[envió un archivo de tipo {media_type}]"
        await db.save_message(wa_id, "user", saved_user_msg)
        await db.save_message(wa_id, "assistant", reply)
        await whatsapp_client.send_text(wa_id, reply)
        await escalations.record_if_escalated(
            wa_id=wa_id, user_text=saved_user_msg, bot_reply=reply,
            message_type=mtype, media_type=media_type, history=history,
        )
        await follow_ups.schedule(wa_id)
        log.info("Media recibida de %s (%s)", wa_id, media_type)
        return

    # ── Caso D: texto (o audio transcrito) → flujo OpenAI ──
    # Agrupación de mensajes rápidos del mismo número en 4s
    if user_text.strip():
        async with _message_buffer_lock:
            if wa_id not in _message_buffer:
                _message_buffer[wa_id] = []
            _message_buffer[wa_id].append(user_text.strip())
            is_first = len(_message_buffer[wa_id]) == 1

        if is_first:
            await asyncio.sleep(MESSAGE_GROUP_WAIT)
            async with _message_buffer_lock:
                mensajes = _message_buffer.pop(wa_id, [])
            if mensajes:
                user_text = " ".join(mensajes)
                log.info("Mensajes agrupados para %s (%d): %r", wa_id, len(mensajes), user_text[:80])
        else:
            await follow_ups.cancel(wa_id)
            log.info("Mensaje agregado al buffer de %s, esperando procesamiento principal", wa_id)
            return

    await follow_ups.cancel(wa_id)

    if not user_text.strip():
        return

    # Detectar solicitud explícita de agente humano
    if _HUMAN_REQUEST_RE.search(user_text):
        await db.set_bot_paused(wa_id, True)
        await db.save_message(wa_id, "user", user_text)
        await db.save_message(wa_id, "assistant", HANDOFF_MSG)
        await whatsapp_client.send_text(wa_id, HANDOFF_MSG)
        await escalations.record_if_escalated(
            wa_id=wa_id, user_text=user_text, bot_reply=HANDOFF_MSG,
            message_type="text", media_type=None, history=history,
        )
        log.info("Handoff solicitado por %s — bot pausado", wa_id)
        return

    raw_reply = await openai_client.complete(user_text, history)
    reply = await leads.process_reply(
        wa_id,
        raw_reply,
        history + [{"role": "user", "content": user_text}],
    )

    await db.save_message(wa_id, "user", user_text)
    await db.save_message(wa_id, "assistant", reply)
    await whatsapp_client.send_text(wa_id, reply)

    # Si la respuesta del bot menciona una sede, enviar el link de Google Maps (ubicación exacta).
    sede_mencionada = sedes.detectar_sede(reply) or sedes.detectar_sede(user_text)
    if sede_mencionada:
        info = sedes.get_info(sede_mencionada)
        if info:
            try:
                ubicacion_msg = (
                    f"📍 *Tu Deseo — {sede_mencionada}*\n"
                    f"📪 {info['dir']}\n"
                    f"👉 {info['link']}"
                )
                await whatsapp_client.send_text(wa_id, ubicacion_msg)
                log.info("Link de sede '%s' enviado a %s", sede_mencionada, wa_id)
            except Exception:
                log.exception("Error enviando link de sede %s", sede_mencionada)

    await escalations.record_if_escalated(
        wa_id=wa_id, user_text=user_text, bot_reply=reply,
        message_type="text", media_type=None, history=history,
    )

    # Si el bot dijo que pasa a un asesor, pausar automáticamente
    if _BOT_SAYS_HANDOFF_RE.search(reply):
        await db.set_bot_paused(wa_id, True)
        log.info("Bot pausado: reply contiene handoff para %s", wa_id)
        return

    # Insistencia: si el bot ya rechazó ≥3 veces, pausar y escalar
    updated_history = history + [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": reply},
    ]
    if _count_bot_refusals(updated_history) >= 3:
        await db.set_bot_paused(wa_id, True)
        insistence_msg = (
            "Entiendo que tienes dudas sobre esto. "
            "Voy a pasarte con alguien del equipo que te puede ayudar mejor."
        )
        await db.save_message(wa_id, "assistant", insistence_msg)
        await whatsapp_client.send_text(wa_id, insistence_msg)
        log.info("Insistencia detectada para %s — bot pausado", wa_id)
        return

    # Programar follow-up solo si la conversación aún está en progreso
    lead = await db.get_lead(wa_id)
    if not lead or lead.get("qualification_status") == "en_progreso":
        await follow_ups.schedule(wa_id)

    log.info("Respondido a %s (%d chars)", wa_id, len(reply))
