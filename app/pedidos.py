"""Creación automática de pedidos al cerrar una venta.

El bot emite el marcador ``[[PEDIDO:CERRADO]]`` cuando confirma una venta. Este
módulo lo detecta, extrae los datos de envío del historial, resuelve los productos
contra el catálogo (con precios reales), calcula el total y crea el pedido en la DB.

Diseño:
  - NO se confía en el LLM para el total ni los IDs: el código resuelve productos
    del historial contra el catálogo y suma precios oficiales (regla de dinero).
  - Si no hay productos claros o el total da 0, igual se crea el pedido con total=0
    para que el equipo humano lo complete desde el panel (mejor tener el pedido con
    datos de envío que perderlo).
  - No se crea un pedido duplicado si ya hay uno pendiente/pagado reciente.
"""
from __future__ import annotations

import logging
import re

from app import config, db, catalog

log = logging.getLogger("pedidos")

# Marcador que el LLM emite al confirmar la venta (variantes tolerantes).
_PEDIDO_MARKER_RE = re.compile(r"\[\[PEDIDO:?\s*(CERRADO|CONFIRMADO|FINALIZADO)?\s*\]\]", re.IGNORECASE)

# Patrones para extraer datos de envío del historial reciente.
_NOMBRE_RE = re.compile(
    r"(?:me\s+llamo|mi\s+nombre\s+es|soy|yo\s+soy|nombre:?)\s+"
    r"([a-zA-ZÁÉÍÓÚÜÑáéíóúüñ]+(?:\s+[a-zA-ZÁÉÍÓÚÜÑáéíóúüñ]+){0,3})",
    re.IGNORECASE,
)
_CIUDAD_RE = re.compile(
    r"(?:ciudad:?\s*|soy\s+de\s+|estoy\s+en\s+|enviar?a?\s+(?:a|para)\s+|comuna\s+)"
    r"(bogot[aá]|medell[ií]n|cali|barranquilla|cartagena|bucaramanga|"
    r"santa marta|pereira|manizales|c[uú]cuta|ibagu[eé]|villavicencio|"
    r"soacha|subachoque|zipaquir[aá]|ch[ií]a|mosquera|funza|facatativ[aá]|"
    r"[a-záéíóúñ]{4,30})",
    re.IGNORECASE,
)
_DIRECCION_RE = re.compile(
    r"(?:direcci[oó]n:?\s*|dir\.?\s*|enviar?a?\s+(?:a|para)\s+(?:la\s+)?(?:calle|carrera|kr|cl|av|diag|transv|manzana|casa|apto|apartamento)\s+.+?)(?=[\n,;.¡!]|$)",
    re.IGNORECASE,
)
_TELEFONO_RE = re.compile(r"(?:tel[eé]fono:?\s*|cel(?:ular)?:?\s*|whatsapp:?\s*|n[uú]mero:?\s*)?(\+?\d[\d\s\-]{7,15}\d)")


def _joined_history(history: list[dict]) -> str:
    """Concatena el contenido del historial reciente (user + assistant)."""
    return "\n".join(m.get("content", "") for m in history[-12:])


def _extraer_nombre(history: list[dict]) -> str | None:
    joined = _joined_history(history)
    for m in (_NOMBRE_RE,):
        match = m.search(joined)
        if match:
            candidato = match.group(1).strip()
            # Filtrar palabras obviamente no-nombre
            invalidas = {"compra", "pedido", "pago", "envio", "producto", "asesor", "humano"}
            if candidato.lower() not in invalidas and 2 <= len(candidato) <= 60:
                return candidato.title()
    return None


def _extraer_ciudad(history: list[dict]) -> str | None:
    joined = _joined_history(history)
    match = _CIUDAD_RE.search(joined)
    return match.group(1).strip().title() if match else None


def _extraer_direccion(history: list[dict]) -> str | None:
    joined = _joined_history(history)
    match = _DIRECCION_RE.search(joined)
    if match:
        return match.group(0).strip()
    # Fallback: línea que contenga calle/carrera/#
    for line in joined.split("\n"):
        if re.search(r"\b(calle|carrera|kr\b|cl\b|diag|transv|#)\b", line, re.IGNORECASE):
            limpio = line.strip()
            if 5 <= len(limpio) <= 100:
                return limpio
    return None


