"""Cliente OpenAI con control de tokens."""
import io
import logging
from datetime import datetime

import httpx
import tiktoken
from openai import AsyncOpenAI
from app import config

log = logging.getLogger("openai_client")

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=config.OPENAI_API_KEY,
            base_url=config.OPENAI_BASE_URL,
        )
    return _client


def _encoder():
    """Siempre usar cl100k_base: funciona para gpt-4o/4o-mini y es un proxy razonable
    para modelos nuevos. El control de tokens no necesita ser exacto, solo acotado."""
    try:
        return tiktoken.encoding_for_model(config.OPENAI_MODEL)
    except KeyError:
        return tiktoken.get_encoding("cl100k_base")


def count_tokens(messages: list[dict]) -> int:
    enc = _encoder()
    total = 0
    for m in messages:
        total += len(enc.encode(m.get("content", ""))) + 4  # overhead por mensaje
    return total + 2


def fit_history(system: str, history: list[dict], user_msg: str,
                max_tokens: int, summary: str | None = None) -> list[dict]:
    """Dropea los mensajes más viejos hasta caber en max_tokens.

    El presupuesto se mide sobre el paquete completo que se envía al modelo:
    system prompt + (resumen) + historial + mensaje actual. El orden de prioridad
    para descartar es siempre el mensaje MÁS VIEJO del historial, de modo que
    system prompt, resumen consolidado y últimos turnos se preservan.
    """
    summary_block = f"\n\n## Memoria previa\n{summary}" if summary else ""
    kept = list(history)
    while True:
        msgs = (
            [{"role": "system", "content": system + summary_block}]
            + kept
            + [{"role": "user", "content": user_msg}]
        )
        if count_tokens(msgs) <= max_tokens or not kept:
            return kept
        kept.pop(0)


def _model_kwargs(messages: list[dict]) -> dict:
    """Construye los kwargs para chat.completions.create.

    Gestiona modelos de razonamiento (thinking) vía OpenRouter:
    - Gemini 3.x Flash y modelos thinking generan razonamiento interno por defecto,
      lo que hace lenta la respuesta y puede devolver content vacío si max_tokens
      es bajo. El pipeline determinístico YA hace el "razonamiento" en Python
      (clasificar, recuperar, validar), así que aquí solo necesitamos la redacción
      final → excluimos el razonamiento con `reasoning: {exclude: true}`.
    - MODEL_REASONING_EFFORT (legacy GLM-5.x) sigue soportándose.
    """
    kwargs: dict = {"model": config.OPENAI_MODEL, "messages": messages}
    extra_body: dict = {}
    if config.MODEL_REASONING_EXCLUDE:
        # No devolver el chain-of-thought; solo la respuesta final.
        extra_body["reasoning"] = {"exclude": True}
    if config.MODEL_REASONING_EFFORT:
        extra_body.setdefault("reasoning", {})["effort"] = config.MODEL_REASONING_EFFORT
    if extra_body:
        kwargs["extra_body"] = extra_body
    if config.MAX_REPLY_TOKENS:
        kwargs["max_tokens"] = config.MAX_REPLY_TOKENS
    return kwargs


