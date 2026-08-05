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
    try:
        return tiktoken.encoding_for_model(config.OPENAI_MODEL)
    except KeyError:
        return tiktoken.get_encoding("cl100k_base")


def count_tokens(messages: list[dict]) -> int:
    enc = _encoder()
    total = 0
    for m in messages:
        total += len(enc.encode(m.get("content", ""))) + 4
    return total + 2


def fit_history(system: str, history: list[dict], user_msg: str,
                max_tokens: int, summary: str | None = None) -> list[dict]:
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
    kwargs: dict = {"model": config.OPENAI_MODEL, "messages": messages}
    extra_body: dict = {}
    if config.MODEL_REASONING_EXCLUDE:
        extra_body["reasoning"] = {"exclude": True}
    if config.MODEL_REASONING_EFFORT:
        extra_body.setdefault("reasoning", {})["effort"] = config.MODEL_REASONING_EFFORT
    if extra_body:
        kwargs["extra_body"] = extra_body
    if config.MAX_REPLY_TOKENS:
        kwargs["max_tokens"] = config.MAX_REPLY_TOKENS
    return kwargs


BOT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "stock_tiempo_real",
            "description": "Consulta disponibilidad de un producto por su ID en la DB local (no toca la web).",
            "parameters": {
                "type": "object",
                "properties": {
                    "producto_id": {"type": "integer", "description": "ID numerico del producto"}
                },
                "required": ["producto_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cross_selling",
            "description": "Obtiene sugerencias de productos complementarios segun categoria.",
            "parameters": {
                "type": "object",
                "properties": {
                    "categoria": {"type": "string", "description": "Categoria principal"}
                },
                "required": ["categoria"]
            }
        }
    }
]


async def execute_tool_call(tool_name: str, args: dict) -> dict:
    from app import catalog, db
    if tool_name == "stock_tiempo_real":
        pid = args.get("producto_id")
        if not pid:
            return {"error": "ID no provisto"}
        async with db._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id, nombre, precio, stock_status, activo FROM productos WHERE id = $1",
                int(pid)
            )
        if not row:
            return {"disponible": False, "motivo": "Producto no encontrado"}
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
            categoria_funcional=target_cat, genero=None, limit=3
        )
        return {"sugeridos": [{"id": p["id"], "nombre": p["nombre"], "precio": p["precio"]} for p in prods]}
    return {"error": f"Herramienta desconocida: {tool_name}"}


async def _resolve_model_response(messages: list[dict], model_kwargs: dict) -> str:
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
            except Exception:
                fargs = {}
            log.info("Modelo invoco herramienta %s %r", fname, fargs)
            result = await execute_tool_call(fname, fargs)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(result, ensure_ascii=False),
            })
        kwargs2 = dict(model_kwargs)
        kwargs2["messages"] = messages
        resp2 = await _get_client().chat.completions.create(**kwargs2)
        return resp2.choices[0].message.content or ""
    return message.content or ""


