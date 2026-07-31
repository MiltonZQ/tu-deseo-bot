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
from app import escalations, admin, leads, follow_ups, sedes, pedidos, redis_client, vector_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("tu-deseo-bot")

_message_buffer: dict[str, list[str]] = {}
_message_buffer_lock = asyncio.Lock()
MESSAGE_GROUP_WAIT = 0.5  # agrupar mensajes ráfaga en 0.5s sin bloquear el lock del usuario

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
    await redis_client.init_redis()
    if config.QDRANT_ENABLED:
        try:
            await vector_store.init_vector_store()
            asyncio.create_task(vector_store.sync_qdrant_from_db())
        except Exception:
            log.exception("Error al inicializar Qdrant (no bloquea el arranque)")


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
    await redis_client.close_redis()
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


@app.post("/maintenance/reset-all-conversations")
async def reset_all_conversations(
    x_reload_token: str = Header(None),
):
    if not config.RELOAD_TOKEN or x_reload_token != config.RELOAD_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    deleted = await db.clear_all_conversations()
    return {"status": "ok", "deleted": deleted}



async def _process_message_safe(payload: dict) -> None:
    try:
        await _process_message(payload)
    except Exception as exc:
        log.exception("Error procesando mensaje: %s", exc)
        try:
            msg = whatsapp_client.extract_message(payload)
            if msg and msg.get("wa_id"):
                wa_id = msg["wa_id"]
                await db.set_bot_paused(wa_id, False)
                user_txt = msg.get("text", "")
                candidatos = await catalog.buscar_producto_especifico(user_txt, limit=3)
                if candidatos:
                    c_names = "\n".join(f"📸 {c['nombre']} — ${c['precio']:,}" for c in candidatos)
                    fallback_text = f"Para ti tengo estas opciones disponibles 👇\n\n{c_names}\n\n¿Cuál de estos te llama más la atención? 😊"
                else:
                    fallback_text = "¡Hola! Con gusto te asesoro 😊 ¿Qué tipo de producto estás buscando hoy? Tenemos vibradores, fundas, lubricantes y más."
                await db.save_message(wa_id, "assistant", fallback_text)
                await whatsapp_client.send_text(wa_id, fallback_text)
        except Exception:
            log.exception("Error enviando mensaje de recuperación")


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
# TOLERANTE A ESPACIOS: el LLM a veces escribe "[ FOTO:64209 ]" con espacios
# dentro de los corchetes. Si la regex no matchea, el marcador NO se extrae ni
# se limpia → se envía crudo al cliente (bug reportado) y los IDs no se resuelven
# (se fuerzan productos del pipeline, a veces equivocados). Por eso el patrón
# permite espacios opcionales tras [ y antes de ].
_FOTO_MARKER_RE = re.compile(r"\[\s*FOTO:\s*([^\]]+?)\s*\]", re.IGNORECASE)

# Marcador de categoría: [CATEGORIA:Punto G] → envía fotos de esa subcategoría.
_CATEGORIA_MARKER_RE = re.compile(r"\[\s*CATEGORIA:\s*([^\]]+?)\s*\]", re.IGNORECASE)


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


# Preguntas de calificación deterministas por categoría funcional. Son la RED DE
# SEGURIDAD cuando el LLM, en un turno de calificación (debe_mostrar=False), incumple
# su contrato y escribe la plantilla "Mira estas opciones…" sin marcadores [FOTO:ID]
# (lo que dejaba al cliente con un mensaje que promete fotos pero no envía nada).
# Fuente: prompts/system.md (árboles de asesoría por categoría).
_PREGUNTAS_CALIFICACION = {
    "masturbadores": (
        "¡Claro que sí! Para mostrarte lo ideal, cuéntame: ¿buscas un **anillo vibrador** "
        "(para pareja/erección), un **masturbador/huevo** (placer personal), o una **funda "
        "para pene** (grosor/textura)? 😊"
    ),
    "anillos-y-fundas": (
        "¡Claro que sí! Para mostrarte lo ideal, cuéntame: ¿buscas un **anillo vibrador** "
        "(para pareja/erección), un **masturbador/huevo** (placer personal), o una **funda "
        "para pene** (grosor/textura)? 😊"
    ),
    "dildos": (
        "¡Claro que sí! Para mostrarte lo ideal, cuéntame: ¿buscas un dildo **realista** "
        "(textura piel), **con ventosa** (para superficie), de **vidrio/cristal**, o **doble**? 😊"
    ),
    "vibradores": (
        "¡Claro que sí! Para recomendarte lo ideal, cuéntame: ¿buscas estimulación para "
        "**ella** (clítoris/punto G), para **él** (pene/anillos vibradores), **anal/próstata**, "
        "o **en pareja**? 😊"
    ),
    "lubricantes-y-cuidado": (
        "¡Claro que sí! Para recomendarte el ideal, cuéntame: ¿lo buscas a **base de agua** "
        "(seguro con juguetes), de **silicona** (duradero), **anal desensibilizante**, o con "
        "**sabores/sensaciones** (calor/frío)? 😊"
    ),
    "anal": (
        "¡Claro que sí! Para recomendarte lo ideal, cuéntame: ¿es para **primera vez** "
        "(plug pequeño/cónico), estimulación de **próstata** (para él), o con **vibración/"
        "control remoto**? 😊"
    ),
    "lenceria": (
        "¡Claro que sí! Para mostrarte las opciones ideales, cuéntame: ¿buscas lencería para "
        "**ella** (body, baby doll, disfraz) o para **él** (suspensorio, pechera, conjunto "
        "masculino)? 😊"
    ),
    "succionadores": (
        "¡Claro que sí! Para recomendarte el ideal, cuéntame: ¿es para **primera vez** "
        "(suave/succión sutil), buscas **doble estimulación** (con vibración), o con **control "
        "por App**? 😊"
    ),
    "pareja-y-bondage": (
        "¡Claro que sí! Para recomendarte lo ideal, cuéntame: ¿buscas **kits de amarre**, "
        "**esposas**, **antifaces**, o **fustas/látigos**? 😊"
    ),
}