async def complete(user_message: str, history: list[dict],
                   lead: dict | None = None,
                   summary: str | None = None,
                   candidatos: list[dict] | None = None,
                   estado: dict | None = None,
                   debe_mostrar_fotos: bool = False) -> str:
    """Redacta la respuesta del bot.

    Pipeline determinístico: el SISTEMA ya recuperó los `candidatos` correctos de
    la DB (filtrados por categoría + género) y los pasa al modelo para que los
    muestre con [FOTO:ID]. El modelo ya NO recibe el catálogo completo; solo los
    candidatos confirmados de este turno, lo que elimina las alucinaciones de IDs.

    Args:
      candidatos: productos confirmados a mostrar (lista de dicts de la DB). Si es
        vacío, el bot hará su ÚNICA pregunta de calificación.
      estado: estado de conversación persistido (categoria, genero, calificado).
      debe_mostrar_fotos: si True, el sistema ya decidió mostrar productos; el
        modelo NO debe preguntar, solo redactar con los candidatos.
    """
    now = datetime.now(config.bot_zoneinfo())

    # Contexto dinámico: operativo (fecha/negocio) + cliente (perfil conocido).
    context_lines = [
        f"- Fecha y hora actual: {now.strftime('%A %d/%m/%Y %H:%M')} ({config.BOT_TIMEZONE})",
        f"- Negocio: {config.BUSINESS_NAME}",
    ]
    if lead:
        cliente_bits = []
        if lead.get("nombre"):
            cliente_bits.append(f"Nombre: {lead['nombre']}")
        if lead.get("negocio"):
            cliente_bits.append(f"Negocio: {lead['negocio']}")
        estado_lead = lead.get("qualification_status")
        if estado_lead and estado_lead != "en_progreso":
            cliente_bits.append(f"Estado del lead: {estado_lead}")
        if cliente_bits:
            context_lines.append("- Cliente: " + "; ".join(cliente_bits))

    # Bloque de memoria consolidada (resumen de conversaciones anteriores).
    summary_block = f"\n\n## Memoria previa de este cliente\n{summary}\n" if summary else ""

    # Bloque de estado de conversación (qué busca el cliente, qué se le mostró).
    estado_block = ""
    if estado:
        estado_lines = []
        if estado.get("categoria_busqueda"):
            estado_lines.append(f"- Buscando: {estado['categoria_busqueda']}")
        if estado.get("genero"):
            estado_lines.append(f"- Género/uso: {estado['genero']}")
        if estado.get("calificado"):
            estado_lines.append("- Ya fue calificado (NO vuelvas a preguntar la categoría)")
        if estado.get("sin_mas_opciones"):
            estado_lines.append(
                "- ⚠️ TODAS LAS OPCIONES DISPONIBLES MOSTRADAS: Con estas fotos ya le enviaste TODAS las "
                "opciones/diseños disponibles en inventario para este producto/subtipo. PROHIBIDO preguntar "
                "'¿deseas ver más diseños?' o '¿quieres ver más opciones?' (no hay más). En su lugar, pregunta "
                "si le gustó alguno de estos diseños o si prefiere explorar otra categoría (ej. lubricantes, lencería) o información de envío."
            )
        elif estado.get("categoria_agotada"):
            estado_lines.append(
                "- ⚠️ CATEGORÍA/SUBTIPO AGOTADO: Ya se le enviaron TODAS las opciones disponibles de esta búsqueda "
                "en mensajes anteriores. PROHIBIDO ofrecer 'ver más diseños' ni prometer más fotos. Explícale "
                "amablemente que esas eran todas las opciones en inventario y pregunta si desea alguna o ver otra categoría."
            )
        if estado.get("productos_mostrados"):
            estado_lines.append(
                f"- Productos ya mostrados (IDs): {', '.join(str(i) for i in estado['productos_mostrados'][-10:])}"
            )
        if estado.get("productos_con_precios"):
            estado_lines.append(
                "- Productos mostrados CON PRECIOS EXACTOS del catálogo (USA ESTOS precios "
                "en el resumen de confirmación del pedido, NO los inventes ni los confundas):\n"
                + estado["productos_con_precios"]
            )
        if estado_lines:
            estado_block = "\n\n## Estado de la conversación\n" + "\n".join(estado_lines) + "\n"

    # Bloque de candidatos confirmados (pipeline). Reemplaza al catálogo gigante.
    candidatos_block = ""
    if candidatos:
        c_lines = [
            "## Productos confirmados para mostrar AHORA (recuperados del inventario real)",
            "Muestra ESTOS productos usando sus [FOTO:ID] exactos. NO inventes otros IDs,",
            "NO uses productos que no estén en esta lista. Si no encajan, di que los",
            "verificarás con el equipo en vez de ofrecer productos distintos:",
        ]
        for p in candidatos:
            desc = (p.get("descripcion") or "")[:90]
            gen = p.get("_genero", "")
            c_lines.append(
                f"- **{p['nombre']}** — ${p['precio']:,} — {desc}"
                + (f" (uso: {gen})" if gen else "")
                + f"  #{p['id']}"
            )
        candidatos_block = "\n\n" + "\n".join(c_lines) + "\n"

    system_prompt = (
        f"{config.SYSTEM_PROMPT}\n\n"
        f"{summary_block}"
        f"{estado_block}"
        f"{candidatos_block}"
        "## Contexto operativo\n"
        + "\n".join(context_lines)
    )

    fitted = fit_history(
        system_prompt, history, user_message, config.MAX_PROMPT_TOKENS,
        summary=summary,
    )
    messages = (
        [{"role": "system", "content": system_prompt}]
        + fitted
        + [{"role": "user", "content": user_message}]
    )
    if len(fitted) < len(history):
        log.info(
            "Historial recortado por tokens: %d -> %d mensajes",
            len(history), len(fitted),
        )
