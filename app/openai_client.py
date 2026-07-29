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
        if estado.get("productos_mostrados"):
            estado_lines.append(
                f"- Productos ya mostrados (IDs): {', '.join(str(i) for i in estado['productos_mostrados'][-10:])}"
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
    # Llamada al modelo con fallback automático: si el modelo principal falla
    # (ej. gpt-5.2 deprecado/apagado por OpenAI), reintenta con el modelo fallback
    # para que el bot nunca se quede sin responder.
    try:
        resp = await _get_client().chat.completions.create(**_model_kwargs(messages))
        return resp.choices[0].message.content or ""
    except Exception as exc:
        if config.OPENAI_MODEL_FALLBACK and config.OPENAI_MODEL_FALLBACK != config.OPENAI_MODEL:
            log.warning("Modelo principal %s falló (%s); reintentando con fallback %s",
                        config.OPENAI_MODEL, exc, config.OPENAI_MODEL_FALLBACK)
            fallback_kwargs = dict(_model_kwargs(messages))
            fallback_kwargs["model"] = config.OPENAI_MODEL_FALLBACK
            # El fallback (gpt-4.1-mini) no es thinking; quitar reasoning exclude.
            fallback_kwargs.pop("extra_body", None)
            resp = await _get_client().chat.completions.create(**fallback_kwargs)
            return resp.choices[0].message.content or ""
        raise


async def transcribe_audio(audio_url: str, mime_type: str | None = None) -> str | None:
    """Descarga el audio de la URL y lo transcribe con Whisper. Devuelve None si falla."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(audio_url)
        if resp.status_code != 200:
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
