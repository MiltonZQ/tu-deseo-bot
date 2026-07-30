"""Validación de comprobantes de pago con GPT-4o vision.

Flujo porteado del workflow n8n de Lavadero/Belux y adaptado a e-commerce de Tu Deseo:
  1. El cliente envía una imagen (comprobante Nequi/Daviplata/Bancolombia).
  2. Se descarga la imagen (auth X-API-Key de yCloud).
  3. GPT-4o vision valida: es comprobante real, banco aceptado, monto coincide con el
     esperado del pedido, fecha de hoy, destinatario correcto.
  4. Extrae monto/referencia/banco/fecha de forma estructurada.
  5. Inserta en abonos con estado verificado_bot / no_valido.
  6. Lógica de máx N intentos fallidos → escalar a humano.

Diferencia clave vs Belux: el monto NO es fijo; se obtiene del pedido pendiente del cliente.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime
from typing import Any

import httpx
from openai import AsyncOpenAI

from app import config, db, whatsapp_client

log = logging.getLogger("payments")

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            api_key=config.OPENAI_API_KEY,
            base_url=config.OPENAI_BASE_URL,
        )
    return _client


def _destinatario_clause() -> str:
    """Construye la cláusula de cuentas destino válidas para el prompt."""
    partes = []
    if config.PAYMENT_ACCOUNT_BANCOLOMBIA:
        partes.append(f"cuenta ahorros {config.PAYMENT_ACCOUNT_BANCOLOMBIA} de Bancolombia a nombre de {config.PAYMENT_COMPANY_NAME} (NIT {config.PAYMENT_COMPANY_NIT})")
    if config.PAYMENT_BRE_B_HANDLE:
        partes.append(f"Llave/Bre-B/Transfiya {config.PAYMENT_BRE_B_HANDLE}")
    if config.PAYMENT_ACCOUNT_NEQUI:
        partes.append(f"Nequi {config.PAYMENT_ACCOUNT_NEQUI}")
    if config.PAYMENT_ACCOUNT_DAVIPLATA:
        partes.append(f"Daviplata {config.PAYMENT_ACCOUNT_DAVIPLATA}")
    if not partes:
        return "(no hay cuentas destino configuradas — omitir validación de destinatario)"
    return " O ".join(partes)


def _build_vision_prompt(monto_esperado: int | None) -> str:
    """Prompt para GPT-4o vision. Porteado del JSON de Lavadero, ampliado con extracción."""
    tz = config.bot_zoneinfo()
    # Formato NUMÉRICO (DD/MM/YYYY) e inmune al locale del contenedor. Antes se
    # usaba "%-d de %B de %Y" que en el contenedor (locale C/inglés) generaba
    # "30 de July de 2026" en inglés, y el modelo no lograba matchear la fecha de
    # comprobantes reales (que vienen en DD/MM/YYYY o español) → valido=false por
    # fecha aunque la fecha fuera correcta → escalada errónea a humano.
    now = datetime.now(tz)
    hoy_numerico = now.strftime("%d/%m/%Y")        # 30/07/2026
    hoy_iso = now.strftime("%Y-%m-%d")              # 2026-07-30
    monto_txt = f"${monto_esperado:,} COP" if monto_esperado else "el monto del pedido"

    return f"""Analiza este comprobante de pago colombiano. Responde SOLO con JSON sin texto
adicional ni backticks, con esta estructura exacta:
{{"valido": true|false, "razon": "motivo corto", "monto": <numero entero en COP sin puntos ni comas, ej: 29800>,
  "referencia": "<id de operación>", "banco": "<Nequi|Bancolombia|Daviplata|Bre-B|PSE|otro>",
  "fecha": "<YYYY-MM-DD o null>"}}

Criterios para valido=true (todos deben cumplirse):
1) Es un comprobante de pago real ({config.ACCEPTED_BANKS} u otro banco colombiano).
2) El monto consignado/transferido en la imagen coincide con {monto_txt} (ten en cuenta que en comprobantes colombianos 29.800 ó 29,800 pesos representa 29800 COP en total; no confundas separadores de miles con decimales).
3) La fecha del comprobante corresponde a HOY ({hoy_numerico} o {hoy_iso}). Acepta cualquier formato equivalente (DD/MM/YYYY, YYYY-MM-DD, "30 de julio", texto en español o numérico); lo importante es que sea la fecha de hoy, no el formato exacto.
4) El destinatario contiene {_destinatario_clause()}.

