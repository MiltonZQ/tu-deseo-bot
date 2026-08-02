"""Preguntar por juegos devuelve juegos; preguntar por una marca, esa marca.

Sesión del 2/08. "tienen quizas productos Calexotics" en mitad de una charla
sobre vibradores devolvió la página siguiente de vibradores genéricos, teniendo
el catálogo al menos tres productos Calexotics. Y los juegos de mesa eróticos
no se muestran nunca: "juegos" no es vocabulario ni del cliente ni del
producto, solo lo son "juego de mesa", "jenga", "cartas", "dados" y "ruleta".
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.stubs import importar_main, stub_drivers  # noqa: E402

stub_drivers()

from app import catalog, facetas  # noqa: E402

main = importar_main()


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


# ── Tarea 3: los juegos ──

def test_el_cliente_puede_pedir_juegos_a_secas():
    """Nadie escribe 'juego de mesa': escribe 'tienen juegos'."""
    for texto in ("tienen juegos sexuales", "tienen juegos", "quiero un juego"):
        r = facetas.interpretar_mensaje(texto)
        assert r.get("tipo") == "juego", f"{texto} → {r}"


def test_juegos_para_parejas_conserva_las_dos_facetas():
    r = facetas.interpretar_mensaje("juegos para parejas")
    assert r.get("tipo") == "juego"
    assert r.get("genero_uso") == "pareja"


def test_un_juego_de_anillos_sigue_siendo_anillos():
    """En castellano 'juego de X' es un CONJUNTO de X. Lo resuelve el orden de
    las reglas: la de juegos es la última y la de anillos va antes."""
    r = facetas.interpretar_mensaje("juego de anillos")
    assert r.get("tipo") == "anillo"


def test_el_producto_llamado_juego_se_clasifica_como_juego():
    f = facetas.clasificar_por_reglas("Juego Sexy Dice", "", "Juegos")
    assert f.tipo == "juego"


def test_un_producto_que_es_un_conjunto_no_es_un_juego_de_mesa():
    f = facetas.clasificar_por_reglas(
        "Anillos para Pene Donut Stay Hard Kit x3", "", "Anillos")
    assert f.tipo == "anillo"
