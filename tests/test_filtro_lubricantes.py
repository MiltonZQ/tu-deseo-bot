"""Regresiones del incidente del 2026-08-05 20:55 (contacto 573232834767).

El cliente venía de ver retardantes y pidió "puedo ver plugs anales"; el bot le
mandó cinco lubricantes anales. El LLM clasificó bien (categoria=anal), pero un
veto por la palabra suelta "anal" apagó el cambio de tema y el tipo volvió a
`lubricante`. Hay 15 plugs en el catálogo y no salió ninguno.

Estos tests EJECUTAN el código real, igual que test_categoria_pegada_y_fotos:
stubean las dependencias de runtime que no están instaladas y nulifican el
respaldo del LLM para que la lógica determinística sea lo único bajo prueba.
"""
from __future__ import annotations

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

from app import catalog, openai_client  # noqa: E402


async def _sin_llm(_texto):
    """El respaldo LLM no debe influir en estos tests (y no hay red).

    La versión real en producción acertó (categoria=anal) y aun así el veto la
    deshizo: el bug es determinístico, así que aquí el LLM se apaga del todo.
    """
    return None


openai_client.clasificar_intencion_llm = _sin_llm


def _clasificar(user_text, history=None, estado=None):
    return asyncio.run(
        catalog.clasificar_intencion_cliente(user_text, history or [], estado))


# ── Task 1: pedir OTRO tipo de producto no es un filtro de lubricantes ──

def test_pedir_plugs_desde_lubricantes_cambia_de_tema():
    """Incidente 2026-08-05 20:55. El cliente venía de ver retardantes y pidió
    "puedo ver plugs anales"; le llegaron cinco lubricantes anales. El LLM
    clasificó bien (categoria=anal); el veto por la palabra "anal" deshizo el
    cambio de tema y el tipo volvió a `lubricante`."""
    r = _clasificar("puedo ver plugs anales", estado="lubricantes-y-cuidado")
    assert r["categoria_funcional"] == "anal", r["categoria_funcional"]
    assert r["restricciones"].get("tipo") == "plug", r["restricciones"]


def test_otros_tipos_propios_tampoco_se_quedan_en_lubricantes():
    """El veto por palabra suelta atrapaba a cualquier tipo de producto cuyo
    nombre trajera una palabra filtro ("anal", "silicona"). Hoy estos son
    cambio de tema: el cliente nombra una clase, no filtra."""
    for msg, tipo in (("quiero unas bolas anales", "bolas"),
                      ("vibrador anal", "vibrador"),
                      ("quiero un dildo de silicona", "dildo"),
                      ("quiero otros tipos de vibradores", "vibrador")):
        r = _clasificar(msg, estado="lubricantes-y-cuidado")
        assert r["restricciones"].get("tipo") == tipo, (msg, r["restricciones"])


def test_anal_a_secas_sigue_siendo_un_filtro_de_lubricantes():
    """Lo que la excepción vino a proteger y no se toca: tras "¿de agua, de
    sabores o anal?", la respuesta "anal" es un filtro, no una categoría."""
    for msg in ("anal", "sabores", "de agua", "cual me recomiendas",
                "que otros tipos tienen"):
        r = _clasificar(msg, estado="lubricantes-y-cuidado")
        assert r["categoria_funcional"] == "lubricantes-y-cuidado", msg


def test_nombrar_lubricante_no_es_cambio_de_tema():
    """El tipo propio es `lubricante`: misma categoría que la memoria, así que
    el veto sigue en pie sin necesidad de tratarlo aparte."""
    r = _clasificar("lubricante anal", estado="lubricantes-y-cuidado")
    assert r["categoria_funcional"] == "lubricantes-y-cuidado"


# ── Task 2: "dilatador" identifica un plug en boca del cliente ──

def test_dilatadores_son_plugs_para_el_cliente():
    """`dilatador` estaba en el clasificador de productos pero no en el de
    mensajes, así que "tienen dilatadores anales" no traía tipo propio y se
    quedaba pegado en lubricantes."""
    r = _clasificar("tienen dilatadores anales", estado="lubricantes-y-cuidado")
    assert r["restricciones"].get("tipo") == "plug", r["restricciones"]