async def complete(user_message: str, history: list[dict],
                   lead: dict | None = None,
                   summary: str | None = None,
                   candidatos: list[dict] | None = None,
                   estado: dict | None = None,
                   debe_mostrar_fotos: bool = False,
                   producto_activo: dict | None = None) -> str:
    now = datetime.now(config.bot_zoneinfo())
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

    summary_block = f"\n\n## Memoria previa de este cliente\n{summary}\n" if summary else ""
    estado_block = ""
    if estado:
        estado_lines = []
        if estado.get("categoria_busqueda"):
            estado_lines.append(f"- Buscando: {estado['categoria_busqueda']}")
        if estado.get("genero"):
            estado_lines.append(f"- Género/uso: {estado['genero']}")
        if estado.get("calificado"):
            estado_lines.append("- Ya fue calificado (NO vuelvas a preguntar la categoría)")
        if estado.get("hay_mas"):
            total = estado.get("total_en_categoria", 0)
            estado_lines.append(
                f"- ✅ HAY MAS PRODUCTOS: total {total} en categoria, quedan por mostrar. "
                f"Usa CTA 'Por favor, indícame el número o los números de los productos "
                f"que deseas adquirir, o si deseas ver más diseños 😊'"
            )
        if estado.get("sin_mas_opciones"):
            estado_lines.append(
                "- ⚠️ TODAS LAS OPCIONES DISPONIBLES MOSTRADAS: Con estas fotos ya le enviaste TODAS las "
                "opciones/diseños disponibles. PROHIBIDO preguntar '¿deseas ver más?'."
            )
        elif estado.get("categoria_agotada"):
            estado_lines.append(
                "- ⚠️ CATEGORÍA/SUBTIPO AGOTADO: Ya se le enviaron TODAS las opciones disponibles. PROHIBIDO ofrecer 'ver más'."
            )
        if estado.get("productos_mostrados"):
            estado_lines.append(
                f"- Productos ya mostrados (IDs): {', '.join(str(i) for i in estado['productos_mostrados'][-10:])}"
            )
        if estado.get("productos_con_precios"):
            estado_lines.append(
                "- Productos mostrados, NUMERADOS como los vio el cliente:\n"
                + estado["productos_con_precios"] + "\n"
                "- ⚠️ REGLA ABSOLUTA DE SELECCIÓN NUMÉRICA: Si el cliente escribe un número (ej: '1', '2', 'el 1'), "
                "corresponde ÚNICA Y EXCLUSIVAMENTE a la línea que tiene ESE MISMO número (1️⃣, 2️⃣...). "
                "Está ESTRICTAMENTE PROHIBIDO reinterpretar el número o cambiarlo por comentarios o colores "
                "mencionados en mensajes anteriores (ej: si dijo 'negras' antes pero ahora escribe '1', DEBES seleccionar "
                "e indicar el producto 1️⃣, NO el 4️⃣). Usa los nombres y precios EXACTOS de esa línea."
            )
        if estado_lines:
            estado_block = "\n\n## Estado de la conversación\n" + "\n".join(estado_lines) + "\n"

    candidatos_block = ""
    if candidatos:
        c_lines = [
            "## Productos confirmados para mostrar AHORA (recuperados del inventario real)",
            "Muestra ESTOS productos usando sus [FOTO:ID] exactos. NO inventes otros IDs:",
        ]
        for idx, p in enumerate(candidatos[:5], 1):
            desc = (p.get("descripcion") or "")[:70]
            gen = p.get("_genero", "") or p.get("genero", "")
            c_lines.append(
                f"{idx}. **{p['nombre']}** — ${p['precio']:,} — {desc}" +
                (f" (uso: {gen})" if gen else "") +
                f"  ID:{p['id']} -> usa [FOTO:{p['id']}]"
            )
        candidatos_block = "\n\n" + "\n".join(c_lines) + "\n"

    # Ficha del producto activo: cuando el cliente pregunta sobre lo que acaba
    # de ver ("¿tiene sabor?", "¿frío y calor?", "¿contraindicaciones?"), el LLM
    # necesita la ficha REAL del producto para no responder con sentido común.
    # Sin esto, contestaba "sí, tiene sabor" a un lubricante que no lo tiene.
    # El producto activo es el ÚLTIMO mostrado; lo resuelve main.py por ID.
    ficha_block = ""
    if producto_activo:
        f_lines = [
            "## Producto que el cliente está viendo AHORA",
            "El cliente pregunta sobre ESTE producto que ya le mostraste. "
            "Responde sus dudas (sabor, material, modo de uso, contraindicaciones, "
            "para quién es, etc.) usando SU ficha real, no conocimiento genérico:",
            f"- **{producto_activo.get('nombre', '')}** — ${int(producto_activo.get('precio', 0)):,}".replace(",", "."),
        ]
        desc = (producto_activo.get("descripcion") or "").strip()
        if desc:
            f_lines.append(f"- Descripción: {desc}")
        attrs = producto_activo.get("atributos") or []
        if attrs:
            f_lines.append(f"- Atributos: {', '.join(attrs)}")
        tipo = producto_activo.get("tipo")
        if tipo:
            f_lines.append(f"- Tipo: {tipo}")
        zona = producto_activo.get("zona")
        if zona:
            f_lines.append(f"- Zona: {zona}")
        if producto_activo.get("vibra"):
            f_lines.append("- Vibra: sí")
        f_lines.append(f"- ID: {producto_activo.get('id')}")
        ficha_block = "\n\n" + "\n".join(f_lines) + "\n"

    system_prompt = (
        f"{config.SYSTEM_PROMPT}\n\n"
        f"{summary_block}"
        f"{estado_block}"
        f"{candidatos_block}"
        f"{ficha_block}"
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
        log.info("Historial recortado por tokens: %d -> %d", len(history), len(fitted))

    try:
        kwargs = _model_kwargs(messages)
        return await _resolve_model_response(messages, kwargs)
    except Exception as exc:
        if config.OPENAI_MODEL_FALLBACK and config.OPENAI_MODEL_FALLBACK != config.OPENAI_MODEL:
            log.warning("Modelo principal %s fallo (%s); reintentando fallback %s",
                        config.OPENAI_MODEL, exc, config.OPENAI_MODEL_FALLBACK)
            fallback_kwargs = dict(_model_kwargs(messages))
            fallback_kwargs["model"] = config.OPENAI_MODEL_FALLBACK
            fallback_kwargs.pop("extra_body", None)
            return await _resolve_model_response(messages, fallback_kwargs)
        raise


async def transcribe_audio(audio_url: str, mime_type: str | None = None) -> str | None:
    try:
        headers = {}
        if config.WHATSAPP_PROVIDER == "ycloud" and config.YCLOUD_API_KEY:
            headers["X-API-Key"] = config.YCLOUD_API_KEY
        elif config.WHATSAPP_API_TOKEN:
            headers["Authorization"] = f"Bearer {config.WHATSAPP_API_TOKEN}"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(audio_url, headers=headers)
        if resp.status_code not in (200, 201) and headers:
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
            "content": "Resume la conversacion comercial en espanol neutro. Entrega un resumen breve y accionable en 5 lineas maximo.",
        },
        {
            "role": "user",
            "content": f"Datos del lead:\n{lead_context}\nConversacion:\n{transcript}",
        },
    ]
    resp = await _get_client().chat.completions.create(**_model_kwargs(messages))
    return resp.choices[0].message.content or "Sin resumen disponible."