# Frases que delatan que el LLM escribió una plantilla de "mostrar productos" cuando
# NO debía (estaba en turno de calificación). Si el reply trae alguna de estas frases
# pero NO trae marcadores [FOTO:ID] válidos, es una contradicción a corregir.
_OFRECE_PRODUCTOS_RE = re.compile(
    r"(mira estas opciones|estas opciones disponibles|te muestro|te las muestro|"
    r"para ti tengo|para ti 👇|que tenemos disponibles|nuestras mejores opciones|"
    r"opciones de (anillos|vibradores|dildos|lubricantes|lenceria|lencería)|"
    r"de anillos y vibradores|de anillos|estás son|estas son|aquí tienes)",
    re.IGNORECASE,
)


async def _enviar_fotos_productos(
    wa_id: str,
    candidatos: list[dict],
) -> list[int]:
    """Envía las fotos de los candidatos confirmados por el pipeline.

    Los candidatos ya vienen validados por el sistema (categoría + género + stock
    + imagen), filtrados por los marcadores [FOTO:ID] que el LLM emitió y que
    estaban en la lista de candidatos. Esta función SOLO envía las imágenes.

    Devuelve la lista de IDs efectivamente enviados (para persistir en estado).
    Máximo 5 fotos por turno.
    """
    enviados: list[int] = []
    try:
        seen_ids: set[int] = set()
        for p in candidatos:
            if len(enviados) >= 5:
                break
            pid = p["id"]
            if pid in seen_ids:
                continue
            if not p.get("imagen_url"):
                log.warning("Candidato sin imagen_url, omitido: '%s' (id=%s)", p.get("nombre"), pid)
                continue
            seen_ids.add(pid)
            caption = f"📸 *{p['nombre']}*\n💰 ${p['precio']:,}"
            await whatsapp_client.send_image(wa_id, p["imagen_url"], caption)
            log.info("Foto de producto '%s' enviada a %s", p["nombre"], wa_id)
            enviados.append(pid)
            if len(enviados) < 5:
                await asyncio.sleep(0.8)
        log.info("Fotos a %s: %d enviadas de %d candidatos", wa_id, len(enviados), len(candidatos))
    except Exception:
        log.exception("Error enviando foto de producto a %s", wa_id)
    return enviados


def _resolver_candidatos_del_llm(
    foto_refs: list[str], candidatos: list[dict]
) -> list[dict]:
    """Filtra los marcadores [FOTO:ID] del LLM contra los candidatos confirmados.

    Pipeline determinístico: el LLM solo puede mostrar productos que estén en la
    lista de candidatos recuperados por el sistema. Esto elimina las alucinaciones
    de IDs que causaban fotos incoherentes (ej: Antifaz/Esposas al pedir anillo).

    - Cualquier ID numérico del LLM que NO esté en candidatos se descarta (log).
    - Referencias por nombre se resuelven contra candidatos por coincidencia.
    - Devuelve los candidatos en el orden que el LLM los mencionó (máx 5).
    """
    if not foto_refs:
        return []
    por_id = {p["id"]: p for p in candidatos}
    por_nombre: list[dict] = candidatos
    resueltos: list[dict] = []
    seen: set[int] = set()
    for ref in foto_refs[:5]:
        ref_s = ref.strip()
        # 1. ID numérico: válido solo si está en candidatos
        if ref_s.isdigit():
            pid = int(ref_s)
            if pid in por_id and pid not in seen:
                resueltos.append(por_id[pid])
                seen.add(pid)
                continue
            if pid not in por_id:
                log.warning("ID %d del LLM rechazado (no está en candidatos) — alucinación evitada", pid)
            continue
        # 2. Referencia por nombre: buscar coincidencia en candidatos
        ref_norm = catalog._normalizar_texto(ref_s)
        for p in por_nombre:
            if p["id"] in seen:
                continue
            nombre_norm = catalog._normalizar_texto(p.get("nombre", ""))
            if ref_norm and (ref_norm in nombre_norm or nombre_norm in ref_norm):
                resueltos.append(p)
                seen.add(p["id"])
                break
    return resueltos


