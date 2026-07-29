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

from app import config, db, openai_client, whatsapp_client, signature, catalog
from app import escalations, admin, leads, follow_ups, sedes, pedidos

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("tu-deseo-bot")

_message_buffer: dict[str, list[str]] = {}
_message_buffer_lock = asyncio.Lock()
MESSAGE_GROUP_WAIT = 6.0  # segundos de espera para agrupar mensajes del mismo número

# Lock por usuario: serializa el procesamiento de un mismo wa_id para que sus
# mensajes se atiendan en secuencia (evita respuestas duplicadas y race conditions
# en el buffer y el historial). Patrón tomado del agente-reservas-lobby.
_user_locks: dict[str, asyncio.Lock] = {}


def _get_user_lock(wa_id: str) -> asyncio.Lock:
    """Devuelve (o crea) el lock de procesamiento para un usuario."""
    if wa_id not in _user_locks:
        _user_locks[wa_id] = asyncio.Lock()
    return _user_locks[wa_id]


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

    # Sincronizar catálogo e imágenes desde la web WooCommerce si está activado
    if config.WOOCOMMERCE_SYNC_ENABLED:
        try:
            from app import woocommerce
            log.info("WooCommerce activado: iniciando sincronización inicial de productos e imágenes...")
            asyncio.create_task(woocommerce.sync_catalog_from_woocommerce(full_replace=True))
        except Exception:
            log.exception("No se pudo iniciar sincronización de WooCommerce")

    deleted = await db.purge_old(config.HISTORY_TTL_DAYS)
    if deleted:
        log.info("Purgados %d mensajes viejos (>%dd)", deleted, config.HISTORY_TTL_DAYS)
    config.load_prompts()
    log.info("Prompts cargados: %d chars", len(config.SYSTEM_PROMPT))
    try:
        backfilled = await db.backfill_verified_abonos()
        if backfilled:
            log.info("Backfill retroactivo: %d pedidos pagados creados", backfilled)
    except Exception:
        log.exception("Error ejecutando backfill retroactivo de abonos")
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


# Marcador de foto: [FOTO:123] (ID numérico) o [FOTO:Nombre del Producto].
# El LLM lo emite cuando quiere que se envíe la foto de un producto.
_FOTO_MARKER_RE = re.compile(r"\[FOTO:\s*([^\]]+)\]", re.IGNORECASE)

# Marcador de categoría: [CATEGORIA:Punto G] → envía fotos de esa subcategoría.
_CATEGORIA_MARKER_RE = re.compile(r"\[CATEGORIA:\s*([^\]]+)\]", re.IGNORECASE)

# Petición explícita de fotos por parte del cliente (acotada para evitar falsos
# positivos como "sí", "claro", "ver": requiere verbo de envío o sustantivo de imagen).
_FOTO_REQUEST_RE = re.compile(
    r"\b(foto[s]?|imagen(es)?|fotografia[s]?|muestr(a|ame|amelo|amelas)|"
    r"mand(a|ame|ala|amelas)|envi(a|ame|ala|amelas)|ver la[s]? (foto|imagen)|"
    r"ver el producto|cada uno|todas las (foto|imagen))\b",
    re.IGNORECASE,
)


def _extraer_marcadores_foto(reply: str) -> tuple[list[str], str]:
    """Extrae marcadores [FOTO:...] de la respuesta y devuelve (ids/nombres, reply_limpio).

    El reply limpio (sin marcadores) es el que se envía al usuario y se persiste.
    """
    ids: list[str] = []
    for match in _FOTO_MARKER_RE.finditer(reply):
        ref = match.group(1).strip()
        if ref:
            ids.append(ref)
    clean = _FOTO_MARKER_RE.sub("", reply).strip()
    # Colapsar espacios múltiples dejados por el strip
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    return ids, clean


def _extraer_marcadores_categoria(reply: str) -> tuple[list[str], str]:
    """Extrae marcadores [CATEGORIA:...] y devuelve (categorias, reply_limpio)."""
    cats: list[str] = []
    for match in _CATEGORIA_MARKER_RE.finditer(reply):
        cat = match.group(1).strip()
        if cat:
            cats.append(cat)
    clean = _CATEGORIA_MARKER_RE.sub("", reply).strip()
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    return cats, clean