_CATEGORIAS_LLM = {
    "vibradores": "vibradores de clítoris, punto G, rabbit, tipo Hitachi, balas, control remoto, app",
    "succionadores": "succionadores de clítoris (Satisfyer, Womanizer), air pulse",
    "dildos": "dildos, consoladores, realistas, con ventosa, de vidrio, dobles",
    "anal": "plugs anales, bolas anales, estimulación de próstata, dilatadores, arneses/strap-on",
    "masturbadores": "masturbadores masculinos, huevos, vaginas artificiales, torsos",
    # `anillos-y-fundas`, `fundas-pene` y `bombas-pene` son categorías distintas
    # del vocabulario canónico y el catálogo las distingue (tipos anillo, funda y
    # bomba). Antes esta única clave las describía a las tres, así que el LLM
    # mandaba a fundas y bombas al cajón de los anillos y el filtro por tipo las
    # dejaba fuera.
    "anillos-y-fundas": "anillos para el pene, vibradores o no",
    "fundas-pene": "fundas, extensores y prótesis que se ponen sobre el pene",
    "bombas-pene": "bombas de vacío para el pene, manuales o automáticas",
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
    "Hay dos claves más, y la diferencia entre ellas importa:\n"
    "- \"ninguna\": el mensaje NO busca un producto. Saludo, despedida, pregunta "
    "de envío o de horario, pago o comprobante, queja, o un producto que no "
    "vendemos.\n"
    "- \"indefinida\": el cliente SÍ busca un producto pero no dice de cuál "
    "categoría, así que no se puede elegir ninguna con seguridad. Ejemplos: "
    "\"quiero un juguete para mi novia\", \"¿qué me recomiendas?\", \"algo "
    "discreto\", \"quiero probar algo nuevo\", \"¿qué tienen para mujer?\". Con "
    "esta clave el bot le preguntará qué busca, que es lo correcto: adivinar una "
    "categoría le mostraría productos que no pidió.\n"
    "\"indefinida\" es el último recurso: si el mensaje da ALGUNA pista del tipo "
    "de producto —para pareja, para jugar los dos, de cuero, que vibre, para el "
    "ano— elige la categoría que corresponda. Úsala solo cuando no haya ninguna "
    "pista. Y no uses \"ninguna\" para un cliente que quiere comprar algo aunque "
    "no sepa qué.\n\n"
    "Subtipo (opcional): si el cliente nombra una variante concreta, ponla en "
    "\"subtipo\". Claves válidas (usa EXACTAMENTE una, minúsculas, o null):\n"
    "colegiala, coneja, conejita, diabla, enfermera, mucama, playboy, policia, "
    "sailor moon, disfraz, realista, ventosa, vidrio, cristal, doble, textura, "
    "piel, rabbit, punto g, hitachi, bala, huevo vibr, prostat, cola, ducha, "
    "enema, base de agua, silicona, sabores, sabor, desensibilizante, calor, frio, "
    "funda, arnes, liguero, pechera, encaje, body, suspensorio, conjunto, "
    "esposas, antifaz, fusta, latigo, amarre, mordaza, vendas, con app, "
    "control remoto, recargable, inalambrico, sencillo, primera vez.\n\n"
    "Atributo (opcional): si el cliente describe una característica funcional "
    "(no un nombre), ponla en \"atributo\". Claves válidas (una, minúsculas, o null):\n"
    "realista, ventosa, vidrio, sabor, agua, silicona, hibrido, desensibilizante, "
    "calor, frio, principiante, recargable, impermeable.\n"
    "IMPORTANTE — claves que valen para los DOS campos (realista, ventosa, vidrio, "
    "sabor, silicona, calor, frio, recargable, desensibilizante): rellena AMBOS con "
    "la misma clave, nunca elijas uno solo. No es redundante: el subtipo busca la "
    "palabra en el nombre y el atributo filtra por la ficha del producto.\n"
    "Ejemplos: \"para demorar\" → subtipo desensibilizante + atributo desensibilizante; "
    "\"con ventosa\" → subtipo ventosa + atributo ventosa; \"disfraz de policia\" → "
    "subtipo policia, atributo null (policia no es un atributo).\n\n"
    "Responde SOLO el JSON, sin texto extra. Formato:\n"
    '{"categoria": "<clave o ninguna>", "genero": "<o null>", '
    '"subtipo": "<o null>", "atributo": "<o null>"}'
)

