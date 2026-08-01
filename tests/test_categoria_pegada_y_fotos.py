"""Regresiones de los bugs reportados el 2026-08-01 (conversación "Prueba 2").

Cuatro fallos distintos, con tres causas raíz:

  1. "bombas para el pene" solo mostraba el Hefesto, y "succionador" nunca mostraba
     el Nyla. Causa: `_categoria_normalizada` mezclaba nombre + descripción +
     categoría de origen en un solo texto, y una palabra de la DESCRIPCIÓN
     disparaba una regla anterior (más genérica) que se llevaba el producto.

  2. El cliente preguntaba por látigos, por lubricantes y por sabores, y seguía
     recibiendo bombas para el pene del primer turno. Causa: el motor de memoria
     solo aceptaba "cambio de tema" si el mensaje contenía uno de 15 sustantivos
     fijos, lista que omitía lubricante, látigo, plug, arnés, kit, masturbador…

  3. Al pedir látigos (que no hay), llegaban dos anillos vibradores al azar.
     Dos causas sumadas: el Intento E-bis relajaba la categoría ignorando el
     subtipo pedido, y `_handle_message` forzaba las fotos de los candidatos aunque
     la respuesta del LLM no ofreciera ningún producto (mensaje de escalado).

A diferencia del resto de la suite, estos tests EJECUTAN el código real: stubean
las dependencias de runtime que no están instaladas y, para las consultas, un
pool de DB falso con un catálogo mínimo.
"""
from __future__ import annotations

import ast
import asyncio
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Dependencias de runtime que no hacen falta para la lógica bajo prueba.
# setdefault: si están instaladas de verdad (Docker), se usan las reales.
for _m in ("asyncpg", "httpx", "openai", "qdrant_client", "redis", "redis.asyncio",
           "tiktoken", "PIL", "PIL.Image"):
    _mod = types.ModuleType(_m)
    _mod.__getattr__ = lambda _n: type("_Any", (), {"__init__": lambda *a, **k: None})  # type: ignore[attr-defined]
    sys.modules.setdefault(_m, _mod)

from app import catalog, db, openai_client  # noqa: E402


async def _sin_llm(_texto):
    """El respaldo LLM no debe influir en estos tests (y no hay red)."""
    return None


openai_client.clasificar_intencion_llm = _sin_llm


def _clasificar(user_text, history=None, estado=None):
    return asyncio.run(
        catalog.clasificar_intencion_cliente(user_text, history or [], estado))


# ── Bug 1: la descripción secuestraba la categoría ──

_BENDER = (
    "Bomba Automática para Pene Recargable Bender Optimus Pro",
    "Bomba automática para pene recargable Bender Optimus Pro CamToyz con sistema "
    "eléctrico de succión y válvula de liberación de aire para mayor seguridad.",
    "Bombas para el Pene",
)
_NYLA = (
    "Succionador Con Ondas Y Vibracion Nyla Fuscia",
    "Succionador de clítoris Nyla con tecnología de ondas de presión para "
    "estimulación precisa e intensa. Compatible con lubricantes a base de agua.",
    "Juguetes",
)
_HEFESTO = (
    "Bomba Para El Pene Hefesto",
    "Bomba para el pene Hefesto con sistema de vacío diseñada para mejorar la firmeza.",
    "Juguetes",
)


def test_bomba_bender_no_la_secuestra_la_palabra_succion_de_su_descripcion():
    # "sistema eléctrico de succión" hacía que cayera en succionadores.
    assert catalog._categoria_normalizada(*_BENDER) == "bombas-pene"


def test_succionador_nyla_no_lo_secuestra_la_palabra_lubricantes_de_su_descripcion():
    # "Compatible con lubricantes a base de agua" lo mandaba a lubricantes-y-cuidado.
    assert catalog._categoria_normalizada(*_NYLA) == "succionadores"


def test_las_dos_bombas_del_catalogo_quedan_en_la_misma_categoria():
    assert (catalog._categoria_normalizada(*_BENDER)
            == catalog._categoria_normalizada(*_HEFESTO) == "bombas-pene")


def test_la_descripcion_sigue_clasificando_cuando_el_nombre_no_dice_nada():
    # El nombre no da señal: hay que seguir mirando descripción y categoría origen.
    assert catalog._categoria_normalizada(
        "Kit Fiore", "Amarres, mordaza y látigo para bondage", "Fetish") == "pareja-y-bondage"
    assert catalog._categoria_normalizada("", "", "") == "juegos-y-accesorios"


# ── Bug 2: la categoría del primer turno se quedaba pegada ──

def test_latigos_no_heredan_la_categoria_bombas_del_turno_anterior():
    r = _clasificar("me gustaria ver si tinen latigos", estado="bombas-pene")
    assert r["categoria_funcional"] == "pareja-y-bondage"
    assert r["subtipo_detectado"] == "latigo"


def test_lubricantes_no_heredan_la_categoria_bombas_del_turno_anterior():
    r = _clasificar("puedo ver que lubricantes manejan", estado="bombas-pene")
    assert r["categoria_funcional"] == "lubricantes-y-cuidado"


def test_sabores_deriva_lubricantes_aunque_la_memoria_diga_bombas():
    r = _clasificar("sabores", estado="bombas-pene")
    assert r["categoria_funcional"] == "lubricantes-y-cuidado"
    assert r["subtipo_detectado"] == "sabores"


