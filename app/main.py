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

    # Calcular las facetas de los productos que aún no las tienen (ver
    # app/facetas.py). Va en background para no retrasar el arranque: las reglas
    # resuelven la gran mayoría al instante y solo los dudosos consultan al LLM.
    # Sin esto, tras un despliegue los productos quedarían sin clasificar y la
    # búsqueda por restricciones no encontraría nada.
    async def _clasificar_catalogo_pendiente():
        try:
            from app import facetas, woocommerce
            pendientes = await db.get_productos_sin_clasificar(limit=2000)
            if not pendientes:
                return
            log.info("Calculando facetas de %d productos…", len(pendientes))
            con_llm = 0
            for p in pendientes:
                f, origen = await facetas.clasificar(
                    p["nombre"], p.get("descripcion"), p.get("categoria"),
                    permitir_llm=False)
                if f.tipo:
                    await db.set_facetas_producto(p["id"], f, origen=origen)
            # Los que las reglas no deciden (unos pocos) sí van al LLM.
            con_llm = await woocommerce._clasificar_pendientes_con_llm(limite=80)
            log.info("Facetas calculadas: %d por reglas, %d por LLM",
                     len(pendientes) - con_llm, con_llm)
        except Exception:
            log.exception("No se pudieron calcular las facetas (no bloquea el arranque)")

    asyncio.create_task(_clasificar_catalogo_pendiente())

    if config.WOOCOMMERCE_SYNC_ENABLED and config.WOOCOMMERCE_AUTO_SYNC:
        try:
            from app import woocommerce
            log.info("WooCommerce auto-sync activado: sincronizacion completa en background (delay para no afectar Hostinger)...")
            async def _delayed_woo_sync():
                await asyncio.sleep(30)
                await woocommerce.sync_catalog_from_woocommerce(full_replace=False)
                if config.QDRANT_ENABLED:
                    await vector_store.sync_qdrant_from_db()
            asyncio.create_task(_delayed_woo_sync())
        except Exception:
            log.exception("No se pudo iniciar sincronizacion WooCommerce")
    elif config.WOOCOMMERCE_SYNC_ENABLED:
        log.info("WooCommerce sync habilitado pero auto-sync desactivado (Hostinger safe) - usa webhook o endpoint manual")

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
                try:
                    recent = await db.get_history(wa_id, 2)
                    if recent:
                        last = recent[-1] if recent else None
                        if last and last.get("role") == "assistant":
                            log.info("Fallback omitido para %s: ya se envio mensaje en este turno", wa_id)
                            return
                except Exception:
                    pass
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

# Lista numerada de productos con precio ("1️⃣ Nombre — $80.000"). Es la forma
# más directa de detectar que el LLM está ofreciendo productos concretos, incluso
# si no usó ninguna de las frases de _OFRECE_PRODUCTOS_RE. Si aparece sin fotos
# reales detrás, son productos inventados.
_LISTA_PRODUCTOS_RE = re.compile(r"[1-9]️⃣[^\n]*\$\s*[\d.,]+")

# Mensaje honesto cuando el cliente pide algo que no se pudo resolver contra el
# catálogo. Coherente con la regla "nunca digas 'no tenemos' sin verificar" del
# system prompt: no niega el producto, ofrece confirmar con el equipo.
_SIN_RESULTADO_MSG = (
    "Déjame confirmar con el equipo si tenemos ese producto disponible 😊 "
    "Mientras tanto, ¿te gustaría que te muestre otras opciones? Tenemos "
    "vibradores, dildos, lencería, lubricantes y más."
)