# Vocabulario cerrado para validar la salida del LLM. Si devuelve algo que no
# está aquí, se descarta (null) — así nunca pide un atributo/subtipo que el
# catálogo no sabe filtrar, lo que daría 0 resultados falsos y un escalado
# sin motivo. Las claves son las que `_SUBTIPO_KEYWORDS` y `_ATRIBUTOS` ya
# reconocen en catalog.py / facetas.py.
_SUBTIPOS_LLM_VALIDOS = frozenset({
    "colegiala", "coneja", "conejita", "diabla", "enfermera", "mucama",
    "playboy", "policia", "sailor moon", "disfraz", "realista", "ventosa",
    "vidrio", "cristal", "doble", "textura", "piel", "rabbit", "punto g",
    "hitachi", "bala", "huevo vibr", "prostat", "cola", "ducha", "enema",
    "base de agua", "silicona", "sabores", "sabor", "desensibilizante", "calor",
    "frio", "funda", "arnes", "liguero", "pechera", "encaje", "body",
    "suspensorio", "conjunto", "esposas", "antifaz", "fusta", "latigo",
    "amarre", "mordaza", "vendas", "con app", "control remoto", "recargable",
    "inalambrico", "sencillo", "primera vez",
})
_ATRIBUTOS_LLM_VALIDOS = frozenset({
    "realista", "ventosa", "vidrio", "sabor", "agua", "silicona", "hibrido",
    "desensibilizante", "calor", "frio", "principiante", "recargable",
    "impermeable",
})

_cache_clasif: dict[str, dict] = {}
_CACHE_MAX = 200