BOT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "busqueda_semantica",
            "description": "Busca productos en el catálogo por descripción semántica, necesidad o características (ej. 'control por app', 'vibrador para parejas', 'lubricante con calor').",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Término o frase de búsqueda semántica expresada por el cliente."
                    },
                    "categoria_funcional": {
                        "type": "string",
                        "description": "Categoría opcional (vibradores, succionadores, dildos, anal, masturbadores, anillos-y-fundas, pareja-y-bondage, lubricantes-y-cuidado, lenceria, juegos-y-accesorios)."
                    },
                    "genero": {
                        "type": "string",
                        "description": "Género opcional: hombre, mujer, pareja, anal."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "stock_tiempo_real",
            "description": "Consulta la disponibilidad e inventario en tiempo real de un producto por su ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "producto_id": {
                        "type": "integer",
                        "description": "ID numérico del producto en la base de datos."
                    }
                },
                "required": ["producto_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cross_selling",
            "description": "Obtiene sugerencias de productos complementarios según la categoría consultada.",
            "parameters": {
                "type": "object",
                "properties": {
                    "categoria": {
                        "type": "string",
                        "description": "Categoría principal de interés del cliente."
                    }
                },
                "required": ["categoria"]
            }
        }
    }
]


async def execute_tool_call(tool_name: str, args: dict) -> dict:
    """Ejecuta determinísticamente la función invocada por el modelo."""
    import json
    from app import catalog, db
    if tool_name == "busqueda_semantica":
        query = args.get("query", "")
        cat = args.get("categoria_funcional")
        gen = args.get("genero")
        prods = await catalog.get_productos_para_recomendar(

            categoria_funcional=cat,
            genero=gen,
            user_text=query,
            limit=5,
        )
        formatted_prods = []
        for p in prods:

            formatted_prods.append({
                "id": p["id"],
                "nombre": p.get("nombre"),
                "precio": p.get("precio"),
                "marcador_foto": f"[FOTO:{p['id']}]",
                "descripcion": (p.get("descripcion") or "")[:200],
            })
        return {
            "instruccion": "Para enviar las fotos por WhatsApp, DEBES incluir el marcador [FOTO:ID] exacto para cada producto en tu mensaje.",
            "productos": formatted_prods,
        }


    elif tool_name == "stock_tiempo_real":
        pid = args.get("producto_id")
        if not pid:
            return {"error": "ID no provisto"}
        async with db._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, nombre, precio, stock_status, activo FROM productos WHERE id = $1",
                int(pid)
            )
        if not row:
            return {"disponible": False, "motivo": "Producto no encontrado en catálogo"}
        return {
            "id": row["id"],
            "nombre": row["nombre"],
            "precio": row["precio"],
            "disponible": bool(row["activo"]) and row["stock_status"] in ("instock", None),
            "stock_status": row["stock_status"],
        }

    elif tool_name == "cross_selling":
        cat = (args.get("categoria") or "").lower()
        target_cat = "lubricantes-y-cuidado"
        if "lenceria" in cat:
            target_cat = "pareja-y-bondage"
        prods = await catalog.get_productos_para_recomendar(
            categoria_funcional=target_cat,
            genero=None,
            limit=3,
        )
        return {"sugeridos": prods}

    return {"error": f"Herramienta desconocida: {tool_name}"}


async def _resolve_model_response(messages: list[dict], model_kwargs: dict) -> str:
    """Llama al modelo y procesa llamadas a herramientas si el modelo decide ejecutarlas."""
    import json
    kwargs = dict(model_kwargs)
    kwargs["tools"] = BOT_TOOLS
    kwargs["tool_choice"] = "auto"
    
    resp = await _get_client().chat.completions.create(**kwargs)
    choice = resp.choices[0]
    message = choice.message

    if message.tool_calls:
        tool_calls = message.tool_calls
        messages.append(message.model_dump())
        for tool_call in tool_calls:
            fname = tool_call.function.name
            try:
                fargs = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError:
                fargs = {}
            log.info("Modelo invocó herramienta %s con args: %r", fname, fargs)
            result = await execute_tool_call(fname, fargs)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(result, ensure_ascii=False),
            })
        # Segunda llamada con la respuesta de la herramienta
        kwargs2 = dict(model_kwargs)
        kwargs2["messages"] = messages
        resp2 = await _get_client().chat.completions.create(**kwargs2)
        return resp2.choices[0].message.content or ""

    return message.content or ""


    # Llamada al modelo con fallback automático: si el modelo principal falla
    # (ej. gpt-5.2 deprecado/apagado por OpenAI), reintenta con el modelo fallback
    # para que el bot nunca se quede sin responder.
    try:
        kwargs = _model_kwargs(messages)
        return await _resolve_model_response(messages, kwargs)
    except Exception as exc:
        if config.OPENAI_MODEL_FALLBACK and config.OPENAI_MODEL_FALLBACK != config.OPENAI_MODEL:
            log.warning("Modelo principal %s falló (%s); reintentando con fallback %s",
                        config.OPENAI_MODEL, exc, config.OPENAI_MODEL_FALLBACK)
            fallback_kwargs = dict(_model_kwargs(messages))
            fallback_kwargs["model"] = config.OPENAI_MODEL_FALLBACK
            fallback_kwargs.pop("extra_body", None)
            return await _resolve_model_response(messages, fallback_kwargs)
        raise



