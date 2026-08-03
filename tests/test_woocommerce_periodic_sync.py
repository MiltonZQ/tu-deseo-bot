"""No existia ningun mecanismo periodico de resincronizacion con WooCommerce:
solo sync una vez al arrancar (si WOOCOMMERCE_AUTO_SYNC=true, apagado por
defecto) y webhook por producto. Si el webhook no llega, un producto nuevo
queda invisible para el bot indefinidamente — reportado el 2026-08-02 con una
crema creada el dia anterior que el bot no conocia.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

for _m in ("asyncpg", "httpx", "openai", "qdrant_client", "redis", "redis.asyncio",
           "tiktoken", "PIL", "PIL.Image"):
    _mod = types.ModuleType(_m)
    _mod.__getattr__ = lambda _n: type("_Any", (), {"__init__": lambda *a, **k: None})  # type: ignore[attr-defined]
    sys.modules.setdefault(_m, _mod)

from app import woocommerce  # noqa: E402


def test_config_tiene_intervalo_de_resincronizacion_por_defecto():
    from app import config
    assert config.WOOCOMMERCE_SYNC_INTERVAL_HOURS > 0


def test_periodic_sync_loop_llama_sync_en_cada_intervalo():
    llamadas = []

    async def _fake_sync(full_replace=False):
        llamadas.append(full_replace)
        return {"total": 0, "sincronizados": 0}

    class _Detener(Exception):
        pass

    contador = {"n": 0}

    async def _fake_sleep(_segundos):
        contador["n"] += 1
        if contador["n"] >= 4:
            raise _Detener()

    original = woocommerce.sync_catalog_from_woocommerce
    woocommerce.sync_catalog_from_woocommerce = _fake_sync
    try:
        try:
            asyncio.run(woocommerce.periodic_sync_loop(interval_hours=0.001, sleep_fn=_fake_sleep))
        except _Detener:
            pass
    finally:
        woocommerce.sync_catalog_from_woocommerce = original

    assert len(llamadas) == 3, llamadas
    assert all(fr is False for fr in llamadas)