async def clasificar_intencion_llm(user_message: str) -> dict | None:
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
        resp = await _get_client().chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=messages,
            max_tokens=60,
            temperature=0.0,
            timeout=4.0,
        )
        raw = (resp.choices[0].message.content or "").strip()
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
        # "indefinida" es una respuesta VÁLIDA, no un fallo: el cliente busca
        # producto pero no dijo de qué categoría. Distinguirla de "ninguna" es lo
        # que permite preguntarle en vez de adivinar; antes ambas caían en
        # "ninguna" y el bot acababa buscando por nombre suelto ("quiero un
        # juguete para mi novia" → Limpiador De Juguetes).
        if cat not in _CATEGORIAS_LLM and cat not in ("ninguna", "indefinida"):
            log.warning("LLM clasificó categoría inválida %r — descartada", cat)
            return None
        # subtipo y atributo: vocabulario cerrado. Si el LLM devuelve algo que no
        # está en el conjunto, se descarta (null) en vez de propagarlo: un
        # atributo que el catálogo no sabe filtrar daría 0 resultados falsos y un
        # escalado a asesor sin motivo. La intersección (ventosa es subtipo y
        # atributo a la vez) es esperada: el subtipo guía el filtro por nombre, el
        # atributo guía el filtro SQL.
        sub = data.get("subtipo")
        if sub:
            sub = str(sub).strip().lower()
            if sub not in _SUBTIPOS_LLM_VALIDOS:
                log.info("LLM devolvió subtipo no reconocido %r — descartado", sub)
                sub = None
        else:
            sub = None
        atrib = data.get("atributo")
        if atrib:
            atrib = str(atrib).strip().lower()
            if atrib not in _ATRIBUTOS_LLM_VALIDOS:
                log.info("LLM devolvió atributo no reconocido %r — descartado", atrib)
                atrib = None
        else:
            atrib = None
        result = {"categoria": cat, "genero": gen, "subtipo": sub, "atributo": atrib}
        if len(_cache_clasif) >= _CACHE_MAX:
            _cache_clasif.pop(next(iter(_cache_clasif)))
        _cache_clasif[key] = result
        log.info("LLM clasificó %r -> %s", user_message[:40], result)
        return result
    except Exception as exc:
        log.warning("Clasificador LLM falló (%s) — fallback a determinístico", type(exc).__name__)
        return None


_REORDENAR_PROMPT = (
    "Eres un asistente que ordena productos de un sex shop por relevancia. "
    "Dado lo que el cliente escribió y una lista de productos candidatos "
    "(id: nombre), devuelve SOLO un JSON con los ids ordenados del más al "
    "menos relevante para lo que el cliente pidió literalmente. Usa "
    "EXCLUSIVAMENTE ids de la lista dada — nunca inventes ids nuevos. Si "
    "ninguno es claramente más relevante que otro, devuelve los ids en el "
    "mismo orden en que se te dieron.\n\n"
    'Formato: {"ids": [id1, id2, ...]}'
)


async def reordenar_candidatos_por_relevancia(
    user_text: str, candidatos: list[dict]
) -> list[int] | None:
    """Reordena candidatos YA filtrados por categoría/género, según qué tan
    bien coinciden con lo que el cliente escribió literalmente.

    No reemplaza el filtro determinístico de catalog.py (categoría y género
    ya se validaron antes de llegar aquí) — es un desempate más inteligente
    que "por longitud de nombre" cuando varios candidatos empatan en el score
    de palabras clave (ej. "colegiala" entre 9 disfraces, ninguno de los
    cuales tiene por qué estar en una lista de sinónimos mantenida a mano).

    Nunca puede devolver un id que no estaba en `candidatos`: cualquier id
    fuera de ese conjunto se descarta antes de usar el resultado. Si falla,
    tarda más de 3s, o no devuelve nada utilizable, devuelve None y el
    llamador debe conservar el orden original.
    """
    if not user_text or len(candidatos) < 2:
        return None
    ids_validos = {c["id"] for c in candidatos}
    lista = "\n".join(f'{c["id"]}: {c["nombre"]}' for c in candidatos[:20])
    messages = [
        {"role": "system", "content": _REORDENAR_PROMPT},
        {"role": "user", "content": f"Cliente pidió: {user_text}\n\nCandidatos:\n{lista}"},
    ]
    try:
        resp = await _get_client().chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=messages,
            max_tokens=200,
            temperature=0.0,
            timeout=3.0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        import json as _json
        import re as _re
        m = _re.search(r"\{.*\}", raw, _re.DOTALL)
        if not m:
            return None
        data = _json.loads(m.group(0))
        ids = [int(i) for i in data.get("ids", [])]
        ids_filtrados = [i for i in ids if i in ids_validos]
        if not ids_filtrados:
            return None
        # Completar con los candidatos que el LLM no mencionó, en su orden original.
        vistos = set(ids_filtrados)
        faltantes = [c["id"] for c in candidatos if c["id"] not in vistos]
        return ids_filtrados + faltantes
    except Exception:
        log.warning("Reordenar candidatos por LLM falló — se mantiene orden determinístico")
        return None


