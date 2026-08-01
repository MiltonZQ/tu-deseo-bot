"""La intención del cliente es un conjunto de restricciones que se ACUMULA.

Bug que lo motivó (2026-08-01):

    cliente: tienen vibradores        → categoria_funcional = vibradores
    bot:     ¿para ella, para él, anal/próstata, o en pareja?
    cliente: anal                     → categoria_funcional = anal   ← se perdió "vibradores"
    bot:     [enemas de limpieza y plugs sin vibración]

El bot preguntaba por un ATRIBUTO del vibrador y el clasificador lo interpretaba
como una categoría nueva, tirando "vibradores" a la basura. Había una sola
variable donde hacían falta varias restricciones.

Regla general que sustituye a los parches por palabra:
  - una palabra que aporta una faceta NUEVA (zona, atributo, control) REFINA
  - una palabra que aporta un TIPO distinto REEMPLAZA (cambio de tema)
"""
from __future__ import annotations

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

from app import facetas  # noqa: E402

leer = facetas.interpretar_mensaje
fusionar = facetas.fusionar_restricciones


# ── El caso del reporte ──

def test_anal_refina_vibradores_no_lo_reemplaza():
    r1 = fusionar({}, leer("tienen vibradores"))
    assert r1 == {"tipo": "vibrador"}
    r2 = fusionar(r1, leer("anal"))
    assert r2.get("tipo") == "vibrador", "no puede perderse el vibrador"
    assert r2.get("zona") == "anal"


def test_pene_refina_vibradores():
    r1 = fusionar({}, leer("tienen vibradores"))
    r2 = fusionar(r1, leer("pene"))
    assert r2.get("tipo") == "vibrador" and r2.get("zona") == "pene"


def test_con_app_refina_sin_perder_nada():
    r = fusionar({"tipo": "vibrador", "zona": "anal"}, leer("con app"))
    assert r == {"tipo": "vibrador", "zona": "anal", "control": "app"}


# ── Cambio de tema: un tipo distinto SÍ reemplaza ──

def test_un_tipo_distinto_reemplaza_todo():
    previas = {"tipo": "vibrador", "zona": "anal", "control": "app"}
    r = fusionar(previas, leer("ahora quiero lubricantes"))
    assert r.get("tipo") == "lubricante"
    assert "zona" not in r or r["zona"] != "anal", "no debe arrastrar la zona anterior"
    assert "control" not in r, "no debe arrastrar el control anterior"


def test_latigos_cambian_de_tema_desde_bombas():
    r = fusionar({"tipo": "bomba", "zona": "pene"}, leer("me gustaria ver si tinen latigos"))
    assert r.get("tipo") == "bondage"


def test_respuesta_afirmativa_no_cambia_nada():
    previas = {"tipo": "succionador", "zona": "clitoris"}
    for msg in ("si", "ok dale", "muéstrame", "claro"):
        assert fusionar(previas, leer(msg)) == previas, msg


# ── Atributos que implican un tipo ──

def test_sabores_implica_lubricante_si_el_tipo_no_lo_admite():
    """Venía de bombas: 'sabores' no es un atributo de una bomba de vacío."""
    r = fusionar({"tipo": "bomba", "zona": "pene"}, leer("sabores"))
    assert r.get("tipo") == "lubricante"
    assert "sabor" in r.get("atributos", [])


def test_sabores_solo_refina_si_ya_hablabamos_de_lubricantes():
    r = fusionar({"tipo": "lubricante"}, leer("sabores"))
    assert r.get("tipo") == "lubricante"
    assert "sabor" in r.get("atributos", [])


def test_anal_dentro_de_lubricantes_es_un_filtro():
    """Regresión: 'anal' tras hablar de lubricantes es 'lubricante anal'."""
    r = fusionar({"tipo": "lubricante"}, leer("anal"))
    assert r.get("tipo") == "lubricante", "no debe convertirse en juguete anal"
    assert r.get("zona") == "anal"


# ── Vocabulario ──

def test_reconoce_el_vocabulario_del_cliente():
    casos = [
        ("quiero un succionador", "tipo", "succionador"),
        ("tienen bombas para el pene", "tipo", "bomba"),
        ("busco un consolador", "tipo", "dildo"),
        ("tienen esposas", "tipo", "bondage"),
        ("un plug", "tipo", "plug"),
        ("algo para el clitoris", "zona", "clitoris"),
        ("con control remoto", "control", "remoto"),
        ("en pareja", "genero_uso", "pareja"),
    ]
    for texto, campo, esperado in casos:
        r = leer(texto)
        assert r.get(campo) == esperado, f"{texto!r} → {r}"


def test_no_coincide_dentro_de_otras_palabras():
    """Mismo blindaje que en la clasificación de productos."""
    assert leer("busco algo profundo").get("tipo") != "funda"
    assert leer("para doble penetracion").get("zona") != "pene"


def test_mensajes_sin_producto_no_aportan_restricciones():
    for msg in ("hola buenas tardes", "cuanto cuesta el envio", "ya hice el pago"):
        assert leer(msg) == {}, msg


# ── Integración con el clasificador que usa el pipeline ──

def test_el_clasificador_devuelve_restricciones_acumuladas():
    import asyncio
    from app import catalog, openai_client

    async def _sin_llm(_t):
        return None
    openai_client.clasificar_intencion_llm = _sin_llm

    async def _run():
        r1 = await catalog.clasificar_intencion_cliente("tienen vibradores", [], None, None)
        assert r1["restricciones"] == {"tipo": "vibrador"}, r1["restricciones"]
        r2 = await catalog.clasificar_intencion_cliente(
            "anal", [], r1["categoria_funcional"], r1["restricciones"])
        # El caso exacto del reporte: no puede perderse "vibrador".
        assert r2["restricciones"].get("tipo") == "vibrador", r2["restricciones"]
        assert r2["restricciones"].get("zona") == "anal", r2["restricciones"]
    asyncio.run(_run())