# Respuestas afirmativas/ambiguas que, cuando ya hay una categoría persistida,
# SIEMPRE disparan mostrar fotos de esa categoría (rompe el bucle de preguntas
# cuando el cliente responde "si", "ok", "dame", etc. a la pregunta de calificación).
_RESPUESTAS_AFIRMATIVAS = {
    "si", "sí", "ok", "okay", "claro", "dame", "ver", "muestrame", "muéstrame",
    "muestramelos", "muéstramelos", "porfa", "por favor", "suena bien", "dale",
    "bueno", "adelante", "esta bien", "está bien", "sip", "siii", "ajá", "aha",
    "claro que si", "claro que sí", "genial", "perfecto", "excelente", "vamos",
    "adelantar", "muestra", "mandame", "mándame", "enviame", "envíame", "fotos",
    "foto", "imagenes", "imágenes", "verlos", "verlas", "cuales", "cuáles",
    "opciones", "catalogo", "catálogo",
}


def _es_respuesta_afirmativa(user_text: str) -> bool:
    """True si el mensaje es una respuesta corta afirmativa/ambigua que debe
    disparar mostrar la categoría persistida (no re-clasificar)."""
    t = catalog._normalizar_texto(user_text).strip().strip(".,!?¿¡")
    if not t:
        return False
    # Coincidencia exacta o el mensaje contiene una afirmativa clave.
    if t in _RESPUESTAS_AFIRMATIVAS:
        return True
    # "dame los rojos", "ver los sencillos", etc. → contiene verbo afirmativo.
    for af in ("dame", "muestrame", "muéstrame", "mandame", "mándame", "enviame", "envíame"):
        if af in t:
            return True
    return False


# Patrones que indican que el cliente está en FASE DE VENTA/PAGO (no explorando
# productos). En estos turnos NO se envían fotos: el cliente ya eligió o está
# dando datos/pagando. Evita el ruido de reenviar productos durante el checkout.
_FASE_VENTA_RE = re.compile(
    r"\b(nequi|daviplata|bancolombia|bold|pago|pagar|pague|transferencia|"
    r"comprobante|comprobant|envio|envío|despacho|despachar|direcci[oó]n|"
    r"telefono|tel[eé]fono|celular|nombre completo|ciudad|barrio|"
    r"cuesta|cuanto cuesta|cu[aá]nto|valor|total|yappi|yapy|"
    r"pedido|lo quiero|lo compro|lo llevo|me lo llevo|ese quiero|"
    r"ya pague|ya pagu|ya transferi|ya transferí|adjunto|adjunt|"
    r"comprobante de pago|efectivo|contra ?entrega)\b",
    re.IGNORECASE,
)


def _es_fase_venta(user_text: str, history: list[dict]) -> bool:
    """Detecta si el cliente está en fase de venta/pago (no de exploración).

    True si el mensaje actual habla de pago/datos/envío, O si en los últimos
    turnos el bot ya pidió datos de envío / cerró venta (el cliente ya eligió y
    está dando sus datos → no es momento de mandar más fotos de productos).
    """
    texto = user_text or ""
    if _FASE_VENTA_RE.search(texto):
        return True
    # Mirar últimos turnos del bot: si pidió datos de envío o cerró venta,
    # estamos en checkout aunque el mensaje actual sea corto ("miltn zambrano...").
    for m in reversed(history[-6:]):
        if m.get("role") != "assistant":
            continue
        c = (m.get("content") or "").lower()
        if any(w in c for w in ("nombre completo", "direcci", "teléfono de contacto",
                                "teléfono", "ciudad", "pedido", "datos de envío",
                                "datos de envio", "nequi", "daviplata", "bancolombia",
                                "pago", "total", "[[pedido")):
            return True
    return False


