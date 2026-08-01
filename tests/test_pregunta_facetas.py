"""Preguntar UNA cosa antes de listar, cuando la petición es demasiado amplia.

El caso que lo motivó (1/08/26):

    cliente: quiero ver lubricantes
    bot:     1️⃣ BliX Lubricante Anal   2️⃣ Lubricante Anal Sen Intimo
             3️⃣ BliX H2O Neutro        4️⃣ Lubricante Anal 500 Ml
             5️⃣ Lubricante Neutro

Hay ~20 lubricantes ofrecibles y solo 5 huecos, así que el cliente recibe una
muestra al azar de cosas que no se parecen entre sí en vez de lo que buscaba.
Con una pregunta ("¿neutro, con sabores, anal…?") los 5 huecos se llenan con lo
que pidió.

La pregunta se construye desde el inventario REAL: ofrecer una rama sin producto
detrás es peor que no preguntar, porque el cliente la elige, la consulta exacta
da 0 filas y la escalera de relajación le devuelve justo lo que no pidió.
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

from app import catalog  # noqa: E402


# No hay pytest en el entorno (ver tests/run.py), así que tampoco hay fixture
# `monkeypatch`. Este contextmanager hace lo mismo y deja el módulo como estaba.
class parchar:
    """with parchar(catalog, buscar_por_restricciones=fake): ..."""

    def __init__(self, obj, **kwargs):
        self._obj, self._nuevos = obj, kwargs
        self._previos: dict = {}

    def __enter__(self):
        for k, v in self._nuevos.items():
            self._previos[k] = getattr(self._obj, k)
            setattr(self._obj, k, v)
        return self._obj

    def __exit__(self, *exc):
        for k, v in self._previos.items():
            setattr(self._obj, k, v)
        return False


# ── facetas_disponibles: qué ramas tienen producto ofrecible ──

FILAS = [
    {"atributos": ["agua", "neutro"], "zona": "ninguna", "genero_uso": "unisex"},
    {"atributos": ["agua", "sabor"], "zona": "ninguna", "genero_uso": "mujer"},
    {"atributos": ["desensibilizante"], "zona": "anal", "genero_uso": "unisex"},
]


def test_facetas_disponibles_agrupa_por_atributo_zona_y_genero():
    async def fake(restricciones, exclude_ids, limit):
        return FILAS

    with parchar(catalog, _consultar_restricciones=fake):
        res = asyncio.run(catalog.facetas_disponibles({"tipo": "lubricante"}))
    assert res["atributos"]["agua"] == 2
    assert res["atributos"]["sabor"] == 1
    assert res["zonas"]["anal"] == 1
    assert res["zonas"]["ninguna"] == 2
    assert res["generos"]["unisex"] == 2


def test_facetas_disponibles_no_revienta_sin_db():
    """Una excepción aquí no puede tumbar el turno: sin recuentos, no se pregunta."""
    async def boom(*a, **k):
        raise RuntimeError("sin pool")

    with parchar(catalog, _consultar_restricciones=boom):
        res = asyncio.run(catalog.facetas_disponibles({"tipo": "lubricante"}))
    assert res == {"atributos": {}, "zonas": {}, "generos": {}}


def test_facetas_disponibles_sin_restricciones_no_consulta():
    llamadas = []

    async def fake(restricciones, exclude_ids, limit):
        llamadas.append(restricciones)
        return FILAS

    with parchar(catalog, _consultar_restricciones=fake):
        res = asyncio.run(catalog.facetas_disponibles({}))
    assert res == {"atributos": {}, "zonas": {}, "generos": {}}
    assert not llamadas, "sin ancla, contar el catálogo entero no dice nada"