async def _enviar_fotos_productos(
    wa_id: str,
    foto_refs: list[str],
    reply: str,
    user_text: str,
    history: list[dict],
    categoria_refs: list[str] | None = None,
) -> None:
    """Envía fotos de productos por WhatsApp.

    Resolución por orden de fiabilidad:
      1. Marcadores [FOTO:ID] del LLM -> get_producto_by_id (fiable).
      2. Marcadores [FOTO:NOMBRE] -> get_productos_en_texto sobre el nombre.
      3. Marcadores [CATEGORIA:Punto G] -> get_productos_por_categoria_origen
         (envía las fotos de esa subcategoría de la web).
      4. Fallback: si el cliente pidió fotos explícitamente y el LLM no emitió
         marcador, se busca por nombre en el mensaje del usuario y la respuesta.
    Máximo 5 fotos por turno.
    """
    try:
        prods_to_send: list[dict] = []

        # 1+2. Resolver marcadores emitidos por el LLM (máximo 5)
        for ref in foto_refs[:5]:
            if ref.isdigit():
                p = await catalog.get_producto_by_id(int(ref))
                if p:
                    p["_explicit_id"] = True
                    prods_to_send.append(p)
                    continue
            # Referencia por nombre: buscar en el texto del propio ref
            found = await catalog.get_productos_en_texto(ref, limit=1)
            if found:
                prods_to_send.extend(found)

        # 3. Resolver marcadores de categoría [CATEGORIA:...] → fotos de esa subcategoría
        if categoria_refs and not prods_to_send:
            for cat in categoria_refs[:1]:
                prods_cat = await catalog.get_productos_por_categoria_origen(cat, limit=5)
                for pc in prods_cat:
                    pc["_por_categoria"] = True
                    prods_to_send.append(pc)
                if cat:
                    log.info("Fotos por categoría '%s': %d productos", cat, len(prods_cat))

        # 4. Fallback: cliente pidió fotos explícitamente y no hubo marcador
        if not prods_to_send and _FOTO_REQUEST_RE.search(user_text):
            is_multi = bool(re.search(
                r"\b(cada\s+uno|todas|todos|cada\s+una|los\s+\w+|las\s+\w+)\b",
                user_text.lower(),
            ))
            if is_multi and history:
                for prev_msg in reversed(history[-6:]):
                    found_list = await catalog.get_productos_en_texto(
                        prev_msg.get("content", ""), limit=5,
                    )
                    if found_list:
                        prods_to_send.extend(found_list)
                        break
            if not prods_to_send:
                p_user = await catalog.get_producto_con_imagen(user_text)
                if p_user:
                    prods_to_send.append(p_user)
            if not prods_to_send:
                found_reply = await catalog.get_productos_en_texto(reply, limit=4)
                prods_to_send.extend(found_reply)

        cat_cliente = catalog._categoria_normalizada(user_text) if user_text else ""

        # Enviar (máx 5, dedup por id, solo con imagen)
        seen_ids: set[int] = set()
        enviadas = 0
        sin_imagen = 0
        fuera_categoria = 0
        for p in prods_to_send:
            if enviadas >= 5:
                break
            pid = p["id"]
            if pid in seen_ids:
                continue
            # Omitir filtro de categoría si el producto se resolvió por ID explícito numérico o por categoría
            if cat_cliente and cat_cliente != "juegos-y-accesorios" and not p.get("_por_categoria") and not p.get("_explicit_id"):
                cat_prod = catalog._categoria_normalizada(
                    p.get("nombre", ""), p.get("descripcion", ""), p.get("categoria", ""),
                )
                if cat_prod != cat_cliente:
                    fuera_categoria += 1
                    log.warning(
                        "Foto omitida (otra categoría): '%s' es %s, cliente pidió %s",
                        p.get("nombre"), cat_prod, cat_cliente,
                    )
                    continue
            if not p.get("imagen_url"):
                sin_imagen += 1
                log.warning(
                    "Producto sin imagen_url, foto omitida: '%s' (id=%s)", p.get("nombre"), pid,
                )
                continue
            seen_ids.add(pid)
            caption = f"📸 *{p['nombre']}*\n💰 ${p['precio']:,}"
            await whatsapp_client.send_image(wa_id, p["imagen_url"], caption)
            log.info("Foto de producto '%s' enviada a %s", p["nombre"], wa_id)
            enviadas += 1
            if enviadas < 5 and prods_to_send:
                await asyncio.sleep(0.8)
        log.info(
            "Fotos a %s: refs=%d resueltos=%d enviadas=%d sin_imagen=%d fuera_categoria=%d",
            wa_id, len(foto_refs), len(prods_to_send), enviadas, sin_imagen, fuera_categoria,
        )
    except Exception:
        log.exception("Error enviando foto de producto a %s", wa_id)