_KEYCAPS = ("0️⃣", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣")


def _numero_lista(n: int) -> str:
    """Numeración visual para las listas de producto.

    1..10 usan el keycap correspondiente; a partir de 11 se concatenan los dígitos
    (12 → 1️⃣2️⃣). Se mantiene siempre el carácter de keycap porque
    `_es_seleccion_de_lista_mostrada` lo usa para reconocer que el bot mostró una
    lista numerada en el turno anterior.
    """
    if n == 10:
        return "🔟"
    if 1 <= n < 10:
        return _KEYCAPS[n]
    return "".join(_KEYCAPS[int(d)] for d in str(n))


def _texto_desde_candidatos(candidatos: list[dict], info: dict,
                            mas_disenos: bool = False, offset: int = 0) -> str:
    """Redacta la lista de productos desde los candidatos reales del catálogo.

    Nombres, precios y marcadores [FOTO:ID] salen del mismo sitio del que salen
    las fotos, así que texto y fotos no pueden desalinearse. Se usa cuando el
    cliente pide "ver más" y como reemplazo si el LLM redacta productos que no
    corresponden a los que se van a enviar.
    """
    cat_nombre = (info.get("intencion") or info.get("categoria_funcional")
                  or "productos").replace("-", " ")
    lineas, marcadores = [], []
    # offset: cuántos productos ya vio el cliente en esta misma búsqueda. La
    # numeración CONTINÚA (6️⃣, 7️⃣…) en vez de reiniciar en 1️⃣, porque al final se
    # le pide "indícame los números de los que quieres" y dos productos distintos
    # con el mismo número hacen ambiguo el pedido.
    for idx, p in enumerate(candidatos[:5], 1 + offset):
        precio_fmt = f"{int(p.get('precio', 0)):,}".replace(",", ".")
        lineas.append(f"{_numero_lista(idx)} *{p['nombre'][:60]}* — ${precio_fmt}")
        marcadores.append(f"[FOTO:{p['id']}]")
    # Si se cedió en alguna restricción, decirlo. Callarlo es lo que hacía que un
    # cliente pidiera "vibrador anal" y recibiera otra cosa sin explicación.
    aviso = ""
    relajado = info.get("relajado")
    if relajado and relajado not in ("todo", "sin_resultado"):
        pedido = info.get("restricciones") or {}
        _ZONAS_EN_TEXTO = {
            "anal": "anales", "clitoris": "de clítoris", "vaginal": "vaginales",
            "pene": "para el pene", "pezones": "para pezones", "cuerpo": "corporales",
        }
        _TIPOS_EN_TEXTO = {
            "vibrador": "vibradores", "succionador": "succionadores", "plug": "plugs",
            "dildo": "dildos", "anillo": "anillos", "funda": "fundas",
            "masturbador": "masturbadores", "bomba": "bombas", "arnes": "arneses",
            "enema": "duchas anales", "lubricante": "lubricantes", "bolas": "bolas",
            "lenceria": "prendas", "bondage": "artículos de bondage", "juego": "juegos",
        }
        # Se describe lo que el cliente PIDIÓ, no la etiqueta interna: decir
        # "No tengo exactamente anal para anal" (lo que salía antes) no es
        # castellano ni le dice nada al cliente.
        que_pidio = _TIPOS_EN_TEXTO.get(pedido.get("tipo") or "", cat_nombre)
        if relajado == "zona" and pedido.get("zona"):
            que_pidio += " " + _ZONAS_EN_TEXTO.get(pedido["zona"], "")
        elif relajado == "vibra":
            que_pidio += " con vibración"
        elif relajado == "control":
            que_pidio += " con ese tipo de control"
        elif relajado == "tipo" and pedido.get("zona"):
            que_pidio = _ZONAS_EN_TEXTO.get(pedido["zona"], que_pidio)
        aviso = (f"No tengo exactamente {que_pidio.strip()} en este momento, "
                 f"pero mira estas opciones muy parecidas 👇\n")

    # El CTA depende de si QUEDAN opciones sin mostrar. Ofrecer "ver más diseños"
    # cuando ya se mostró todo lleva al cliente a un callejón sin salida (y antes
    # provocaba que el sistema rellenara con productos que no cumplían).
    if info.get("hay_mas"):
        intro = (f"¡Buena elección! Aquí tienes más diseños de {cat_nombre} 👇"
                 if mas_disenos else f"¡Buena elección! Te muestro estas opciones de {cat_nombre} 👇")
        cta = "¿Cuál te gusta o deseas ver más diseños? 😊"
    else:
        intro = (f"¡Perfecto! Te muestro los últimos diseños de {cat_nombre} 👇"
                 if mas_disenos else f"¡Perfecto! Estas son las opciones de {cat_nombre} que tenemos 👇")
        cta = "¿Cuál de estos te gustaría llevar? 😊"
    # Con aviso, el aviso ES la entrada: encadenar los dos dejaba dos saludos
    # seguidos ("No tengo exactamente… 👇" + "¡Buena elección!… 👇").
    encabezado = aviso.rstrip("\n") if aviso else intro
    return f"{encabezado}\n" + "\n".join(lineas) + f"\n\n{' '.join(marcadores)}\n\n{cta}"


async def _enviar_fotos_productos(
    wa_id: str,
    candidatos: list[dict],
    offset: int = 0,
) -> list[int]:
    enviados: list[int] = []
    fallidos: list[int] = []
    seen_ids: set[int] = set()
    for idx, p in enumerate(candidatos, 1 + offset):
        if len(enviados) >= 5:
            break
        pid = p.get("id")
        if not pid or pid in seen_ids:
            continue
        if not p.get("imagen_url"):
            log.warning("Candidato sin imagen_url omitido id=%s", pid)
            continue
        seen_ids.add(pid)
        precio = p.get("precio", 0)
        precio_fmt = f"{int(precio):,}".replace(",", ".") if precio else "0"
        nombre = (p.get("nombre") or "")[:60]
        caption = f"{_numero_lista(idx)} *{nombre}*\n💰 ${precio_fmt}"
        # El try/except va DENTRO del bucle: antes envolvía el bucle entero, así que
        # la primera imagen que fallara (URL rota, formato que WhatsApp rechaza,
        # rate limit) cancelaba en silencio TODAS las fotos restantes. El cliente
        # veía una lista de 3 productos y recibía una sola foto.
        try:
            await whatsapp_client.send_image(wa_id, p["imagen_url"], caption)
            log.info("Foto %d/%d '%s' enviada a %s", idx, len(candidatos), p.get("nombre"), wa_id)
            enviados.append(pid)
        except Exception:
            fallidos.append(pid)
            log.exception(
                "Error enviando foto de '%s' (id=%s url=%s) a %s — se continúa con las demás",
                p.get("nombre"), pid, p.get("imagen_url"), wa_id,
            )
        if len(enviados) < 5:
            await asyncio.sleep(0.6)
    log.info("Fotos a %s: %d enviadas, %d fallidas, de %d candidatos",
             wa_id, len(enviados), len(fallidos), len(candidatos))
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
    t = catalog._normalizar_texto(user_text).strip().strip(".,!?¿¡")
    if not t:
        return False
    if t in _RESPUESTAS_AFIRMATIVAS:
        return True
    for af in ("dame", "muestrame", "muéstrame", "mandame", "mándame", "enviame", "envíame"):
        if af in t:
            return True
    return False


# Definido en catalog.py para que la recuperación de candidatos también pueda
# reconocer un "ver más" (ahí decide si tiene sentido la búsqueda semántica).
_es_ver_mas = catalog._es_ver_mas


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

_RECHAZO_CROSS_SELLING_RE = re.compile(
    r"\b(solo\s+(las|los|el|la|eso|el\s+primero|la\s+1|el\s+1|lo\s+que\s+pedi|las\s+esposas|el\s+vibrador)|"
    r"no\s+gracias|asi\s+esta\s+bien|así\s+está\s+bien|sin\s+aceite|sin\s+perfume|sin\s+lubricante|"
    r"nada\s+mas|nada\s+más|ninguno\s+mas|ninguno\s+más|ningun\s+otro|ningún\s+otro|"
    r"proceder\s+al\s+pago|dame\s+los\s+datos|solo\s+eso)\b",
    re.IGNORECASE,
)

# Selección de producto(s) por número de una lista YA enviada (ej: "el 2 y 3",
# "dame el 1", "los numeros 2 y 4"). El cliente está eligiendo, no pidiendo ver
# fotos de nuevo — reenviar el catálogo en este turno es ruido que confunde al
# cliente justo cuando ya decidió qué comprar.
_SELECCION_NUMERICA_RE = re.compile(
    r"\b(?:el|los|las|la|numero|número)?\s*\d{1,2}\s*(?:(?:y|,|&|\+)\s*(?:el\s*)?\d{1,2}\s*)+\b|"
    r"^\s*(?:el|la|dame\s+el|quiero\s+el)?\s*(?:numero|número)?\s*\d{1,2}\s*[\.,!]?\s*$",
    re.IGNORECASE,
)


def _es_seleccion_de_lista_mostrada(user_text: str, history: list[dict]) -> bool:
    """El cliente elige por número ("el 2 y 3") de una lista que el bot ya
    envió con emojis de numeración (1️⃣, 2️⃣...) en el turno anterior. Requiere
    ver esa numeración reciente para no confundir cualquier número suelto
    (ej: "tengo 25 años", una dirección) con una selección de producto."""
    if not _SELECCION_NUMERICA_RE.search(user_text or ""):
        return False
    for m in reversed(history[-4:]):
        if m.get("role") == "assistant" and "️⃣" in (m.get("content") or ""):
            return True
    return False


# Frases que indican que el bot REALMENTE abrió el checkout (pidió datos de envío
# o dio medios de pago). Son frases completas a propósito: palabras sueltas como
# "pedido", "ciudad" o "total" aparecen en mensajes de exploración normales — la
# venta cruzada "¿te gustaría agregarlo a tu pedido?" activaba modo-venta para
# todo el resto de la conversación, bloqueando las fotos de consultas nuevas.
_BOT_ABRIO_CHECKOUT = (
    "nombre completo", "datos de envío", "datos de envio",
    "teléfono de contacto", "telefono de contacto",
    "dirección de envío", "direccion de envio",
    "información de pagos", "informacion de pagos",
    "confirmar tu pedido", "confirmo tu pedido", "resumen de tu pedido",
    "[[pedido",
)

# Respuestas cortas con las que el cliente cierra la venta cruzada ("solo eso",
# "no gracias", "listo"). Se evalúan como palabra completa: usar substrings hacía
# que "no" matcheara dentro de "u-no"/"bue-no" y "asi" dentro de "casi".
_CIERRE_CROSS_SELLING_RE = re.compile(
    r"\b(solo|no|así|asi|nada|listo|pago|ninguno|ninguna)\b",
    re.IGNORECASE,
)

_BOT_OFRECIO_CROSS_SELLING = (
    "aceite", "perfume", "lubricante", "complementar", "agregar a tu pedido",
    "experiencia más completa", "experiencia mas completa",
)


def _es_fase_venta(user_text: str, history: list[dict]) -> bool:
    """Detecta si el cliente está en fase de venta/pago (no de exploración).

    True si el mensaje actual habla de pago/datos/envío, O si el bot ofreció
    venta cruzada en su ÚLTIMO mensaje y el cliente responde cerrando sin
    adicionales.
    """
    texto = user_text or ""
    if _FASE_VENTA_RE.search(texto) or _RECHAZO_CROSS_SELLING_RE.search(texto):
        return True

    # Solo el ÚLTIMO mensaje del bot: la fase de venta la define el turno
    # inmediatamente anterior, no cualquier mensaje de los últimos seis (si no,
    # una venta cruzada vieja dejaba la conversación en modo-venta para siempre).
    ultimo_bot = next(
        (m for m in reversed(history) if m.get("role") == "assistant"), None)
    if not ultimo_bot:
        return False
    c = (ultimo_bot.get("content") or "").lower()
    if any(f in c for f in _BOT_ABRIO_CHECKOUT):
        return True
    if any(w in c for w in _BOT_OFRECIO_CROSS_SELLING):
        if _RECHAZO_CROSS_SELLING_RE.search(texto) or _CIERRE_CROSS_SELLING_RE.search(texto):
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
    clasif = await catalog.clasificar_intencion_cliente(
        user_text, history, (estado or {}).get("categoria_funcional"),
        (estado or {}).get("restricciones"))
    restricciones = clasif.get("restricciones") or {}

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
    # Una respuesta afirmativa ("si", "ok", "dale") NUNCA es cambio de tema: el
    # cliente está aceptando lo que se le ofreció. Sin esta guarda, un "si" mal
    # clasificado borraba el estado entero — incluida la lista de productos ya
    # mostrados — y el turno arrancaba de cero en otra categoría.
    if (estado_tiene_cat and nueva_cat_clara
            and clasif["categoria_funcional"] != estado.get("categoria_funcional")
            and not _es_respuesta_afirmativa(user_text)):
        log.info("Cambio de tema detectado: %s -> %s — reseteando estado",
                 estado.get("categoria_funcional"), clasif["categoria_funcional"])
        reset_state = True
        estado = None  # ignorar el estado viejo para la recuperación
        estado_tiene_cat = False

    # BÚSQUEDA POR NOMBRE EN PRIMER CONTACTO: si el cliente menciona un término
    # que NO es vocabulario conocido de categorías/subtipos (ej. "King Cock",
    # "Icicles", "Tenera" — marcas/modelos que no están en ninguna lista fija)
    # y no hay una conversación de categoría ya en curso, buscarlo por NOMBRE
    # antes de decidir si toca calificar. Sin esto, una marca que el
    # clasificador de categorías no reconoce podía terminar generando una
    # pregunta de calificación genérica en vez de mostrar directo el producto
    # que el cliente ya nombró explícitamente.
    producto_por_nombre: list[dict] = []
    if not clasif.get("es_especifico") and (not estado_tiene_cat or reset_state):
        tokens_extra = catalog._tokens_no_reconocidos(user_text)
        if tokens_extra:
            exclude_previo = estado.get("productos_mostrados", []) if estado else []
            producto_por_nombre = await catalog.buscar_producto_especifico(
                user_text, limit=5, exclude_ids=exclude_previo)
            if not producto_por_nombre:
                # El término puede venir con un typo ("multiorgarmo"). Corregirlo
                # contra los nombres reales del catálogo y reintentar: sin esto el
                # turno se queda sin candidatos, que es justo cuando el LLM puede
                # inventarse una lista de productos.
                texto_corregido = await catalog.corregir_typos_contra_catalogo(user_text)
                if texto_corregido != user_text:
                    producto_por_nombre = await catalog.buscar_producto_especifico(
                        texto_corregido, limit=5, exclude_ids=exclude_previo)
            if producto_por_nombre:
                clasif["es_especifico"] = True
                clasif["calificado"] = True
                log.info(
                    "Producto específico detectado por nombre (sin marca conocida) %r → %d resultados",
                    tokens_extra, len(producto_por_nombre),
                )

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
    en_fase_venta = _es_fase_venta(user_text, history)
    if debe_mostrar and en_fase_venta:
        debe_mostrar = False
        clasif["calificado"] = False
        log.info("Fase de venta detectada — fotos desactivadas este turno")

    # REGLA ANTI-REENVÍO EN SELECCIÓN: si el cliente elige productos por número
    # de una lista que el bot ya mostró (ej: "el 2 y 3"), NO reenviar fotos.
    # Antes, calificado=True persistido hacía que CUALQUIER mensaje sin categoría
    # nueva disparara mostrar_por_estado=True, y el sistema forzaba el reenvío de
    # los mismos candidatos cuando el LLM (correctamente) no emitía marcadores
    # [FOTO] en su respuesta de confirmación de venta.
    if debe_mostrar and _es_seleccion_de_lista_mostrada(user_text, history):
        debe_mostrar = False
        clasif["calificado"] = False
        log.info("Selección numérica de lista ya mostrada — fotos desactivadas este turno")

    candidatos: list[dict] = []
    exclude = estado.get("productos_mostrados", []) if estado else []
    # ¿Se llegó a consultar el inventario en este turno? Si debe_mostrar quedó en
    # False, ninguna búsqueda corre y `candidatos` queda vacío por no haber
    # buscado — NO por falta de stock. Distinguir ambos casos evita el handoff
    # falso "no tengo ese producto" (ver sin_stock_subtipo más abajo).
    busqueda_ejecutada = bool(debe_mostrar)

    # Si el cliente pidió un PRODUCTO ESPECÍFICO por marca/modelo (Lovense Lush,
    # Satisfyer Pro 2), buscar por NOMBRE primero (más preciso que categoría).
    # Así "Lovense Lush" muestra Lovense, no vibradores genéricos al azar.
    if clasif.get("es_especifico") and debe_mostrar:
        especificos = producto_por_nombre or await catalog.buscar_producto_especifico(
            user_text, limit=5, exclude_ids=exclude)
        if especificos:
            candidatos = especificos
            log.info("Producto específico encontrado por nombre: %d", len(candidatos))

    # ── RECUPERACIÓN POR RESTRICCIONES (camino principal) ──
    # Filtra en SQL sobre las facetas guardadas (tipo, zona, vibra, control…) en
    # vez de bajar el catálogo entero y clasificarlo en Python. Es lo que permite
    # expresar "vibrador anal" como intersección y no como un cajón.
    relajado = None
    if not candidatos and debe_mostrar and restricciones.get("tipo"):
        # En un "ver más" NO se relaja: el cliente pide más de LO MISMO. Si ya
        # se mostró todo lo que cumple, la respuesta es "no queda más", no
        # rellenar con productos de otra zona o tipo.
        res = await catalog.buscar_por_restricciones(
            restricciones, exclude_ids=exclude, limit=5,
            permitir_relajar=not _es_ver_mas(user_text))
        if res.productos:
            candidatos = res.productos
            relajado = res.relajado
            log.info("Restricciones %s → %d productos%s", restricciones,
                     len(candidatos), f" (relajando {relajado})" if relajado else "")

    if not candidatos and debe_mostrar and cat_func:
        # Camino anterior, aún activo para los productos que todavía no tienen
        # facetas calculadas (hasta que corra el backfill).
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

    # ¿Ofrecer "ver más"? Solo si quedan productos que CUMPLEN lo que pidió el
    # cliente y todavía no vio. Con 3 opciones en total no se pregunta "¿deseas
    # ver más diseños?" — se pregunta cuál quiere. Antes esto se calculaba con un
    # recuento por categoría legacy que ignoraba las restricciones, así que el
    # bot ofrecía más diseños de algo que ya había mostrado entero.
    try:
        if clasif.get("es_especifico"):
            # Pidió un producto concreto por nombre: se muestran esos y ya.
            total_en_categoria = len(candidatos)
            hay_mas = False
        elif restricciones.get("tipo") and not relajado:
            total_en_categoria = await catalog.contar_por_restricciones(restricciones)
            hay_mas = bool(candidatos) and total_en_categoria > (len(exclude) + len(candidatos))
        elif relajado:
            # Se cedió en algo para poder responder: no prometer más de lo mismo.
            total_en_categoria = len(candidatos)
            hay_mas = False
        else:
            total_en_categoria = await catalog.contar_productos(cat_func, genero) if cat_func else 0
            hay_mas = False
            if total_en_categoria and candidatos:
                hay_mas = total_en_categoria > (len(exclude) + len(candidatos))
    except Exception:
        total_en_categoria = len(candidatos) if candidatos else 0
        hay_mas = False
    sin_mas = not hay_mas if candidatos else False

    info = {
        "intencion": intencion,
        "categoria_funcional": cat_func,
        "genero": genero,
        "calificado": clasif["calificado"] or (debe_mostrar and bool(candidatos)),
        "pide_fotos": clasif["pide_fotos"],
        "es_especifico": bool(clasif.get("es_especifico")),
        "en_fase_venta": en_fase_venta,
        "reset_state": reset_state,
        "categoria_agotada": categoria_agotada,
        "sin_mas_opciones": sin_mas,
        "hay_mas": hay_mas,
        "total_en_categoria": total_en_categoria,
        # Solo es "sin stock" si de verdad se buscó y no había nada. Sin
        # busqueda_ejecutada, un turno que no mostraba fotos (fase de venta,
        # selección numérica) se leía como inventario vacío y disparaba el
        # handoff "no tengo ese producto" + bot pausado sin justificación.
        "sin_stock_subtipo": bool(busqueda_ejecutada and _subtipo_actual and not candidatos
                                  and not exclude and not _es_soft_actual),
        "debe_mostrar": debe_mostrar and bool(candidatos),
        "restricciones": restricciones,
        "relajado": relajado,
    }
    return candidatos, info


# Restricciones DURAS: si un producto no las cumple, no se envía. Son las que el
# cliente nombró explícitamente y definen de qué estamos hablando. Las demás
# (control, atributos, género) son preferencias: relajarlas devuelve algo
# parecido, relajar estas devuelve otra cosa.
_RESTRICCIONES_DURAS = ("tipo", "zona")


def _validar_envio(productos: list[dict], restricciones: dict,
                   relajado: str | None) -> list[dict]:
    """Descarta los productos que NO cumplen las restricciones duras.

    Es la última red antes de enviar. Aunque falle la clasificación, la búsqueda
    o el LLM, aquí no pasa un producto que contradiga lo que el cliente pidió.
    Se corrigió porque un cliente pidió "vibrador anal" y recibió enemas de
    limpieza: ningún paso comprobaba si lo que se iba a enviar cumplía la
    petición.

    Un campo relajado a propósito NO se valida — ya se decidió ceder ahí, y el
    mensaje al cliente lo dice.
    """
    if not productos or not restricciones:
        return productos
    validos, descartados = [], []
    for p in productos:
        ok = True
        for campo in _RESTRICCIONES_DURAS:
            esperado = restricciones.get(campo)
            # La ZONA no se exime nunca, ni siquiera si se relajó: haber cedido en
            # la forma del juguete no autoriza a cambiar la parte del cuerpo.
            if not esperado or (campo == relajado and campo != "zona"):
                continue
            actual = p.get(campo)
            if actual and actual != esperado:
                ok = False
                descartados.append(f"{p.get('nombre', '?')[:34]} ({campo}={actual})")
                break
        if ok:
            validos.append(p)
    if descartados:
        log.warning("Validación previa al envío: %d producto(s) descartados por no "
                    "cumplir %s → %s", len(descartados),
                    {k: v for k, v in restricciones.items() if k in _RESTRICCIONES_DURAS},
                    descartados)
    return validos


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

    pedido_creado_id = 0
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

    es_ver_mas_pedido = _es_ver_mas(user_text) and info.get("debe_mostrar") and candidatos
    es_agotado = info.get("categoria_agotada")
    # EL SISTEMA REDACTA LOS TURNOS DE PRODUCTO.
    # Nombres, precios, numeración, marcadores de foto y CTA salen todos de los
    # mismos candidatos, así que texto y fotos no pueden desalinearse y el CTA no
    # puede prometer "ver más" cuando no queda nada. El LLM sigue llevando el
    # resto de la conversación (asesoría, dudas, datos de envío, pago, tono).
    # Antes el LLM redactaba también estos turnos y el sistema iba detrás
    # corrigiéndolo —detectar si prometió productos, forzar fotos, reemplazar el
    # texto—, y esas correcciones se pisaban entre sí.
    texto_lo_arma_el_sistema = bool(info.get("debe_mostrar") and candidatos)
    # Numeración continua entre rondas: en un "ver más" los productos nuevos siguen
    # desde donde quedó la ronda anterior. Solo se aplica cuando el TEXTO lo redacta
    # el sistema; si lo escribe el LLM numera desde 1 por su cuenta y un offset en
    # las fotos las desalinearía del texto.
    offset_numeracion = 0
    if es_agotado:
        cat_nombre = info.get("intencion") or info.get("categoria_funcional") or "productos"
        cat_nombre = cat_nombre.replace("-", " ")
        raw_reply = f"Te mostré todas las opciones de {cat_nombre} disponibles 😊 ¿Cuál te gustaría llevar para continuar con tu pedido? 😊"
    elif texto_lo_arma_el_sistema:
        # La numeración continúa mientras siga la misma búsqueda; si el cliente
        # cambió de tema, el estado se reinició y arranca de nuevo en 1️⃣.
        offset_numeracion = 0 if info.get("reset_state") else len(ids_mostrados)
        raw_reply = _texto_desde_candidatos(candidatos, info,
                                            mas_disenos=bool(es_ver_mas_pedido),
                                            offset=offset_numeracion)
    else:
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
            "sin_mas_opciones": info.get("sin_mas_opciones", False),
            "hay_mas": info.get("hay_mas", False),
            "total_en_categoria": info.get("total_en_categoria", 0),
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
    if texto_lo_arma_el_sistema:
        # El texto salió de estos mismos candidatos: van sus fotos, sin más
        # comprobaciones. Aquí vivían las tres banderas que se pisaban entre sí
        # (forzar fotos, detectar si el LLM prometía productos, reemplazar su
        # texto); ya no hacen falta porque el LLM no redacta estos turnos.
        final_productos = candidatos[:5]
    else:
        # Turno de conversación: el LLM redacta. Si emitió marcadores de foto, se
        # validan contra los candidatos reales para descartar IDs inventados.
        final_productos = _resolver_candidatos_del_llm(foto_ids, candidatos)

    # RED DE SEGURIDAD — el LLM ofrece productos que NO se van a enviar. Se evalúa
    # sobre final_productos (marcadores ya validados contra candidatos reales), no
    # sobre foto_ids brutos, para cubrir también el [FOTO:999] alucinado. El filtro
    # de IDs impide mandar la foto, pero sin esta guardia el TEXTO igual salía.
    # Se detecta por las frases de plantilla ("Mira estas opciones…") y por una
    # lista numerada con precios. Dos salidas según haya categoría o no.
    if (not info["debe_mostrar"]
            and not final_productos
            and (_OFRECE_PRODUCTOS_RE.search(reply) or _LISTA_PRODUCTOS_RE.search(reply))):
        pregunta = _PREGUNTAS_CALIFICACION.get(info["categoria_funcional"]) if info["categoria_funcional"] else None
        if pregunta:
            log.info(
                "LLM omitió la pregunta de calificación para '%s' (escribió plantilla sin "
                "fotos válidas) — pregunta determinista inyectada",
                info["categoria_funcional"],
            )
            reply = pregunta
        elif not info.get("en_fase_venta"):
            # Sin categoría no hay pregunta de calificación que inyectar. Antes se
            # dejaba pasar el texto tal cual, y el LLM podía listar productos que no
            # existen (caso "multiorgarmo": 5 productos fabricados con precios).
            # Se excluye la fase de venta: ahí una lista numerada con precios es el
            # resumen legítimo del pedido, no una invención.
            log.warning(
                "LLM ofreció productos sin candidatos ni categoría — texto reemplazado "
                "por mensaje honesto (posible invención)",
            )
            reply = _SIN_RESULTADO_MSG

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

    # PUERTA DE VALIDACIÓN — última red antes de enviar. Ningún producto que
    # contradiga lo que el cliente pidió (tipo/zona) sale de aquí, aunque hayan
    # fallado la clasificación, la búsqueda o el LLM.
    antes_de_validar = len(final_productos)
    final_productos = _validar_envio(
        final_productos, info.get("restricciones") or {}, info.get("relajado"))
    if antes_de_validar and not final_productos:
        log.warning("Ningún producto pasó la validación para %s — no se envían fotos", wa_id)
        reply = _SIN_RESULTADO_MSG

    # Enviar fotos (candidatos ya validados + filtrados por el filtro final).
    enviados_ids = await _enviar_fotos_productos(wa_id, final_productos,
                                                 offset=offset_numeracion)

    if info["debe_mostrar"] and not enviados_ids and not pedido_creado_id:
        log.warning(
            "Bot prometio fotos a %s pero envio 0 (cat=%s gen=%s candidatos=%d) — se deja reply original para evitar doble mensaje",
            wa_id, info["categoria_funcional"], info["genero"], len(final_productos),
        )

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
            restricciones=info.get("restricciones") or None,
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

    updated_history = history + [
        {"role": "user", "content": user_text},
        {"role": "assistant", "content": reply},
    ]
    if _count_bot_refusals(updated_history) >= 3:
        await db.set_bot_paused(wa_id, True)
        log.info("Insistencia detectada para %s — bot pausado (sin 2do mensaje)", wa_id)
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