_SELECCION_PROMPT = (
    "Eres un asistente de un sex shop. El cliente acaba de ver una lista de "
    "productos y ahora escribe algo. Tu única tarea es decidir a CUÁL de esos "
    "productos se refiere.\n\n"
    "Devuelve SOLO un JSON con los ids de los productos que el cliente está "
    "eligiendo. Usa EXCLUSIVAMENTE ids de la lista dada — nunca inventes ids.\n\n"
    "Si el cliente NO está eligiendo un producto concreto (pregunta algo "
    "general, saluda, pide ver más opciones, o menciona algo que no coincide "
    'con ninguno), devuelve {"ids": []}. Es preferible no elegir a elegir mal: '
    "el cliente no se entera de que decidiste por él.\n\n"
    "El cliente puede referirse a un producto por su nombre, por una parte del "
    "nombre, por su color o material, o por para qué sirve.\n\n"
    'Formato: {"ids": [id1, id2, ...]}'
)


async def seleccionar_producto_llm(
    user_text: str, productos: list[dict]
) -> list[int] | None:
    """A cuál de los productos mostrados se refiere el cliente.

    Último recurso de la cascada de `app/seleccion.py`, para lo que las reglas no
    alcanzan: la referencia por atributos que no están en el nombre ("las esposas
    negras peludas"). Seguir ampliando listas de palabras a mano no escala.

    Igual que `reordenar_candidatos_por_relevancia`, es una elección de MUNDO
    CERRADO: se le dan los productos con su id y cualquier id fuera de ese
    conjunto se descarta al volver. La imposibilidad de inventar un producto es
    estructural — está en el filtro, no en la confianza en el prompt.

    Devuelve None si no resuelve nada (respuesta vacía, ids inválidos, fallo o
    timeout); el llamador debe preguntarle al cliente en vez de adivinar.
    """
    if not user_text or not productos:
        return None
    ids_validos = {p["id"] for p in productos}
    lista = "\n".join(
        f'{p["id"]}: {p["nombre"]} — ${int(p.get("precio") or 0):,}'.replace(",", ".")
        for p in productos[:10]
    )
    messages = [
        {"role": "system", "content": _SELECCION_PROMPT},
        {"role": "user",
         "content": f"Productos mostrados:\n{lista}\n\nEl cliente escribió: {user_text}"},
    ]
    try:
        resp = await _get_client().chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=messages,
            max_tokens=60,
            temperature=0.0,
            timeout=4.0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        import json as _json
        import re as _re
        m = _re.search(r"\{.*\}", raw, _re.DOTALL)
        if not m:
            return None
        ids = [int(i) for i in _json.loads(m.group(0)).get("ids", [])]
        elegidos = [i for i in ids if i in ids_validos]
        if len(elegidos) != len(ids):
            log.warning("El LLM devolvió ids fuera de los mostrados (%s) — descartados",
                        [i for i in ids if i not in ids_validos])
        if not elegidos:
            return None
        log.info("LLM resolvió la selección %r -> %s", user_text[:40], elegidos)
        return elegidos
    except Exception as exc:
        log.warning("Selección por LLM falló (%s) — se pedirá el nombre al cliente",
                    type(exc).__name__)
        return None
