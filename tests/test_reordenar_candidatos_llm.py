"""El usuario pidio explicitamente: si el regex no reconoce una palabra
concreta (ej. una variante de producto que no esta en ninguna lista), que el
LLM entienda lo que el cliente busca y se lo pase al codigo de forma que
pueda usarlo, en vez de seguir ampliando listas de palabras clave a mano para
siempre.

reordenar_candidatos_por_relevancia() es esa capa: NO decide la categoria
(eso lo sigue haciendo el filtro deterministico de catalog.py) y NO puede
inventar productos — solo puede reordenar ids que ya estaban en la lista de
candidatos que se le paso. Si el LLM devuelve algo invalido, tarda, o falla,
se usa el orden de siempre.
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

_CANDIDATOS = [
    {"id": 1, "nombre": "Disfraz Colegiala Inocente Lerot"},
    {"id": 2, "nombre": "Disfraz Policía Lerot"},
    {"id": 3, "nombre": "Disfraz Enfermera Sexy Dulce Tentación"},
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


def test_reordena_usando_solo_ids_validos():
    openai_client._get_client = _con_respuesta_llm('{"ids": [1, 3, 2]}')
    orden = asyncio.run(openai_client.reordenar_candidatos_por_relevancia(
        "quiero el disfraz de colegiala", _CANDIDATOS))
    assert orden == [1, 3, 2]


def test_ids_inventados_por_el_llm_se_descartan():
    # El LLM devuelve un id que NO estaba en la lista (999): se descarta y no
    # rompe el resultado — nunca puede "inventar" un producto.
    openai_client._get_client = _con_respuesta_llm('{"ids": [999, 2, 1]}')
    orden = asyncio.run(openai_client.reordenar_candidatos_por_relevancia(
        "disfraz de policia", _CANDIDATOS))
    assert orden == [2, 1, 3]  # 999 descartado; 3 no mencionado, se agrega al final


def test_respuesta_no_parseable_devuelve_none():
    openai_client._get_client = _con_respuesta_llm("no soy json")
    orden = asyncio.run(openai_client.reordenar_candidatos_por_relevancia(
        "disfraz de enfermera", _CANDIDATOS))
    assert orden is None


def test_falla_de_red_devuelve_none_sin_reventar():
    def _rompe():
        raise ConnectionError("timeout simulado")
    openai_client._get_client = _rompe
    orden = asyncio.run(openai_client.reordenar_candidatos_por_relevancia(
        "disfraz de colegiala", _CANDIDATOS))
    assert orden is None


def test_con_un_solo_candidato_no_llama_al_llm():
    llamado = {"n": 0}

    def _contar():
        llamado["n"] += 1
        raise AssertionError("no debe llamarse con un solo candidato")

    openai_client._get_client = _contar
    orden = asyncio.run(openai_client.reordenar_candidatos_por_relevancia(
        "disfraz de colegiala", _CANDIDATOS[:1]))
    assert orden is None
    assert llamado["n"] == 0