async def transcribe_audio(audio_url: str, mime_type: str | None = None) -> str | None:
    """Descarga el audio de la URL y lo transcribe con Whisper. Devuelve None si falla."""
    try:
        headers = {}
        if config.WHATSAPP_PROVIDER == "ycloud" and config.YCLOUD_API_KEY:
            headers["X-API-Key"] = config.YCLOUD_API_KEY
        elif config.WHATSAPP_API_TOKEN:
            headers["Authorization"] = f"Bearer {config.WHATSAPP_API_TOKEN}"

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(audio_url, headers=headers)

        if resp.status_code not in (200, 201) and headers:
            # Intento de respaldo sin headers en caso de ser una URL pública de CDN firmada
            async with httpx.AsyncClient(timeout=30) as client:
                resp_no_auth = await client.get(audio_url)
            if resp_no_auth.status_code in (200, 201):
                resp = resp_no_auth

        if resp.status_code not in (200, 201):
            log.warning("Audio download failed: HTTP %s for %s", resp.status_code, audio_url)
            return None
        ext = "ogg"
        if mime_type:
            if "mp4" in mime_type or "m4a" in mime_type:
                ext = "m4a"
            elif "mp3" in mime_type or "mpeg" in mime_type:
                ext = "mp3"
            elif "wav" in mime_type:
                ext = "wav"
            elif "webm" in mime_type:
                ext = "webm"
        audio_file = io.BytesIO(resp.content)
        audio_file.name = f"audio.{ext}"
        transcript = await _get_client().audio.transcriptions.create(
            model=config.WHISPER_MODEL,
            file=audio_file,
            language="es",
        )
        text = (transcript.text or "").strip()
        log.info("Audio transcrito (%d chars)", len(text))
        return text or None
    except Exception as exc:
        log.warning("Error transcribiendo audio: %s", exc)
        return None


async def summarize_conversation(history: list[dict], lead: dict | None = None) -> str:
    transcript = "\n".join(
        f"{'Cliente' if item.get('role') == 'user' else 'Bot'}: {item.get('content', '')}"
        for item in history[-24:]
    )
    lead_context = ""
    if lead:
        lead_context = (
            f"Nombre: {lead.get('nombre') or '-'}\n"
            f"Negocio: {lead.get('negocio') or '-'}\n"
        )
    messages = [
        {
            "role": "system",
            "content": (
                "Resume la conversacion comercial en espanol neutro. "
                "Entrega un resumen breve y accionable en 5 lineas maximo."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Datos del lead:\n{lead_context}\n"
                f"Conversacion:\n{transcript}"
            ),
        },
    ]
    resp = await _get_client().chat.completions.create(**_model_kwargs(messages))
    return resp.choices[0].message.content or "Sin resumen disponible."


# ── Clasificador de intención por LLM (respaldo del determinístico) ──────────
#
# Cuando el clasificador determinístico (listas de palabras) no reconoce la
# categoría del mensaje del cliente (ej: "kits de sadomasoquismo", jerga nueva,
# variantes no listadas), esta función hace una llamada BARATA al LLM para
# clasificar la intención en una de las 11 categorías funcionales cerradas.
#
# Seguridad: el LLM SOLO puede devolver una de las 11 categorías o "ninguna".
# No inventa categorías ni productos — la recuperación sigue siendo determinística.
# Cache en memoria: mismo mensaje → misma respuesta (evita reclasificar).

