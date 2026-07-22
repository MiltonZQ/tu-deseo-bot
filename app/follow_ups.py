"""Follow-up suave: un único mensaje si el lead no responde en 10 minutos."""
import asyncio
import logging
from app import db, whatsapp_client, config

log = logging.getLogger("follow_ups")

FOLLOW_UP_MESSAGE = (
    "¡Hola! Solo quería saber si tienes alguna pregunta o si puedo ayudarte en algo más. 😊"
)


async def schedule(wa_id: str) -> None:
    """Programa un follow-up para este wa_id. Lo reprograma si ya había uno."""
    if not config.ENABLE_FOLLOW_UPS:
        return
    delay = int(getattr(config, "FOLLOW_UP_MINUTES", 10))
    await db.upsert_follow_up(wa_id, delay)


async def cancel(wa_id: str) -> None:
    """Cancela el follow-up cuando el usuario escribe antes de que se dispare."""
    await db.cancel_follow_up(wa_id)


async def _process_due() -> None:
    if not config.ENABLE_FOLLOW_UPS:
        cleared = await db.mark_all_follow_ups_sent()
        if cleared:
            log.info("Follow-ups pendientes desactivados: %s", cleared)
        return
    due = await db.get_due_follow_ups()
    for row in due:
        wa_id = row["wa_id"]
        try:
            await whatsapp_client.send_text(wa_id, FOLLOW_UP_MESSAGE)
            await db.mark_follow_up_sent(row["id"])
            log.info("Follow-up enviado a %s", wa_id)
        except Exception:
            log.exception("Error enviando follow-up a %s", wa_id)


async def run_loop() -> None:
    """Loop perpetuo. Se lanza como background task al arrancar la app."""
    while True:
        await asyncio.sleep(60)
        try:
            await _process_due()
        except Exception:
            log.exception("Error en follow_ups.run_loop")
