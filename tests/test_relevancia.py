"""Los productos que se recuperan deben tener relación con lo que pidió el cliente.

Reportes del 1/08:

  - "succionadores" → de 12 succionadores, los 5 que se llaman "Succionador"
    caían en las posiciones 5,7,9,10,11 del ORDER BY LENGTH(nombre), o sea
    casi todos en la segunda página. El texto del cliente nunca llegaba a la
    consulta.
  - "multiorgasmo" → 1 de 5 productos: el scoring compara tokens exactos y los
    otros 4 se llaman "Multiorgasmos".
  - "Hola, que dildos tinen" → 2 de 22 dildos: dos coincidencias en la
    DESCRIPCIÓN (0.5 + 0.5) alcanzaban el umbral de 1.0 sin ninguna
    coincidencia en el nombre.
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


class parchar:
    """Sustituye atributos de un módulo y los restaura al salir.

    No hay pytest en el entorno (ver tests/run.py), así que tampoco fixture
    `monkeypatch`.
    """

    def __init__(self, obj, **kwargs):
        self._obj, self._nuevos, self._previos = obj, kwargs, {}

    def __enter__(self):
        for k, v in self._nuevos.items():
            self._previos[k] = getattr(self._obj, k)
            setattr(self._obj, k, v)
        return self._obj

    def __exit__(self, *exc):
        for k, v in self._previos.items():
            setattr(self._obj, k, v)
        return False


# ── Tarea 1: plurales ──

def test_el_plural_del_cliente_casa_con_el_singular_del_producto():
    """'succionadores' → 'Succionador de Clítoris Tenera 2'."""
    p = {"nombre": "Succionador de Clítoris Tenera 2", "descripcion": ""}
    assert catalog._score_candidato(p, "quizás tienen succionadores") >= 2.0


def test_el_singular_del_cliente_casa_con_el_plural_del_producto():
    """'multiorgasmo' → 'Multiorgasmos Original X 30 Ml'."""
    p = {"nombre": "Multiorgasmos Original X 30 Ml Sen Intimo", "descripcion": ""}
    assert catalog._score_candidato(p, "tienen productos multiorgasmo") >= 2.0


def test_no_confunde_palabras_que_solo_comparten_prefijo():
    """`_misma_familia` (prefijo puro, catalog.py:1072) sería demasiado laxo."""
    assert not catalog._mismo_termino("anal", "analgesico")
    assert not catalog._mismo_termino("gel", "gelatina")
    assert not catalog._mismo_termino("pro", "prostata")


def test_reconoce_las_dos_formas_de_plural():
    assert catalog._mismo_termino("succionador", "succionadores")
    assert catalog._mismo_termino("multiorgasmos", "multiorgasmo")
    assert catalog._mismo_termino("anillo", "anillos")
    assert catalog._mismo_termino("anal", "anales")
    assert catalog._mismo_termino("dildo", "dildo")


# ── Tarea 2: qué productos entran en la página ──

# Los 12 succionadores ofrecibles reales, en el orden que devuelve el SQL
# (LENGTH(nombre) ASC). Los 5 que se llaman "Succionador" están en las
# posiciones 5,7,9,10,11: con LIMIT 5 el cliente no veía casi ninguno.
SUCCIONADORES = [
    {"id": 1, "nombre": "Satisfyer Curvy 3 Connect App", "descripcion": ""},
    {"id": 2, "nombre": "Satisfyer Love Triangle Negro", "descripcion": ""},
    {"id": 3, "nombre": "Satisfyer Love Triangle Blanco", "descripcion": ""},
    {"id": 4, "nombre": "Satisfyer Pro 2+ Generación Rosa", "descripcion": ""},
    {"id": 5, "nombre": "Succionador de Clítoris Tenera 2", "descripcion": ""},
    {"id": 6, "nombre": "Satisfyer Pro 3+ Generación Negro", "descripcion": ""},
    {"id": 7, "nombre": "Vibrador y Succionador Ohlala Rose", "descripcion": ""},
    {"id": 8, "nombre": "Satisfyer Pro 2 Generación Oro Rosa", "descripcion": ""},
    {"id": 9, "nombre": "Satisfyer Penguin Succionador Clitorial", "descripcion": ""},
    {"id": 10, "nombre": "Satisfyer Succionador Clitorial Number One", "descripcion": ""},
    {"id": 11, "nombre": "Succionador Con Ondas Y Vibracion Nyla Fuscia", "descripcion": ""},
    {"id": 12, "nombre": "Estimulador de Clítoris Sona 2 Cruise Lelo Original",
     "descripcion": ""},
]

# Los que llevan "Succionador" en el nombre.
IDS_SUCCIONADOR = {5, 7, 9, 10, 11}


def _catalogo_fake(filas):
    async def fake_fetch(sql, *params):
        return [dict(p) for p in filas]
    return parchar(catalog, _fetch_restricciones=fake_fetch)


def test_la_primera_pagina_trae_los_productos_que_el_cliente_nombro():
    """El reporte del 1/08: de 12 succionadores, la primera vuelta traía 4
    Satisfyer y un solo 'Succionador'. Los 5 que se llaman así deben ENTRAR,
    no reordenarse dentro de una página que no los contenía."""
    with _catalogo_fake(SUCCIONADORES):
        res = asyncio.run(catalog._consultar_restricciones(
            {"tipo": "succionador"}, None, 5, user_text="quizás tienen succionadores"))
    assert {p["id"] for p in res} == IDS_SUCCIONADOR, \
        f"recibidos: {[p['nombre'] for p in res]}"


def test_el_sql_no_puede_cortar_antes_de_ordenar():
    """Si el LIMIT del SQL siguiera siendo `limit`, el fixture nunca vería los
    productos de las posiciones 6-12 y el test anterior pasaría por azar."""
    visto = {}

    async def fake_fetch(sql, *params):
        visto["sql"] = sql
        return [dict(p) for p in SUCCIONADORES]

    with parchar(catalog, _fetch_restricciones=fake_fetch):
        asyncio.run(catalog._consultar_restricciones(
            {"tipo": "succionador"}, None, 5, user_text="succionadores"))
    assert "LIMIT 5" not in visto["sql"], \
        "con texto del cliente hay que traer el conjunto completo y ordenar aquí"


def test_sin_texto_del_cliente_el_orden_no_cambia():
    """`contar_por_restricciones` y `facetas_disponibles` llaman sin texto: no
    deben pagar el coste de ordenar ni ver alterado su resultado."""
    with _catalogo_fake(SUCCIONADORES):
        res = asyncio.run(catalog._consultar_restricciones(
            {"tipo": "succionador"}, None, 5))
    assert [p["id"] for p in res] == [1, 2, 3, 4, 5]


def test_buscar_por_restricciones_propaga_el_texto():
    visto = {}

    async def fake_consultar(restricciones, exclude_ids, limit, user_text=""):
        visto["user_text"] = user_text
        return [dict(p) for p in SUCCIONADORES[:5]]

    with parchar(catalog, _consultar_restricciones=fake_consultar):
        asyncio.run(catalog.buscar_por_restricciones(
            {"tipo": "succionador"}, limit=5, user_text="tienen succionadores"))
    assert visto["user_text"] == "tienen succionadores"


# ── Tarea 3: la paginación conserva el criterio ──

from tests.stubs import importar_main  # noqa: E402

main = importar_main()


def _sin_db(**extra):
    """Silencia todas las consultas del pipeline salvo las que interesan."""
    async def sin_contar(r):
        return 12

    async def sin_facetas(r):
        return {"atributos": {}, "zonas": {}, "generos": {}}

    async def sin_nombre(*a, **k):
        return []

    async def sin_typos(t, *a, **k):
        return t

    async def sin_reco(**k):
        return []

    base = dict(contar_por_restricciones=sin_contar, facetas_disponibles=sin_facetas,
                buscar_producto_especifico=sin_nombre,
                corregir_typos_contra_catalogo=sin_typos,
                get_productos_para_recomendar=sin_reco)
    base.update(extra)
    return parchar(catalog, **base)


def _estado_succionadores(**kw):
    base = {"categoria_busqueda": "succionadores",
            "categoria_funcional": "succionadores", "genero": None,
            "calificado": True, "productos_mostrados": [5, 7, 9, 10, 11],
            "restricciones": {"tipo": "succionador"}, "preguntas_hechas": [],
            "texto_busqueda": "quizás tienen succionadores"}
    base.update(kw)
    return base


def _espiar_texto(mensaje, estado, producto=None):
    """Corre un turno y devuelve (user_text que llegó a la búsqueda, info)."""
    visto = {}

    async def fake_buscar(restricciones, exclude_ids=None, limit=5,
                          permitir_relajar=True, user_text=""):
        visto["user_text"] = user_text
        p = producto or SUCCIONADORES[4]
        return catalog.Resultado(
            productos=[dict(p, precio=1000, imagen_url="http://x/a.jpg",
                            tipo="succionador", zona="clitoris", atributos=[])],
            restricciones=restricciones)

    with _sin_db(buscar_por_restricciones=fake_buscar):
        _c, info = asyncio.run(main._recuperar_candidatos(mensaje, [], estado))
    return visto.get("user_text"), info


def test_ver_mas_reutiliza_el_texto_de_la_busqueda_original():
    """'Ver más' no tiene tokens de producto: sin esto la página 2 volvería a
    ordenarse por longitud de nombre."""
    texto, _info = _espiar_texto("Ver más", _estado_succionadores())
    assert texto == "quizás tienen succionadores"


def test_una_busqueda_nueva_reemplaza_el_texto_guardado():
    texto, info = _espiar_texto("ahora quiero succionadores con app",
                                _estado_succionadores())
    assert texto == "ahora quiero succionadores con app"
    assert info["texto_busqueda"] == "ahora quiero succionadores con app"


def test_el_texto_de_busqueda_se_persiste_para_el_turno_siguiente():
    texto, info = _espiar_texto("quizás tienen succionadores", None)
    assert texto == "quizás tienen succionadores"
    assert info["texto_busqueda"] == "quizás tienen succionadores"
