"""El LLM como último recurso para resolver a qué producto se refiere el cliente.

La cascada determinística de `app/seleccion.py` cubre nombre, token distintivo,
precio y posición. Lo que no cubre es la referencia por atributos que no están en
el nombre: "quiero las esposas negras peludas" describe el producto sin nombrarlo.
Ampliar listas de palabras a mano para siempre no escala; eso es justo lo que
`reordenar_candidatos_por_relevancia` ya resolvió para el orden de candidatos.

La garantía es la misma y es estructural, no una promesa del prompt: se le pasan
los productos mostrados con su id, y **cualquier id que no estuviera en esa lista
se descarta**. El LLM no puede inventar un producto porque el conjunto de salidas
válidas está acotado por el conjunto de entrada.
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

from app import openai_client  # noqa: E402

_MOSTRADOS = [
    {"id": 11, "nombre": "Esposas Peludas Negras Bondage", "precio": 29900},
    {"id": 12, "nombre": "Kit Bondage Rojo 7 Piezas", "precio": 89000},
    {"id": 13, "nombre": "Antifaz Satinado Negro", "precio": 15000},
]


class _FakeResp:
    def __init__(self, content):
        self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=content))]


def _con_respuesta_llm(contenido):
    class _FakeCompletions:
        async def create(self, *_a, **_k):
            return _FakeResp(contenido)

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    return lambda: _FakeClient()


def _con_fallo_llm():
    class _FakeCompletions:
        async def create(self, *_a, **_k):
            raise RuntimeError("timeout")

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeClient:
        chat = _FakeChat()

    return lambda: _FakeClient()


def _resolver(contenido, texto="quiero las esposas negras peludas"):
    openai_client._get_client = _con_respuesta_llm(contenido)
    return asyncio.run(openai_client.seleccionar_producto_llm(texto, _MOSTRADOS))


def test_resuelve_una_referencia_por_atributos():
    """El caso que la cascada determinística no alcanza: 'negras peludas' son
    atributos, no el nombre."""
    assert _resolver('{"ids": [11]}') == [11]


def test_puede_resolver_varios_productos():
    assert _resolver('{"ids": [11, 13]}', "las esposas y el antifaz") == [11, 13]


def test_un_id_inventado_se_descarta():
    """La garantía estructural: 999 no estaba en la entrada, así que no puede
    salir. No depende de que el prompt convenza al modelo."""
    assert _resolver('{"ids": [999]}') is None


def test_un_id_inventado_no_arrastra_a_los_validos():
    assert _resolver('{"ids": [999, 11]}') == [11]


def test_respuesta_no_parseable_devuelve_none():
    assert _resolver("lo siento, no estoy seguro") is None


def test_lista_vacia_devuelve_none():
    """'null' es una respuesta VÁLIDA y esperada: el cliente no se refería a
    ninguno de los mostrados. Devolver None deja que el llamador pregunte."""
    assert _resolver('{"ids": []}') is None


def test_un_fallo_de_red_no_rompe_el_turno():
    openai_client._get_client = _con_fallo_llm()
    assert asyncio.run(openai_client.seleccionar_producto_llm(
        "las esposas negras", _MOSTRADOS)) is None


def test_sin_productos_mostrados_ni_se_llama_al_llm():
    """Sin mundo cerrado no hay nada que acotar la respuesta, así que no se
    gasta una llamada."""
    llamado = False

    class _FakeCompletions:
        async def create(self, *_a, **_k):
            nonlocal llamado
            llamado = True
            raise AssertionError("no debería llamarse")

    class _FakeChat:
        completions = _FakeCompletions()

    openai_client._get_client = lambda: type("_C", (), {"chat": _FakeChat()})()
    assert asyncio.run(openai_client.seleccionar_producto_llm("el 2", [])) is None
    assert not llamado
