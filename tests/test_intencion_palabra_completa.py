"""_intencion_desde_texto comparaba la clave como substring suelto del texto
completo (sin limites de palabra), a diferencia de _categoria_normalizada que
ya fue corregido para esto (ver tests/test_categoria_pegada_y_fotos.py, Bug 4).

Caso real: una clave corta de categoria puede aparecer como fragmento dentro
de una palabra mas larga que no tiene nada que ver con esa categoria.
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

from app import catalog  # noqa: E402


def test_kit_no_hace_match_dentro_de_una_palabra_mas_larga():
    # "kit" es clave de _INTENCION_A_CATEGORIA_FUNCIONAL (pareja-y-bondage).
    # "kitchenette" (o cualquier palabra que la contenga) no debe activarla.
    intencion, _ = catalog._intencion_desde_texto("vivo en un edificio con kitchenette")
    assert intencion != "kit", intencion


def test_conjunto_si_hace_match_cuando_es_palabra_completa():
    # No hay que romper el caso real: "quiero un conjunto de encaje" SI debe
    # reconocer "conjunto" (o su plural "conjuntos", ya reconocido por
    # matching de raíz) como palabra completa, y mapear a lencería.
    intencion, _ = catalog._intencion_desde_texto("quiero un conjunto de encaje negro")
    assert intencion in ("conjunto", "conjuntos"), intencion
    assert catalog._INTENCION_A_CATEGORIA_FUNCIONAL[intencion] == "lenceria"


def test_intenciones_normales_siguen_funcionando():
    casos = {
        "tienen bombas para el pene": "bomba",
        "quiero ver succionadores": "succionador",
        "me gustaria ver si tinen latigos": "latigo",
    }
    for msg, clave_esperada_fragmento in casos.items():
        intencion, _ = catalog._intencion_desde_texto(msg)
        assert intencion is not None, msg
