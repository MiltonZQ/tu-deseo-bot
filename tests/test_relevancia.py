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
