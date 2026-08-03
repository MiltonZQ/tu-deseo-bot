"""Bug reportado el 2026-08-02: el cliente daba su direccion (con la palabra
"Conjunto", tipica de "Conjunto Residencial..."), el bot la interpretaba como
intencion de comprar lenceria, reseteaba el estado (productos_mostrados,
categoria) y a partir de ahi ofrecia productos sin relacion / fotos equivocadas.

Causa raiz: clasificar_intencion_cliente() no distingue "el cliente esta dando
su direccion de entrega" de "el cliente esta pidiendo un producto nuevo" —
corre el mismo clasificador de intencion sobre cualquier texto.
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

from app import catalog, openai_client  # noqa: E402


async def _sin_llm(_texto):
    return None


openai_client.clasificar_intencion_llm = _sin_llm


def _clasificar(user_text, history=None, estado=None):
    return asyncio.run(
        catalog.clasificar_intencion_cliente(user_text, history or [], estado))


def test_direcciones_reales_se_reconocen_como_direccion():
    direcciones = [
        "Calle 1#32a-47",
        "Conjunto Residencial Torres de Zajaguán, apto 302",
        "Cra 70d #64-38 sur",
        "Barrio Hayuelos, Manzana 5 casa 12",
    ]
    for d in direcciones:
        assert catalog._parece_direccion_envio(d), d


def test_mensajes_de_producto_no_se_confunden_con_direccion():
    productos = [
        "quiero un conjunto de lencería negro",
        "tienen kit bdsm",
        "dame el dildo realista",
    ]
    for p in productos:
        assert not catalog._parece_direccion_envio(p), p


def test_direccion_con_conjunto_no_resetea_la_categoria_activa():
    # El cliente ya estaba viendo dildos; da su dirección con "Conjunto
    # Residencial" y NO debe cambiar a lencería ni perder la categoría.
    r = _clasificar(
        "Conjunto Residencial Torres de Zajaguán, apto 302",
        estado="dildos",
    )
    assert r["categoria_funcional"] == "dildos", r["categoria_funcional"]


def test_direccion_sola_sin_estado_previo_no_activa_ninguna_categoria():
    r = _clasificar("Conjunto Residencial Torres de Zajaguán, apto 302")
    assert r["categoria_funcional"] is None, r["categoria_funcional"]