async def _recuperar_candidatos(
    user_text: str, history: list[dict], estado: dict | None,
) -> tuple[list[dict], dict]:
    """Núcleo del pipeline determinístico: clasifica la intención del cliente,
    la fusiona con el estado persistido, y recupera los productos correctos.

    Devuelve (candidatos, info_clasificacion):
      - candidatos: lista de productos confirmados (vacía si hay que calificar).
      - info_clasificacion: dict con la clasificación resultante + estado a guardar.

    Enfoque híbrido anti-bucle: si ya hay una categoría persistida (el bot ya
    preguntó en el turno anterior), cualquier respuesta que NO introduzca una
    categoría NUEVA y distinta dispara mostrar fotos de esa categoría. Así
    "si", "ok", "dame", "los rojos" muestran productos en vez de re-preguntar.
    """
    clasif = await catalog.clasificar_intencion_cliente(user_text, history)

    # Fusionar con estado previo (el estado recalifica categoría/género si el
    # nuevo mensaje los aclara; si no, conserva lo persistido).
    cat_func = clasif["categoria_funcional"]
    genero = clasif["genero"]
    intencion = clasif["intencion"]
    estado_tiene_cat = bool(estado and estado.get("categoria_funcional"))

    if estado:
        if not cat_func and estado.get("categoria_funcional"):
            cat_func = estado["categoria_funcional"]
            intencion = intencion or estado.get("categoria_busqueda")
        if not genero and estado.get("genero"):
            genero = estado["genero"]

    # Detectar cambio radical de tema: si la nueva intención es distinta y clara
    # respecto a la persistida, reiniciar el estado para no mezclar productos.
    # Esto es CRÍTICO: si el cliente vio dildos y ahora pide "lubricantes", debe
    # resetear el estado (productos_mostrados, calificado) para no mezclar dildos
    # con lubricantes en los candidatos.
    reset_state = False
    nueva_cat_clara = bool(clasif["categoria_funcional"] and clasif["intencion"])
    if (estado_tiene_cat and nueva_cat_clara
            and clasif["categoria_funcional"] != estado.get("categoria_funcional")):
        log.info("Cambio de tema detectado: %s -> %s — reseteando estado",
                 estado.get("categoria_funcional"), clasif["categoria_funcional"])
        reset_state = True
        estado = None  # ignorar el estado viejo para la recuperación
        estado_tiene_cat = False

    # ── REGLA HÍBRIDA ANTI-BUCLE ──
    # Si ya hay categoría persistida (el bot preguntó en el turno anterior) y el
    # cliente responde sin introducir una categoría nueva/distinta, mostrar fotos
    # de esa categoría. Esto cubre respuestas afirmativas ("si","ok","dame") y
    # atributos ("rojos","sencillo") que antes caían en re-pregunta infinita.
    mostrar_por_estado = False
    if estado_tiene_cat and not reset_state:
        afirmativa = _es_respuesta_afirmativa(user_text)
        # Mostrar si: ya estaba calificado, o es afirmativa, o pide fotos, o el
        # cliente aclara género/subtipo sobre la misma categoría.
        if (estado.get("calificado") or afirmativa or clasif["pide_fotos"]):
            mostrar_por_estado = True
            clasif["calificado"] = True
        elif nueva_cat_clara and clasif["categoria_funcional"] == estado.get("categoria_funcional"):
            # Mismo tema con más detalle (ej: ya en lencería, ahora dice "body").
            mostrar_por_estado = True
            clasif["calificado"] = True

    # ¿Hay que mostrar fotos? Sí si: ya calificado, pide fotos, o regla híbrida.
    debe_mostrar = bool(clasif["calificado"] or clasif["pide_fotos"] or mostrar_por_estado)

    # REGLA ANTI-RUIDO EN CHECKOUT: si el cliente está en fase de venta/pago
    # (dio datos, habla de nequi/comprobante, o el bot ya pidió datos de envío),
    # NO mostrar fotos. El cliente ya eligió su producto; reenviar opciones ahora
    # es ruido que confunde y rompe la venta. Las fotos vuelven solo cuando el
    # cliente haga una consulta NUEVA de productos (el detector de cambio de tema
    # reinicia el estado).
    if debe_mostrar and _es_fase_venta(user_text, history):
        debe_mostrar = False
        clasif["calificado"] = False
        log.info("Fase de venta detectada — fotos desactivadas este turno")

    candidatos: list[dict] = []
    exclude = estado.get("productos_mostrados", []) if estado else []

    # Si el cliente pidió un PRODUCTO ESPECÍFICO por marca/modelo (Lovense Lush,
    # Satisfyer Pro 2), buscar por NOMBRE primero (más preciso que categoría).
    # Así "Lovense Lush" muestra Lovense, no vibradores genéricos al azar.
    if clasif.get("es_especifico") and debe_mostrar:
        especificos = await catalog.buscar_producto_especifico(
            user_text, limit=5, exclude_ids=exclude)
        if especificos:
            candidatos = especificos
            log.info("Producto específico encontrado por nombre: %d", len(candidatos))

    if not candidatos and debe_mostrar and cat_func:
        # Excluir productos ya mostrados en la conversación para no repetir fotos.
        # Pasar el subtipo detectado para priorizar productos que lo cumplan
        # (exactitud: "doble" → dildos dobles primero, "ventosa" → con ventosa).
        candidatos = await catalog.get_productos_para_recomendar(
            categoria_funcional=cat_func,
            genero=genero,
            user_text=user_text,
            exclude_ids=exclude,
            limit=5,
            subtipo=clasif.get("subtipo_detectado"),
        )

    # Si subtipo SOFT (primera vez, sencillo, suave...) no dio matches estrictos, relajar a categoría sin subtipo.
    # Evita el bug "primera vez" -> 0 resultados -> handoff.
    if not candidatos and clasif.get("subtipo_detectado"):
        _sub = clasif.get("subtipo_detectado")
        try:
            es_soft = catalog._es_subtipo_soft(_sub)
        except AttributeError:
            es_soft = False
        if es_soft:
            log.info("Subtipo soft %r sin matches, mostrando categoria %s completa para no dejar sin fotos", _sub, cat_func)
            candidatos = await catalog.get_productos_para_recomendar(
                categoria_funcional=cat_func,
                genero=genero,
                user_text=user_text,
                exclude_ids=exclude,
                limit=5,
                subtipo=None,
            )
            if not candidatos and debe_mostrar and cat_func:
                candidatos = await catalog.get_productos_para_recomendar(
                    categoria_funcional=cat_func,
                    genero=None,
                    user_text=user_text,
                    exclude_ids=exclude,
                    limit=5,
                )

    # Si se especificó un subtipo HARD (ej: "duchas anales") y no hay candidatos, NO ejecutar
    # fallbacks a otras categorías irrelevantes (como Arneses Strap-On). Para SOFT ya se intentó arriba.
    if not candidatos and not clasif.get("subtipo_detectado"):
        # BLINDAJE ANTI-BUCLE: si el cliente está respondiendo a una pregunta de
        # calificación (estado.calificado=True, debe_mostrar=True) PERO no se
        # encontraron candidatos (género/subtipo restrictivo no matchea nada),
        # buscar productos de la categoría RELAJADO AL MÁXIMO (sin género, sin
        # subtipo). Garantiza que cualquier respuesta a una calificación muestre
        # productos en vez de re-preguntar infinitamente.
        if debe_mostrar and cat_func and estado_tiene_cat and estado.get("calificado"):
            log.info("Blindaje anti-bucle: buscando %s relajado (género=%s→None)", cat_func, genero)
            candidatos = await catalog.get_productos_para_recomendar(
                categoria_funcional=cat_func,
                genero=None,  # relajar género
                user_text=user_text,
                exclude_ids=exclude,
                limit=5,
            )

        # Si no hay categoría clara pero el cliente pidió fotos y hay un sustantivo,
        # intentar recuperación por el sustantivo (fallback).
        if not candidatos and clasif["pide_fotos"] and clasif["sustantivo"]:
            cat_func_fb = catalog._INTENCION_A_CATEGORIA_FUNCIONAL.get(
                clasif["sustantivo"],
                catalog._categoria_normalizada(clasif["sustantivo"], "", ""),
            )
            if cat_func_fb and cat_func_fb != "juegos-y-accesorios":
                candidatos = await catalog.get_productos_para_recomendar(
                    categoria_funcional=cat_func_fb, genero=genero,
                    user_text=user_text, exclude_ids=exclude, limit=5,
                )
                if candidatos:
                    cat_func = cat_func_fb

        # Si no hay candidatos por categoría, intentar búsqueda por nombre:
        if not candidatos and (debe_mostrar or clasif.get("es_especifico")):
            especificos = await catalog.buscar_producto_especifico(
                user_text, limit=5, exclude_ids=exclude)
            if especificos:
                candidatos = especificos

        if not candidatos and debe_mostrar:
            # Último recurso: buscar por el sustantivo/intención detectada.
            termino = clasif["sustantivo"] or intencion or cat_func
            if termino:
                especificos2 = await catalog.buscar_producto_especifico(
                    termino, limit=5, exclude_ids=exclude)
                if especificos2:
                    candidatos = especificos2
                    log.info("Candidatos recuperados por término fallback %r: %d", termino, len(candidatos))

    # Detectar CATEGORÍA/SUBTIPO AGOTADO: el cliente pidió "ver más" o un subtipo específico
    # cuyas opciones disponibles en inventario ya fueron totalmente mostradas en mensajes previos.
    categoria_agotada = bool(
        not candidatos and (clasif["pide_fotos"] or clasif.get("subtipo_detectado")) and bool(exclude) and cat_func
    )

    _subtipo_actual = clasif.get("subtipo_detectado")
    _es_soft_actual = False
    if _subtipo_actual:
        try:
            _es_soft_actual = catalog._es_subtipo_soft(_subtipo_actual)
        except AttributeError:
            _es_soft_actual = False

    info = {
        "intencion": intencion,
        "categoria_funcional": cat_func,
        "genero": genero,
        "calificado": clasif["calificado"] or (debe_mostrar and bool(candidatos)),
        "pide_fotos": clasif["pide_fotos"],
        "reset_state": reset_state,
        "categoria_agotada": categoria_agotada,
        "sin_mas_opciones": bool(candidatos and len(candidatos) < 5),
        "sin_stock_subtipo": bool(_subtipo_actual and not candidatos and not exclude and not _es_soft_actual),
        "debe_mostrar": debe_mostrar and bool(candidatos),
    }
    return candidatos, info


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
    # atienden en secuencia (nunca en paralelo). Usa Redis lock distribuido o fallback en memoria.
    if redis_client.is_redis_available():
        acquired = await redis_client.acquire_user_lock(wa_id)
        if not acquired:
            log.info("Mensaje para %s pospuesto: lock distribuido activo en Redis", wa_id)
            await asyncio.sleep(0.5)
            acquired = await redis_client.acquire_user_lock(wa_id)
        if acquired:
            try:
                await _handle_message(msg, wa_id)
            finally:
                await redis_client.release_user_lock(wa_id)
        else:
            log.warning("No se pudo adquirir lock Redis para %s — omitiendo ejecución recurrente", wa_id)
    else:
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
        image_url = msg.get("image_url")
        if image_url:
            handled = await payments.handle_comprobante_imagen(
                wa_id=wa_id,
                image_url=image_url,
                caption=user_text,
                history=history,
            )
            if handled:
                log.info("Comprobante de pago procesado por visión para %s", wa_id)
                return
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
    # Agrupación de mensajes rápidos del mismo número
    if user_text.strip():
        if redis_client.is_redis_available():
            count = await redis_client.push_user_message(wa_id, user_text.strip())
            if count == 1:
                await asyncio.sleep(config.REDIS_BUFFER_WAIT_SECONDS)
                mensajes = await redis_client.pop_all_user_messages(wa_id)
                if mensajes:
                    user_text = " ".join(mensajes)
                    log.info("Mensajes agrupados en Redis para %s (%d): %r", wa_id, len(mensajes), user_text[:80])
            else:
                await follow_ups.cancel(wa_id)
                log.info("Mensaje agregado al buffer Redis de %s, esperando procesamiento principal", wa_id)
                return
        else:
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

    # ── PIPELINE DETERMINÍSTICO ──
    # El SISTEMA (no el LLM) clasifica la intención del cliente, la fusiona con
    # el estado persistido, y recupera los productos CORRECTOS de la DB. El LLM
    # solo redacta con los candidatos confirmados. Así se eliminan:
    #  - fotos incoherentes (los IDs se validan contra candidatos reales),
    #  - el bucle de preguntas (el estado controla si ya se calificó),
    #  - la dependencia de un catálogo gigante en el prompt.
    estado_previo = await db.get_conversation_state(wa_id)
    candidatos, info = await _recuperar_candidatos(user_text, history, estado_previo)

    # ESCALAMIENTO POR PRODUCTO/SUBTIPO NO DISPONIBLE:
    # Si el cliente especificó un subtipo (ej: "duchas anales") y no hay candidatos
    # en stock, NO enviar productos no relacionados. Pausar el bot y escalar a humano.
    if info.get("sin_stock_subtipo"):
        HANDOFF_NO_STOCK = (
            "En este momento no cuento con ese producto o diseño específico en inventario 😔. "
            "Te estoy transfiriendo con un asesor humano para que te ayude a consultar disponibilidad "
            "o buscar la mejor alternativa 💬"
        )
        await db.set_bot_paused(wa_id, True)
        await db.save_message(wa_id, "user", user_text)
        await db.save_message(wa_id, "assistant", HANDOFF_NO_STOCK)
        await whatsapp_client.send_text(wa_id, HANDOFF_NO_STOCK)
        await escalations.record_if_escalated(
            wa_id=wa_id, user_text=user_text, bot_reply=HANDOFF_NO_STOCK,
            message_type="text", media_type=None, history=history,
        )
        log.info("Handoff por producto/subtipo sin stock (%s) para %s — bot pausado",
                 info.get("intencion") or user_text, wa_id)
        return

    if info["reset_state"] and estado_previo:
        await db.upsert_conversation_state(wa_id, reset=True)

    # Resolver los IDs de productos_mostrados a nombres+precios reales del catálogo.
    # El LLM necesita esta información cuando confirma el pedido: cuando el cliente
    # confirma un producto (ej: reply a la foto "quiero esta"), el LLM debe usar el
    # precio EXACTO del catálogo, no adivinarlo del historial (bug: $55.000 por $29.900).
    productos_detalle_estado = ""
    ids_mostrados = (estado_previo or {}).get("productos_mostrados", [])
    if ids_mostrados:
        detalle_lines = []
        for pid in ids_mostrados[-10:]:
            p = await catalog.get_producto_by_id(pid)
            if p:
                detalle_lines.append(f"  • {p['nombre']} — ${p['precio']:,}")
        if detalle_lines:
            productos_detalle_estado = "\n".join(detalle_lines)

    raw_reply = await openai_client.complete(
        user_text, history,
        lead=lead, summary=summary_text,
        candidatos=candidatos if info["debe_mostrar"] else [],
        estado={
            "categoria_busqueda": info["intencion"],
            "categoria_funcional": info["categoria_funcional"],
            "genero": info["genero"],
            "calificado": info["calificado"],
            "categoria_agotada": info.get("categoria_agotada", False),
            "productos_mostrados": ids_mostrados,
            "productos_con_precios": productos_detalle_estado,
        },
        debe_mostrar_fotos=info["debe_mostrar"],
    )
    reply = await leads.process_reply(
        wa_id,
        raw_reply,
        history + [{"role": "user", "content": user_text}],
    )

    # Extraer marcadores de foto [FOTO:ID] y limpiarlos del texto visible.
    foto_ids, reply = _extraer_marcadores_foto(reply)
    _, reply = _extraer_marcadores_categoria(reply)

    # Limpiar el marcador [[PEDIDO_DATOS:...]] del texto visible. Es un marcador
    # INTERNO (datos estructurados para crear el pedido) y jamás debe verse en el
    # chat. Se limpia aquí SIEMPRE, antes de enviar, sin importar el flujo.
    reply = re.sub(r"\[\[PEDIDO_DATOS:[^\]]*\]\]", "", reply).strip()
    reply = re.sub(r"[ \t]{2,}", " ", reply)

    # VALIDAR candidatos: los [FOTO:ID] del LLM deben estar en la lista de
    # candidatos confirmados. Esto elimina alucinaciones (ej: Antifaz/Esposas
    # cuando el cliente pidió anillo). Si el LLM no emitió marcadores válidos
    # pero teníamos candidatos, se inyectan los del sistema.
    # IMPORTANTE: se calcula ANTES de la guardia de calificación para que esa
    # guardia sepa cuántos productos se enviarán REALMENTE (no solo si el LLM
    # puso marcadores brutos, que pueden ser IDs alucinados y descartarse aquí).
    if info["debe_mostrar"] and candidatos:
        final_productos = _resolver_candidatos_del_llm(foto_ids, candidatos)
        if len(final_productos) < 2:
            # El LLM omitió las fotos o puso IDs inválidos: forzar candidatos.
            log.info(
                "Fotos forzadas desde candidatos del sistema (%d) — LLM omitió marcadores",
                len(candidatos),
            )
            final_productos = candidatos[:5]
    else:
        # No debía mostrar fotos: el LLM solo califica. Si por error emitió
        # marcadores, validarlos igualmente contra candidatos (vacío = nada).
        final_productos = _resolver_candidatos_del_llm(foto_ids, candidatos)

    # RED DE SEGURIDAD — turno de calificación: si el sistema decidió NO mostrar
    # fotos (debe_mostrar=False) porque toca calificar, pero el LLM incumplió y
    # escribió una plantilla de "mostrar productos" ("Mira estas opciones…") SIN
    # fotos que se vayan a enviar realmente, corregimos reemplazando por la
    # pregunta determinista de esa categoría. Se evalúa sobre final_productos
    # (marcadores válidos) — no sobre foto_ids brutos — para cerrar el hueco del
    # marcador [FOTO] espurio (el LLM puede alucinar [FOTO:999] sin candidatos;
    # al resolver contra candidatos vacíos final_productos queda vacío y la
    # guardia SÍ dispara, evitando el estado roto "ofrece pero no envía nada").
    if (not info["debe_mostrar"]
            and info["categoria_funcional"]
            and not final_productos
            and _OFRECE_PRODUCTOS_RE.search(reply)):
        pregunta = _PREGUNTAS_CALIFICACION.get(info["categoria_funcional"])
        if pregunta:
            log.info(
                "LLM omitió la pregunta de calificación para '%s' (escribió plantilla sin "
                "fotos válidas) — pregunta determinista inyectada",
                info["categoria_funcional"],
            )
            reply = pregunta

    # DEFENSA A PRUEBA DE FALLOS — filtro FINAL de productos ya mostrados.
    # Aunque la exclusión se propaga por todos los caminos de _recuperar_candidatos,
    # este es el PUNTO ÚNICO de garantía: nunca se envía una foto de un producto
    # que ya se mostró en un turno anterior. Cubre todos los caminos (query
    # principal, fallbacks, Intento E-bis, etc.) sin depender de que cada uno
    # respete exclude_ids. Es la red definitiva anti-repetición ("ver más").
    ids_ya_mostrados = set((estado_previo or {}).get("productos_mostrados", []))
    if final_productos:
        ids_a_enviar = [p["id"] for p in final_productos]
        repetidos = [pid for pid in ids_a_enviar if pid in ids_ya_mostrados]
        # Log de diagnóstico SIEMPRE: confirma cuántos ya mostrados había y cuántos
        # se van a enviar. Si los ya-mostrados llegan vacíos en un "ver más", indica
        # que el estado no se persistió (problema upstream), no del filtro.
        log.info(
            "Filtro final [%s]: ya_mostrados=%d (%s), candidatos=%d, repetidos=%d",
            wa_id, len(ids_ya_mostrados), sorted(ids_ya_mostrados)[:8],
            len(final_productos), len(repetidos),
        )
        if repetidos:
            log.warning(
                "Filtro final: %d producto(s) repetidos removidos antes de enviar a %s: %s",
                len(repetidos), wa_id, repetidos,
            )
            final_productos = [p for p in final_productos if p["id"] not in ids_ya_mostrados]

    # Si tras el filtro final no quedan productos NUEVOS que enviar (todo ya fue
    # mostrado) y el bot iba a mostrar fotos (debe_mostrar) o ya redactó una
    # plantilla de "mostrar productos", reescribimos el reply con un mensaje
    # honesto: la categoría está agotada. Así el cliente nunca recibe la misma
    # foto repetida ni un "mira estas opciones" sin fotos nuevas.
    if ids_ya_mostrados and not final_productos and not pedido_creado_id:
        if info["debe_mostrar"] or _OFRECE_PRODUCTOS_RE.search(reply) or foto_ids:
            log.warning(
                "Categoría agotada para %s (cat=%s): todos los candidatos ya fueron "
                "mostrados — reescribiendo reply con mensaje honesto",
                wa_id, info["categoria_funcional"],
            )
            cat_nombres = {
                "vibradores": "vibradores", "dildos": "dildos", "anal": "juguetes anales",
                "masturbadores": "masturbadores", "anillos-y-fundas": "anillos y fundas",
                "lubricantes-y-cuidado": "lubricantes", "lenceria": "lencería",
                "succionadores": "succionadores", "pareja-y-bondage": "productos de pareja",
                "juegos-y-accesorios": "accesorios",
            }
            nombre_cat = cat_nombres.get(info["categoria_funcional"], "ese producto")
            reply = (
                f"Te mostré todas las opciones de {nombre_cat} que tenemos disponibles "
                f"😊 ¿Te gustó alguno de los que viste, o te interesa que te ayude con "
                f"otro tipo de producto? Tenemos vibradores, lencería, lubricantes y más."
            )

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

    # Enviar imagen de medios de pago si se creó el pedido o la respuesta contiene datos bancarios
    es_mensaje_pago = any(w in reply for w in ("INFORMACIÓN DE PAGOS", "PIGELI GROUP SAS", "05400003434", "@pigeli06", "PAGO A CUENTA BANCARIA"))
    if (pedido_creado_id or es_mensaje_pago) and config.PAYMENT_INFO_IMAGE_URL:
        try:
            await whatsapp_client.send_image(
                to_wa_id=wa_id,
                image_url=config.PAYMENT_INFO_IMAGE_URL,
                caption="💳 Medios de pago autorizados — Tu Deseo",
            )
            log.info("Imagen de medios de pago enviada a %s", wa_id)
        except Exception:
            log.exception("Error enviando imagen de medios de pago a %s", wa_id)

    # Enviar fotos (candidatos ya validados + filtrados por el filtro final).
    enviados_ids = await _enviar_fotos_productos(wa_id, final_productos)

    # GARANTÍA ESTRUCTURAL "nunca prometer sin enviar": si el sistema decidió
    # mostrar fotos (debe_mostrar=True) pero al final NO se envió ninguna
    # (candidatos sin imagen_url, categoría vacía, typo, LLM rebelde), no podemos
    # dejar al cliente con un texto que promete opciones que nunca llegaron.
    # Enviamos un mensaje de recuperación honesto y, si aplica, la pregunta de
    # calificación de la categoría. Esto es la red final: cubre TODAS las causas
    # de "0 fotos" y evita el estado roto ("ofrece pero no envía nada").
    if info["debe_mostrar"] and not enviados_ids and not pedido_creado_id:
        log.warning(
            "Bot prometió fotos a %s pero se enviaron 0 (cat=%s, género=%s) — "
            "recuperando con mensaje honesto",
            wa_id, info["categoria_funcional"], info["genero"],
        )
        cat_nombres = {
            "vibradores": "vibradores", "dildos": "dildos", "anal": "juguetes anales",
            "masturbadores": "masturbadores", "anillos-y-fundas": "anillos y fundas",
            "lubricantes-y-cuidado": "lubricantes", "lenceria": "lencería",
            "succionadores": "succionadores", "pareja-y-bondage": "productos de pareja",
            "juegos-y-accesorios": "accesorios",
        }
        nombre_cat = cat_nombres.get(info["categoria_funcional"], "ese producto")
        # Si toca calificar (género sin aclarar), mejor enviar la pregunta; si no,
        # mensaje honesto de derivación (no perder la venta: el equipo confirma).
        if info["genero"] is None and info["categoria_funcional"] in _PREGUNTAS_CALIFICACION:
            recuperacion = _PREGUNTAS_CALIFICACION[info["categoria_funcional"]]
        else:
            recuperacion = (
                f"Te cuento que estoy confirmando con el equipo cuáles opciones de "
                f"{nombre_cat} tenemos disponibles ahora mismo 😊 Dame un segundo y te "
                f"envío las fotos. ¿O prefieres que te ayude con algo más mientras tanto?"
            )
        await db.save_message(wa_id, "assistant", recuperacion)
        await whatsapp_client.send_text(wa_id, recuperacion)

    # Persistir estado de conversación: registrar categoría/género/calificación
    # y los productos efectivamente mostrados.
    # IMPORTANTE: si el bot acaba de calificar (tiene categoría pero no mostró
    # fotos), marcamos calificado=True para que el SIGUIENTE turno sepa que ya
    # preguntó y debe mostrar fotos ante cualquier respuesta del cliente. Esto
    # rompe el bucle de re-preguntas infinitas ("si" → vuelve a preguntar).
    if info["categoria_funcional"] or enviados_ids:
        recien_califico = bool(info["categoria_funcional"] and not enviados_ids)
        await db.upsert_conversation_state(
            wa_id,
            categoria_busqueda=info["intencion"],
            categoria_funcional=info["categoria_funcional"],
            genero=info["genero"],
            calificado=info["calificado"] or bool(enviados_ids) or recien_califico,
            add_productos_mostrados=enviados_ids,
        )

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
