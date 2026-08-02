"""Después de la lista, el cliente tiene que saber qué contestar.

El CTA decía "¿Cuál te gusta?" y el cliente contestaba "ese", "el dildo" o
"quiero pedir". Ahora pide el número, y un mensaje de compra sin número recibe
la petición del número en vez de otra página de catálogo.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.stubs import importar_main, stub_drivers  # noqa: E402

stub_drivers()

main = importar_main()

PRODS = [{"id": i, "nombre": f"Producto {i}", "precio": 50000} for i in (1, 2, 3)]


# ── Tarea 1: el CTA ──

def test_con_mas_opciones_el_cta_pide_numero_y_ofrece_ver_mas():
    txt = main._texto_desde_candidatos(PRODS, {"intencion": "vibradores", "hay_mas": True})
    assert "indícame el número o los números" in txt, txt
    assert "ver más diseños" in txt, txt


def test_sin_mas_opciones_el_cta_pide_numero_y_no_ofrece_ver_mas():
    txt = main._texto_desde_candidatos(PRODS, {"intencion": "vibradores", "hay_mas": False})
    assert "indícame el número o los números" in txt, txt
    assert "ver más" not in txt.lower(), txt


def test_el_cta_pide_el_numero_tambien_cuando_se_cedio_en_algo():
    """Con `relajado` el aviso sustituye la entrada, pero el CTA no cambia."""
    txt = main._texto_desde_candidatos(PRODS, {
        "intencion": "anal", "relajado": "zona", "hay_mas": False,
        "restricciones": {"tipo": "vibrador", "zona": "anal"}})
    assert "indícame el número o los números" in txt, txt
