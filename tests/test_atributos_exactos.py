"""El bot muestra lo que el cliente pidió, y solo eso.

Sesión del 2/08: el cliente pidió dildos DOBLES y recibió diez productos, de
los cuales uno era doble. Dos causas independientes, ambas verificadas en los
logs de producción:

  - `Restricciones {'tipo': 'dildo', 'atributos': ['doble']} → 5 productos`,
    sin relajar. Los atributos se detectan sobre nombre + descripción, y
    "doble densidad" —la frase de todo dildo ultrarrealista— marcaba el
    producto como doble.
  - `Restricción relajada: atributos (quedan {'tipo': 'dildo'}) → 5 productos`.
    La escalera soltaba justo lo que el cliente había pedido.
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

from app import catalog, clasificacion, escalations, facetas  # noqa: E402

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


# ── Tarea 1: la auditoría ──

def test_la_auditoria_separa_el_nombre_de_la_descripcion():
    """Lo que hay que poder ver: cuántos productos deben un atributo solo a la
    descripción, que es donde vive el ruido comercial."""
    filas = [
        {"id": 1, "nombre": "Dildo Doble Niel 38 cm",
         "descripcion": "", "categoria": "Dildo"},
        {"id": 2, "nombre": "Dildo Ultra Realista Burgo Camtoyz",
         "descripcion": "Silicona de doble densidad, tacto piel",
         "categoria": "Dildo"},
    ]
    res = clasificacion.auditar_filas(filas)
    assert res["productos"] == 2
    assert res["atributos"]["doble"]["por_nombre"] == 1
    assert res["atributos"]["doble"]["solo_descripcion"] == 1
    assert res["atributos"]["doble"]["ejemplos"] == ["Dildo Ultra Realista Burgo Camtoyz"]


def test_la_auditoria_no_cuenta_atributos_que_nadie_tiene():
    filas = [{"id": 1, "nombre": "Anillo Vibrador Simple",
              "descripcion": "", "categoria": "Anillo"}]
    res = clasificacion.auditar_filas(filas)
    assert "sabor" not in res["atributos"]