_NOUN_KEYWORDS = [
    "suspensorio", "suspensor", "lenceria", "lencería", "body", "babydoll", "baby doll",
    "disfraz", "vibrador", "dildo", "succionador", "plug", "anal", "arnes", "arnés",
    "lubricante", "anillo", "funda", "masturbador", "bomba", "bondage"
]


async def _buscar_productos_para_contexto(
    user_text: str, history: list[dict] | None = None
) -> str | None:
    """Busca en la DB productos que coincidan con el mensaje del cliente y los
    devuelve formateados como bloque de contexto para el LLM (RAG ligero).

    Si el mensaje del cliente es corto o un atributo/color (ej: "negro", "rojo", "sencillo"),
    extrae el sustantivo principal del historial de la conversación (ej: "suspensorio")
    para buscar "suspensorio negro" en Postgres en lugar de "negro" a secas.
    """
    if not user_text or len(user_text.strip()) < 2:
        return None
    try:
        search_phrase = user_text.strip()

        # Si user_text no contiene un sustantivo explícito, buscar el sustantivo principal en el historial
        has_noun = any(w in search_phrase.lower() for w in _NOUN_KEYWORDS)
        if not has_noun and history:
            found_noun = None
            for h_msg in reversed(history[-6:]):
                content = h_msg.get("content", "").lower()
                for n_kw in _NOUN_KEYWORDS:
                    if n_kw in content:
                        found_noun = n_kw
                        break
                if found_noun:
                    break
            if found_noun:
                search_phrase = f"{found_noun} {search_phrase}"
                log.info("RAG: frase combinada con historial: %r", search_phrase)

        candidatos = set()
        # 1. Frase completa o combinada
        for p in await catalog.search_with_stock(search_phrase, limit=6):
            candidatos.add(p["id"])
            if len(candidatos) >= 6:
                break

        # 2. Si no hay suficientes, probar tokens de la frase original
        if len(candidatos) < 4:
            tokens = [t for t in re.findall(r"[a-záéíóúñ]{3,}", user_text.lower())
                      if t not in {"quiero", "necesito", "busco", "tienen", "hola", "buenas",
                                   "buenos", "gracias", "podrian", "podemos", "deseo",
                                   "gustaria", "para", "hombre", "mujer", "pareja"}]
            for tok in tokens[:3]:
                for p in await catalog.search_with_stock(tok, limit=3):
                    candidatos.add(p["id"])
                    if len(candidatos) >= 8:
                        break

        if not candidatos:
            return None

        productos = []
        for pid in list(candidatos)[:6]:
            p = await catalog.get_producto_by_id(pid)
            if p:
                productos.append(p)
        if not productos:
            return None

        lineas = ["## Productos disponibles que coinciden con la consulta del cliente"]
        lineas.append("(Ofrécelos usando [FOTO:ID] con el ID exacto; son productos reales con stock):")
        for p in productos:
            desc = (p.get("descripcion") or "")[:80]
            lineas.append(f"- **{p['nombre']}** — ${p['precio']:,} — {desc}  #{p['id']}")
        log.info("RAG: %d productos inyectados al contexto para consulta %r",
                 len(productos), search_phrase)
        return "\n".join(lineas)
    except Exception:
        log.exception("Error en búsqueda RAG para contexto")
        return None


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

    # Serializar el procesamiento por usuario: los mensajes de un mismo wa_id se
    # atienden en secuencia (nunca en paralelo). Evita respuestas duplicadas y
    # que el buffer de agrupación se des sincronice.
    async with _get_user_lock(wa_id):
        await _handle_message(msg, wa_id)