Solo marca valido=false si claramente ves que un dato es incorrecto o falta.
Los campos monto/referencia/banco/fecha deben extraerse siempre que sean legibles,
incluso cuando valido=false (para registrar el intento)."""


def _parse_vision_response(content: str) -> dict[str, Any]:
    """Extrae el JSON de la respuesta del modelo (tolera fences/backticks)."""
    text = (content or "").strip()
    # Quitar fences ```json ... ```
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        # Tomar el primer objeto { ... }
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.warning("Respuesta vision no es JSON válido: %r", content[:200])
        return {"valido": False, "razon": "No se pudo leer el comprobante"}

    def _int(v):
        if v is None:
            return None
        if isinstance(v, (int, float)):
            if isinstance(v, float) and 0 < v < 1000:
                return int(round(v * 1000))
            return int(v)
        s = str(v).strip().replace("$", "").replace(" ", "")
        if s.endswith(".00") or s.endswith(",00"):
            s = s[:-3]
        s = s.replace(".", "").replace(",", "")
        try:
            return int(s)
        except (ValueError, TypeError):
            return None

    monto = _int(data.get("monto"))
    return {
        "valido": bool(data.get("valido")),
        "razon": str(data.get("razon") or "")[:200],
        "monto": monto,
        "referencia": (str(data.get("referencia") or "").strip() or None),
        "banco": (str(data.get("banco") or "").strip() or None),
        "fecha": (str(data.get("fecha") or "").strip() or None),
    }


async def _download_image_as_b64(image_url: str) -> str | None:
    """Descarga la imagen con auth de yCloud y la devuelve en base64."""
    headers = {}
    if config.WHATSAPP_PROVIDER == "ycloud" and config.YCLOUD_API_KEY:
        headers["X-API-Key"] = config.YCLOUD_API_KEY
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(image_url, headers=headers)
        if r.status_code != 200:
            log.warning("Descarga imagen falló: HTTP %s", r.status_code)
            return None
        return base64.b64encode(r.content).decode("ascii")
    except Exception as exc:
        log.warning("Error descargando imagen: %s", exc)
        return None


async def _get_monto_esperado(wa_id: str) -> tuple[int | None, int | None]:
    """Obtiene (monto_esperado, pedido_id) del pedido pendiente activo de las últimas 24 horas.

    Devuelve (None, None) si no hay pedido asociado (el bot pedirá contexto).
    """
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        row = await conn.fetchrow(
            """
            SELECT id, total FROM pedidos
            WHERE wa_id = $1 AND estado = 'pendiente' AND created_at >= NOW() - INTERVAL '24 hours'
            ORDER BY created_at DESC LIMIT 1
            """,
            wa_id,
        )
    if not row:
        return None, None
    return int(row["total"] or 0), int(row["id"])


async def _analyze_comprobante(image_b64: str, monto_esperado: int | None) -> dict[str, Any]:
    """Llama a GPT-4o vision y devuelve el dict parseado."""
    prompt = _build_vision_prompt(monto_esperado)
    resp = await _get_client().chat.completions.create(
        model=config.OPENAI_VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ],
        }],
        temperature=0,
    )
    content = resp.choices[0].message.content or ""
    return _parse_vision_response(content)


async def handle_inbound_image(
    *,
    wa_id: str,
    image_url: str | None,
    caption: str | None,
    message_id: str,
    history: list[dict],
) -> bool:
    """Punto de entrada desde main.py.

    Devuelve True si la imagen fue tratada como comprobante (procesada, válida o no).
    Devuelve False si no hay contexto de pago (para que main.py la trate como media genérica).
    Heurística de contexto: hay un pedido pendiente, o el historial reciente habla de pago,
    o el caption menciona pago/comprobante/transferencia.
    """
    if not image_url:
        return False

    monto_esperado, pedido_id = await _get_monto_esperado(wa_id)
    recent = " ".join(m["content"] for m in history[-6:]).lower()
    caption_lc = (caption or "").lower()
    habla_pago = any(w in recent or w in caption_lc for w in (
        "pago", "comprobante", "transferencia", "consignacion", "consignación",
        "abono", "nequi", "daviplata", "bancolombia", "envio", "envío",
    ))

    if not pedido_id and not habla_pago:
        return False  # sin contexto de pago → main.py la maneja como media genérica

    # 1) Descargar
    image_b64 = await _download_image_as_b64(image_url)
    if not image_b64:
        await whatsapp_client.send_text(
            wa_id,
            "No pude descargar la imagen del comprobante 😔 Por favor envíala de nuevo.",
        )
        return True

    # 2) Analizar
    try:
        result = await _analyze_comprobante(image_b64, monto_esperado)
    except Exception as exc:
        log.exception("Error analizando comprobante para %s: %s", wa_id, exc)
        await whatsapp_client.send_text(
            wa_id,
            "Tuvo un problema procesando tu comprobante. Un asesor lo revisará. 🙌",
        )
        await db.insert_abono({
            "telefono": wa_id, "pedido_id": pedido_id,
            "url_comprobante": image_url, "estado": "pendiente",
            "verificado_por": "bot", "intento_n": 1,
        })
        return True

    valido = result["valido"]
    intento_n = await db.count_abonos_fallidos(wa_id, config.PAYMENT_ATTEMPTS_WINDOW_HOURS) + 1

    # 3) Registrar abono
    abono_id = await db.insert_abono({
        "telefono": wa_id,
        "pedido_id": pedido_id,
        "monto_esperado": monto_esperado,
        "monto_declarado": result.get("monto"),
        "monto": result.get("monto"),
        "estado": "verificado_bot" if valido else "no_valido",
        "banco": result.get("banco"),
        "referencia": result.get("referencia"),
        "fecha_comprobante": result.get("fecha"),
        "url_comprobante": image_url,
        "verificado_por": "bot",
        "intento_n": intento_n,
    })

    # 4) Respuesta al cliente + lógica de escalamiento
    if valido:
        if pedido_id:
            await db.update_pedido_estado(pedido_id, "pagado")
        else:
            # Si no había un pedido pendiente previo, crear el pedido automáticamente en estado 'pagado'
            try:
                from app import pedidos
                nombre = pedidos._extraer_nombre(history)
                ciudad = pedidos._extraer_ciudad(history)
                direccion = pedidos._extraer_direccion(history)
                telefono = pedidos._extraer_telefono(history, wa_id)
                items, _ = await pedidos._resolver_productos_y_total(history)
                monto_pagado = result.get("monto") or 0

                pedido_id = await db.create_pedido({
                    "wa_id": wa_id,
                    "nombre_cliente": nombre,
                    "direccion_envio": direccion,
                    "ciudad": ciudad,
                    "telefono_contacto": telefono,
                    "estado": "pagado",
                    "total": monto_pagado,
                    "creado_por": "bot",
                    "notas": f"Pago verificado automáticamente por bot (Banco: {result.get('banco') or 'S/N'}, Ref: {result.get('referencia') or 'S/N'})",
                })
                if abono_id:
                    await db.update_abono_pedido_id(abono_id, pedido_id)
                for item in items:
                    try:
                        await db.add_pedido_item(
                            pedido_id=pedido_id,
                            producto_id=item["producto_id"],
                            nombre_snapshot=item["nombre"],
                            cantidad=item["cantidad"],
                            precio_unitario=item["precio_unitario"],
                        )
                    except Exception:
                        pass
            except Exception:
                log.exception("Error creando pedido automático tras pago verificado de %s", wa_id)

        await whatsapp_client.send_text(
            wa_id,
            "✅ ¡Pago verificado! Gracias. Nuestro equipo confirmará el despacho en breve. 🎉",
        )
        log.info("Comprobante VÁLIDO para %s (pedido %s)", wa_id, pedido_id)
        return True

    # inválido → ¿agotó intentos?
    if intento_n >= config.PAYMENT_MAX_ATTEMPTS:
        await db.set_bot_paused(wa_id, True, motivo="Comprobante inválido tras varios intentos")
        await db.create_escalation({
            "wa_id": wa_id,
            "issue_summary": f"Comprobante inválido (intento {intento_n}): {result['razon']}",
            "reason": "pago_invalido",
            "reason_detail": result["razon"],
        })
        await whatsapp_client.send_text(
            wa_id,
            "No fue posible verificar tu comprobante después de varios intentos. "
            "Un asesor se comunicará contigo en breve para ayudarte. 🙌",
        )
        log.info("Comprobante ESCALADO para %s (intento %d)", wa_id, intento_n)
    else:
        await whatsapp_client.send_text(
            wa_id,
            f"No pudimos verificar tu comprobante 😔\n\n*Motivo:* {result['razon']}\n\n"
            "Por favor envíalo nuevamente, asegurándote de que la captura se vea clara "
            "y corresponda al pago de tu pedido.",
        )
        log.info("Comprobante inválido para %s (intento %d): %s",
                 wa_id, intento_n, result["razon"])
    return True
