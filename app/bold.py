"""Integración con la pasarela de pagos Bold.

Se habilita cuando el cliente entregue los permisos API de Bold y se configure
BOLD_INTEGRATION_ENABLED=true. Mientras tanto, las funciones son no-ops seguros.

Referencia de la API (a confirmar con los asesores de Bold durante la Semana 2):
  - Crear enlace de pago (checkout link) para un pedido.
  - Webhook de confirmación de pago (POST /webhook/bold).
"""
from __future__ import annotations

import logging

import httpx

from app import config, db

log = logging.getLogger("bold")


def is_enabled() -> bool:
    return bool(config.BOLD_INTEGRATION_ENABLED and config.BOLD_API_KEY)


async def create_payment_link(*, pedido_id: int, amount: int, description: str) -> str | None:
    """Crea un enlace de pago en Bold para un pedido.

    Devuelve la URL de checkout o None si la integración no está lista/falla.
    La forma exacta del endpoint y payload depende de la documentación de Bold que
    entregue el cliente; este es el esqueleto a completar.
    """
    if not is_enabled():
        log.info("Bold no habilitado — no se crea link para pedido %s", pedido_id)
        return None
    url = f"{config.BOLD_API_BASE_URL.rstrip('/')}/payment-link"
    headers = {"Authorization": f"Bearer {config.BOLD_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "amount": amount,
        "currency": "COP",
        "description": description,
        "reference": str(pedido_id),
    }
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
        link = data.get("checkout_url") or data.get("url") or data.get("payment_url")
        log.info("Link Bold creado para pedido %s", pedido_id)
        return link
    except Exception as exc:
        log.warning("Error creando link Bold para pedido %s: %s", pedido_id, exc)
        return None


async def process_webhook(payload: dict) -> dict:
    """Procesa el webhook de confirmación de Bold.

    Marca el pedido como pagado y crea un abono verificado_bot.
    Endpoints a registrar en Bold: POST /webhook/bold (se añade a main.py en su momento).
    """
    if not is_enabled():
        return {"skipped": "bold no habilitado"}
    try:
        referencia = payload.get("reference") or payload.get("transaction_id")
        pedido_id = int(referencia) if referencia else None
        amount = int(payload.get("amount", 0))
        estado = (payload.get("status") or "").lower()
        if estado not in ("approved", "success", "paid", "ok"):
            return {"ignored": f"estado={estado}"}
        if pedido_id:
            await db.update_pedido_estado(pedido_id, "pagado")
            await db.insert_abono({
                "telefono": None,
                "pedido_id": pedido_id,
                "monto": amount,
                "monto_esperado": amount,
                "monto_declarado": amount,
                "estado": "verificado_bot",
                "metodo": "bold",
                "verificado_por": "bold",
                "intento_n": 1,
            })
        return {"ok": True, "pedido_id": pedido_id}
    except Exception as exc:
        log.exception("Error procesando webhook Bold: %s", exc)
        return {"error": str(exc)}
