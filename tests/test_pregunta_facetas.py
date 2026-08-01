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


# ── El menú: qué se le pregunta al cliente ──

from app import facetas, preguntas  # noqa: E402

DISPONIBLE = {"atributos": {"neutro": 5, "sabor": 6, "calor": 3, "silicona": 1},
              "zonas": {"anal": 4, "ninguna": 16}, "generos": {}}


def test_solo_ofrece_ramas_con_stock():
    texto = preguntas.construir("lubricante", DISPONIBLE)
    assert "sabores" in texto and "anal" in texto
    assert "silicona" not in texto, "1 producto no es una rama del menú"


def test_respeta_el_maximo_de_ramas():
    """Más de 4 opciones en un mensaje de WhatsApp no se leen, se saltan."""
    abundante = {"atributos": {a: 9 for a in
                               ("neutro", "sabor", "calor", "frio", "silicona", "hibrido")},
                 "zonas": {"anal": 9}, "generos": {}}
    texto = preguntas.construir("lubricante", abundante)
    ofrecidas = [etq for _c, _g, etq in preguntas._MENUS["lubricante"]
                 if etq.strip("*") in texto or etq in texto]
    assert len(ofrecidas) == preguntas.MAX_RAMAS


def test_sin_ramas_suficientes_no_pregunta():
    """Con una sola rama viva no hay nada que preguntar: se lista y ya."""
    assert preguntas.construir("lubricante", {"atributos": {"sabor": 9},
                                              "zonas": {}, "generos": {}}) is None


def test_tipo_sin_menu_no_pregunta():
    assert preguntas.construir("enema", DISPONIBLE) is None
    assert preguntas.construir(None, DISPONIBLE) is None


def test_recuentos_vacios_no_preguntan():
    assert preguntas.construir("lubricante", {"atributos": {}, "zonas": {}, "generos": {}}) is None


def test_la_respuesta_del_cliente_filtra_de_verdad():
    """Cada rama del menú debe ser interpretable por el vocabulario cliente.

    Si no, se pregunta algo que luego no se sabe leer: el cliente responde
    "híbrido", el mensaje no aporta ninguna restricción, y se le lista lo mismo
    que si no hubiera contestado.
    """
    for tipo, menu in preguntas._MENUS.items():
        for clave, grupo, _etiqueta in menu:
            leido = facetas.interpretar_mensaje(clave)
            assert leido, f"[{tipo}] la rama {clave!r} no la entiende interpretar_mensaje"
            campo = {"atributos": "atributos", "zonas": "zona", "generos": "genero_uso"}[grupo]
            assert campo in leido, (
                f"[{tipo}] la rama {clave!r} debería aportar {campo}, aportó {leido}")


def test_las_ramas_existen_en_el_vocabulario_cerrado():
    """Una rama mal escrita nunca tendría recuento y jamás se ofrecería."""
    for tipo, menu in preguntas._MENUS.items():
        assert tipo in facetas.TIPOS
        for clave, grupo, _etiqueta in menu:
            if grupo == "zonas":
                assert clave in facetas.ZONAS, f"zona desconocida: {clave}"
            elif grupo == "generos":
                assert clave in facetas.GENEROS, f"género desconocido: {clave}"
            else:
                assert clave in facetas._ATRIBUTOS, f"atributo desconocido: {clave}"