async def _handle_message(msg: dict, wa_id: str) -> None:
    """Procesa un mensaje ya desduplicado, bajo el lock del usuario."""
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

    # Cargar perfil del cliente y memoria consolidada para enriquecer el contexto.
    lead = await db.get_lead(wa_id)
    summary_row = await db.get_summary(wa_id)
    summary_text = summary_row["summary"] if summary_row else None

    # RAG ligero: buscar en la DB productos que coincidan con la consulta del cliente
    # ANTES de llamar al LLM, e inyectarlos como contexto. Así el bot encuentra
    # productos que no están en su catalogo.md (ej: suspensorios, productos nuevos).
    extra_context = await _buscar_productos_para_contexto(user_text, history=history)

    raw_reply = await openai_client.complete(
        user_text, history, lead=lead, summary=summary_text, extra_context=extra_context,
    )
    reply = await leads.process_reply(
        wa_id,
        raw_reply,
        history + [{"role": "user", "content": user_text}],
    )

    # Extraer marcadores de foto [FOTO:ID] y de categoría [CATEGORIA:...] y limpiarlos
    # del texto visible. Las fotos se envían tras el mensaje de texto.
    foto_ids, reply = _extraer_marcadores_foto(reply)
    categoria_refs, reply = _extraer_marcadores_categoria(reply)

    # Detectar cierre de venta [[PEDIDO:CERRADO]]: crea el pedido automáticamente
    # (con datos de envío del historial y total calculado del catálogo).
    reply, pedido_creado_id = await pedidos.maybe_create_pedido(
        wa_id, reply, history + [{"role": "user", "content": user_text}],
    )
    if pedido_creado_id:
        log.info("Pedido #%d creado automáticamente para %s", pedido_creado_id, wa_id)

    await db.save_message(wa_id, "user", user_text)
    await db.save_message(wa_id, "assistant", reply)
    await whatsapp_client.send_text(wa_id, reply)

    # Enviar fotos resueltas por marcador (fiable) o por fallback de nombre.
    await _enviar_fotos_productos(wa_id, foto_ids, reply, user_text, history, categoria_refs)

    # Memoria comprimida: si la conversación crece, consolidarla en un resumen
    # que se reinyecta en futuros turnos para no perder contexto de largo plazo.
    total_msgs = len(history) + 2  # +user +assistant recién guardados
    if total_msgs >= config.SUMMARY_THRESHOLD:
        try:
            refreshed = await db.get_history(wa_id, config.HISTORY_WINDOW)
            new_summary = await openai_client.summarize_conversation(refreshed, lead)
            await db.save_summary(wa_id, new_summary, total_msgs)
            log.info("Resumen consolidado actualizado para %s (%d msgs)", wa_id, total_msgs)
        except Exception:
            log.exception("Error generando resumen consolidado para %s", wa_id)

    # Si la conversación se refiere a UNA sede específica, enviar el pin de ubicación nativo de WhatsApp.
    sede_mencionada = sedes.detectar_sede_para_enviar(user_text, reply)
    if sede_mencionada:
        info = sedes.get_info(sede_mencionada)
        if info and "lat" in info and "lng" in info:
            try:
                await whatsapp_client.send_location(
                    to_wa_id=wa_id,
                    latitude=info["lat"],
                    longitude=info["lng"],
                    name=f"Tu Deseo — {sede_mencionada}",
                    address=info["dir"],
                )
                log.info("Pin de ubicación de sede '%s' enviado a %s", sede_mencionada, wa_id)
            except Exception:
                log.exception("Error enviando pin de ubicación de sede %s", sede_mencionada)

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
    # (lead ya cargado arriba para enriquecer el contexto).
    if not lead or lead.get("qualification_status") == "en_progreso":
        await follow_ups.schedule(wa_id)

    log.info("Respondido a %s (%d chars)", wa_id, len(reply))


@app.post("/webhooks/woocommerce")
async def woocommerce_webhook(
    request: Request,
    x_wc_webhook_signature: str = Header(default="", alias="X-WC-Webhook-Signature"),
    x_wc_webhook_topic: str = Header(default="", alias="X-WC-Webhook-Topic"),
):
    """Endpoint para recibir webhooks de WooCommerce en tiempo real (product.created, product.updated, product.deleted)."""
    body = await request.body()
    from app import woocommerce

    if not woocommerce.verify_signature(body, x_wc_webhook_signature):
        log.warning("Firma de webhook de WooCommerce inválida")
        raise HTTPException(status_code=401, detail="Firma de webhook inválida")

    try:
        payload = json.loads(body.decode("utf-8"))
        await woocommerce.process_webhook_payload(payload, x_wc_webhook_topic)
        return {"status": "ok"}
    except Exception as e:
        log.exception("Error procesando webhook de WooCommerce: %s", e)
        return {"status": "error", "detail": str(e)}