def _extraer_telefono(history: list[dict], wa_id: str) -> str | None:
    joined = _joined_history(history)
    match = _TELEFONO_RE.search(joined)
    if match:
        tel = re.sub(r"[^\d+]", "", match.group(1))
        if len(tel.lstrip("+")) >= 7:
            return tel
    # Fallback: el wa_id es el teléfono de WhatsApp del cliente
    return wa_id


def _detectar_pedido_marker(reply: str) -> bool:
    """True si el reply contiene el marcador de cierre de venta."""
    return bool(_PEDIDO_MARKER_RE.search(reply))


def _limpiar_marker(reply: str) -> str:
    """Elimina el marcador de pedido del texto visible."""
    return _PEDIDO_MARKER_RE.sub("", reply).strip()


async def _resolver_productos_y_total(history: list[dict]) -> tuple[list[dict], int]:
    """Resuelve productos mencionados en el historial contra el catálogo.

    Devuelve (items, total). Cada item es {producto_id, nombre, cantidad, precio_unitario}.
    El total se calcula sumando precios oficiales del catálogo (no del LLM).
    """
    items: list[dict] = []
    seen_ids: set[int] = set()
    total = 0

    # Buscar productos en los mensajes del asistente (donde el bot recomienda)
    # y en los del usuario (donde elige).
    for msg in history[-12:]:
        contenido = msg.get("content", "")
        if not contenido:
            continue
        encontrados = await catalog.get_productos_en_texto(contenido, limit=5)
        for p in encontrados:
            pid = p["id"]
            if pid in seen_ids:
                continue
            seen_ids.add(pid)
            precio = int(p.get("precio", 0) or 0)
            items.append({
                "producto_id": pid,
                "nombre": p["nombre"],
                "cantidad": 1,
                "precio_unitario": precio,
            })
            total += precio
            if len(items) >= 8:  # tope razonable de items
                break
        if len(items) >= 8:
            break

    return items, total


async def maybe_create_pedido(
    wa_id: str, reply: str, history: list[dict]
) -> tuple[str, int]:
    """Si el reply marca cierre de venta, crea el pedido.

    Devuelve (reply_limpio, pedido_id). pedido_id=0 si no se creó nada.
    """
    if not _detectar_pedido_marker(reply):
        return reply, 0

    reply_limpio = _limpiar_marker(reply)

    # No duplicar: si ya hay un pedido pendiente/pagado reciente, no crear otro.
    existente = await db.get_pedido_pendiente(wa_id)
    if existente:
        log.info("Pedido ya existe para %s (id=%s, estado=%s) — no se duplica",
                 wa_id, existente["id"], existente["estado"])
        return reply_limpio, existente["id"]

    # Extraer datos de envío del historial.
    nombre = _extraer_nombre(history)
    ciudad = _extraer_ciudad(history)
    direccion = _extraer_direccion(history)
    telefono = _extraer_telefono(history, wa_id)

    # Resolver productos y total del catálogo.
    items, total = await _resolver_productos_y_total(history)

    # Crear el pedido (aunque falten datos o total=0 — el equipo lo completa).
    try:
        pedido_id = await db.create_pedido({
            "wa_id": wa_id,
            "nombre_cliente": nombre,
            "direccion_envio": direccion,
            "ciudad": ciudad,
            "telefono_contacto": telefono,
            "estado": "pendiente",
            "total": total,
            "creado_por": "bot",
            "notas": None,
        })
    except Exception:
        log.exception("Error creando pedido para %s", wa_id)
        return reply_limpio, 0

    # Crear los items.
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
            log.exception("Error añadiendo item al pedido %s", pedido_id)

    log.info(
        "Pedido #%d creado para %s | %s | %s | total=$%d | items=%d",
        pedido_id, wa_id, nombre or "(sin nombre)", ciudad or "(sin ciudad)",
        total, len(items),
    )
    return reply_limpio, pedido_id