# Las 11 categorías funcionales con descripciones para guiar al LLM.
_CATEGORIAS_LLM = {
    "vibradores": "vibradores de clítoris, punto G, rabbit, tipo Hitachi, balas, control remoto, app",
    "succionadores": "succionadores de clítoris (Satisfyer, Womanizer), air pulse",
    "dildos": "dildos, consoladores, realistas, con ventosa, de vidrio, dobles",
    "anal": "plugs anales, bolas anales, estimulación de próstata, dilatadores, arneses/strap-on",
    "masturbadores": "masturbadores masculinos, huevos, vaginas artificiales, torsos",
    "anillos-y-fundas": "anillos para pene (vibradores o no), fundas/extensores, bombas de vacío",
    "pareja-y-bondage": "bondage, BDSM, kits de amarre, esposas, antifaz, fustas, látigos, velos, sadomasoquismo, juegos de pareja",
    "lubricantes-y-cuidado": "lubricantes (base agua/silicona/sabores), estimulantes, retardantes, limpiadores de juguetes, aceites, cremas íntimas",
    "lenceria": "lencería (body, baby doll, disfraz), suspensorios, pecheras, conjuntos masculinos, arneses de lencería",
    "juegos-y-accesorios": "juegos de mesa eróticos, dados, cartas, accesorios varios",
}

_CLASIFICADOR_PROMPT = (
    "Eres un clasificador de intención para un sex shop por WhatsApp. "
    "Dado el mensaje del cliente, responde SOLO con un JSON compacto indicando qué busca.\n\n"
    "Categorías posibles (usa EXACTAMENTE la clave, minúsculas):\n"
    + "\n".join(f"- {k}: {v}" for k, v in _CATEGORIAS_LLM.items())
    + "\n\nGéneros posibles: hombre, mujer, pareja, anal, o null si no se aclara.\n"
    "Si el mensaje NO busca un producto del catálogo (saludo, pregunta de envío, "
    "pago, queja, producto que no vendemos), devuelve categoria \"ninguna\".\n\n"
    "Responde SOLO el JSON, sin texto extra. Formato:\n"
    '{"categoria": "<clave o ninguna>", "genero": "<o null>"}'
)

# Cache simple en memoria: evita reclasificar el mismo mensaje en reintentos.
_cache_clasif: dict[str, dict] = {}
_CACHE_MAX = 200


async def clasificar_intencion_llm(user_message: str) -> dict | None:
    """Clasifica la intención del mensaje del cliente con una llamada LLM barata.

    Devuelve {"categoria": <una de las 11 o "ninguna">, "genero": <o None>} o
    None si el LLM falla/timed out. Es el RESPALDO del clasificador determinístico:
    solo se llama cuando las listas de palabras no reconocen la categoría.

    Restricción: el LLM solo puede elegir una de las 11 categorías — no inventa.
    """
    if not user_message or not user_message.strip():
        return None
    key = user_message.strip().lower()
    if key in _cache_clasif:
        return _cache_clasif[key]

    messages = [
        {"role": "system", "content": _CLASIFICADOR_PROMPT},
        {"role": "user", "content": user_message},
    ]
    try:
        # Llamada barata con timeout corto: si tarda, fallback al comportamiento actual.
        resp = await _get_client().chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=messages,
            max_tokens=60,   # JSON compacto, no necesita más
            temperature=0.0,  # determinístico
            timeout=4.0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        # Extraer el JSON (el LLM a veces añade markdown ```json).
        import json as _json
        import re as _re
        m = _re.search(r"\{[^{}]*\}", raw)
        if not m:
            return None
        data = _json.loads(m.group(0))
        cat = str(data.get("categoria", "")).strip().lower()
        gen = data.get("genero")
        if gen:
            gen = str(gen).strip().lower()
            if gen not in ("hombre", "mujer", "pareja", "anal"):
                gen = None
        # Validar que la categoría sea una de las 11 o "ninguna".
        if cat != "ninguna" and cat not in _CATEGORIAS_LLM:
            log.warning("LLM clasificó categoría inválida %r — descartada", cat)
            return None
        result = {"categoria": cat, "genero": gen}
        # Guardar en cache.
        if len(_cache_clasif) >= _CACHE_MAX:
            _cache_clasif.pop(next(iter(_cache_clasif)))
        _cache_clasif[key] = result
        log.info("LLM clasificó %r -> %s", user_message[:40], result)
        return result
    except Exception as exc:
        log.warning("Clasificador LLM falló (%s) — fallback a determinístico", type(exc).__name__)
        return None
