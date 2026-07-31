"""Cliente Redis para locks distribuidos y buffer de mensajes ráfaga.

Si REDIS_URL no está configurado o Redis no está disponible, cae de forma transparente
en el almacenamiento en memoria existente.
"""
import logging
from typing import Optional

from app import config

log = logging.getLogger("redis_client")

_redis_client = None


async def init_redis():
    """Inicializa la conexión con Redis si REDIS_URL está presente."""
    global _redis_client
    if not config.REDIS_URL:
        log.info("REDIS_URL no configurada — usando fallback en memoria para locks y buffer")
        return
    try:
        import redis.asyncio as aioredis
        _redis_client = aioredis.from_url(
            config.REDIS_URL,
            decode_responses=True,
            socket_timeout=3.0,
            socket_connect_timeout=3.0,
        )
        # Probar ping
        await _redis_client.ping()
        log.info("Conexión con Redis establecida exitosamente (%s)", config.REDIS_URL)
    except Exception as exc:
        log.warning("No se pudo conectar a Redis (%s) — se usará fallback en memoria", exc)
        _redis_client = None


async def close_redis():
    """Cierra la conexión con Redis si estaba abierta."""
    global _redis_client
    if _redis_client:
        try:
            await _redis_client.aclose()
            log.info("Conexión con Redis cerrada")
        except Exception as exc:
            log.warning("Error al cerrar conexión con Redis: %s", exc)
        _redis_client = None


def is_redis_available() -> bool:
    """True si Redis está activo y listo."""
    return _redis_client is not None


async def acquire_user_lock(wa_id: str, ttl: Optional[int] = None) -> bool:
    """Intenta adquirir un lock distribuido para wa_id usando SETNX + TTL.

    Devuelve True si el lock fue adquirido, False de lo contrario.
    Si Redis no está disponible, devuelve False (para usar lock en memoria).
    """
    if not _redis_client:
        return False
    lock_key = f"wa_lock:{wa_id}"
    ttl_seconds = ttl or config.REDIS_LOCK_TTL_SECONDS
    try:
        acquired = await _redis_client.set(lock_key, "1", nx=True, ex=ttl_seconds)
        return bool(acquired)
    except Exception as exc:
        log.warning("Error al adquirir lock en Redis para %s: %s", wa_id, exc)
        return False


async def release_user_lock(wa_id: str) -> None:
    """Libera el lock en Redis para wa_id."""
    if not _redis_client:
        return
    lock_key = f"wa_lock:{wa_id}"
    try:
        await _redis_client.delete(lock_key)
    except Exception as exc:
        log.warning("Error al liberar lock en Redis para %s: %s", wa_id, exc)


async def push_user_message(wa_id: str, text: str) -> Optional[int]:
    """Añade un mensaje al buffer de ráfaga en Redis.

    Devuelve la cantidad de mensajes en el buffer tras el push, o None si falla.
    """
    if not _redis_client:
        return None
    buffer_key = f"wa_buffer:{wa_id}"
    try:
        async with _redis_client.pipeline(transaction=True) as pipe:
            pipe.rpush(buffer_key, text)
            pipe.expire(buffer_key, 60)  # TTL de 60 segundos por seguridad
            results = await pipe.execute()
        return results[0]
    except Exception as exc:
        log.warning("Error agregando mensaje al buffer Redis para %s: %s", wa_id, exc)
        return None


async def pop_all_user_messages(wa_id: str) -> list[str]:
    """Extrae atómicamente todos los mensajes acumulados en el buffer de Redis."""
    if not _redis_client:
        return []
    buffer_key = f"wa_buffer:{wa_id}"
    try:
        async with _redis_client.pipeline(transaction=True) as pipe:
            pipe.lrange(buffer_key, 0, -1)
            pipe.delete(buffer_key)
            results = await pipe.execute()
        messages = results[0] or []
        return list(messages)
    except Exception as exc:
        log.warning("Error al vaciar buffer Redis para %s: %s", wa_id, exc)
        return []