def test_anal_sigue_siendo_un_filtro_dentro_de_lubricantes():
    # Regresión del fix anterior: "anal" tras hablar de lubricantes NO es cambio
    # de tema, es "lubricante anal".
    r = _clasificar("anal", estado="lubricantes-y-cuidado")
    assert r["categoria_funcional"] == "lubricantes-y-cuidado"
    assert r["subtipo_detectado"] == "desensibiliz"


def test_respuesta_afirmativa_no_cambia_de_tema():
    for msg in ("si", "ok dale", "los rojos", "mas diseños"):
        r = _clasificar(msg, estado="succionadores")
        assert r["categoria_funcional"] == "succionadores", msg


def test_conversacion_reportada_completa():
    """Los 5 turnos exactos de la conversación del reporte."""
    esperado = [
        ("tienen bombas para el pene", "bombas-pene"),
        ("me gustaria ver si tinen latigos", "pareja-y-bondage"),
        ("puedo ver que lubricantes manejan", "lubricantes-y-cuidado"),
        ("sabores", "lubricantes-y-cuidado"),
    ]
    estado = None
    history: list[dict] = []
    for msg, cat in esperado:
        r = _clasificar(msg, history, estado)
        assert r["categoria_funcional"] == cat, f"{msg!r} → {r['categoria_funcional']}"
        history += [{"role": "user", "content": msg}, {"role": "assistant", "content": "..."}]
        estado = r["categoria_funcional"] or estado


# ── Bug 3a: E-bis relajaba la categoría ignorando el subtipo pedido ──

class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, *_a, **_k):
        return list(self._rows)

    async def fetchrow(self, *_a, **_k):
        return self._rows[0] if self._rows else None


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    def acquire(self):
        rows = self._rows

        class _Ctx:
            async def __aenter__(self):
                return _FakeConn(rows)

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


_CATALOGO_FAKE = [
    {"id": 1, "nombre": "Bomba Para El Pene Hefesto", "descripcion": "Bomba de vacío",
     "categoria": "Juguetes", "precio": 180000, "imagen_url": "http://x/1.jpg",
     "galeria_urls": None, "permalink": None},
    {"id": 2, "nombre": "Anillo Vibrador Candil para el Pene", "descripcion": "Anillo vibrador",
     "categoria": "Juguetes", "precio": 25000, "imagen_url": "http://x/2.jpg",
     "galeria_urls": None, "permalink": None},
    {"id": 3, "nombre": "Anillos para Pene Donut Stay Hard Kit x3", "descripcion": "Anillos",
     "categoria": "Juguetes", "precio": 14900, "imagen_url": "http://x/3.jpg",
     "galeria_urls": None, "permalink": None},
]


def _recomendar(**kwargs):
    original, db._pool = getattr(db, "_pool", None), _FakePool(_CATALOGO_FAKE)
    qdrant, catalog.config.QDRANT_ENABLED = catalog.config.QDRANT_ENABLED, False
    try:
        return asyncio.run(catalog.get_productos_para_recomendar(**kwargs))
    finally:
        db._pool = original
        catalog.config.QDRANT_ENABLED = qdrant


def test_subtipo_concreto_sin_stock_no_devuelve_productos_de_otra_categoria():
    # Pedir látigos no puede terminar en anillos vibradores.
    res = _recomendar(categoria_funcional="pareja-y-bondage", genero="hombre",
                      user_text="me gustaria ver si tinen latigos", subtipo="latigo")
    assert res == [], [p["nombre"] for p in res]


def test_sin_subtipo_e_bis_sigue_relajando_la_categoria_por_genero():
    # Regresión del fix anterior: "vibradores" + "hombre" no existe como
    # intersección, y E-bis debe traer anillos/fundas de hombre.
    res = _recomendar(categoria_funcional="vibradores", genero="hombre",
                      user_text="vibradores para el pene")
    assert res, "E-bis debe seguir funcionando cuando no hay subtipo concreto"
    assert all("anillo" in p["nombre"].lower() for p in res)


# ── Bug 3b: se forzaban fotos sobre respuestas que no ofrecían productos ──

def test_no_se_fuerzan_fotos_si_la_respuesta_no_ofrece_productos():
    src = (_ROOT / "app" / "main.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_message":
            fn_src = ast.get_source_segment(src, node)
            idx = fn_src.index("promete_productos")
            bloque = fn_src[idx:idx + 700]
            assert "_LISTA_PRODUCTOS_RE" in bloque and "_OFRECE_PRODUCTOS_RE" in bloque
            assert "final_productos = []" in bloque, (
                "sin oferta de productos en el texto no deben adjuntarse fotos")
            return
    raise AssertionError("No se encontró _handle_message")


def test_un_fallo_de_envio_no_cancela_las_fotos_restantes():
    """El try/except debe estar DENTRO del bucle de envío."""
    src = (_ROOT / "app" / "main.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_enviar_fotos_productos":
            for child in node.body:
                # Ningún try puede envolver el for: el for debe estar en el cuerpo.
                assert not isinstance(child, ast.Try), (
                    "el try no puede envolver el bucle entero: un fallo cancelaría "
                    "todas las fotos siguientes")
            fors = [n for n in ast.walk(node) if isinstance(n, ast.For)]
            assert fors and any(
                isinstance(c, ast.Try) for f in fors for c in ast.walk(f)), (
                "cada envío debe tener su propio try/except")
            return
    raise AssertionError("No se encontró _enviar_fotos_productos")
