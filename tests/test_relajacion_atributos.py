"""Con varios atributos, soltar uno antes que rendirse.

Incidente de producción 2026-08-05: el cliente pidió un dildo con ventosa para
primera vez y acabó escalado a un humano, con el bot pausado.

    "Sin productos para {'tipo': 'dildo', 'atributos': ['principiante',
     'ventosa']} (ni relajando)"
    "Handoff por inventario sin coincidencias — bot pausado"

Medido sobre el catálogo real: hay **11 dildos con ventosa**, pero el atributo
`principiante` solo está en **1 de 22** dildos. La intersección da 0.

`_ESCALERA_RELAJACION` no incluye `atributos`, y el último recurso se los salta a
propósito. Ese razonamiento es correcto con UN atributo —"otros dildos" no es una
respuesta parcial a "un dildo doble", es otro producto— y no se toca. Con VARIOS
es otra cosa: soltar uno no es soltarlos todos, y enseñarle los de ventosa
respeta lo que el cliente más marcó.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.stubs import stub_drivers  # noqa: E402

stub_drivers()

sys.path.insert(0, str(_ROOT / "scripts" / "eval"))
import fake_db  # noqa: E402

from app import catalog, db  # noqa: E402


def _buscar(restricciones: dict):
    """Contra el catálogo real del arnés de evaluación."""
    original = getattr(db, "_pool", None)
    fake_db.instalar(db, _ROOT / "scripts" / "eval" / "catalogo.json")
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return asyncio.run(catalog.buscar_por_restricciones(restricciones, limit=5))
    finally:
        db._pool = original


def test_con_dos_atributos_se_suelta_el_que_no_da_resultados():
    """El caso exacto del incidente. Debe mostrar dildos con ventosa reales en
    vez de mandar al cliente con un humano."""
    res = _buscar({"tipo": "dildo", "atributos": ["principiante", "ventosa"]})
    assert res.productos, "debería mostrar los dildos con ventosa"
    assert res.relajado, "y decir qué se cedió, para poder avisar al cliente"
    for p in res.productos:
        assert "ventosa" in (p.get("atributos") or []), p["nombre"]


def test_se_conserva_el_atributo_con_mas_cobertura():
    """De los dos, se suelta el que el catálogo apenas tiene poblado."""
    res = _buscar({"tipo": "dildo", "atributos": ["principiante", "ventosa"]})
    assert res.relajado == "principiante", (
        f"debería soltar el atributo sin cobertura, no el otro: {res.relajado}")


def test_con_un_solo_atributo_se_sigue_sin_rellenar():
    """Lo que el comentario de la escalera defiende, y que no se toca: "otros
    dildos" no es una respuesta parcial a "un dildo doble"."""
    res = _buscar({"tipo": "dildo", "atributos": ["inexistente_xyz"]})
    assert not res.productos, res.productos


def test_si_la_combinacion_exacta_existe_no_se_relaja_nada():
    """La relajación es el último recurso, no el camino normal."""
    res = _buscar({"tipo": "dildo", "atributos": ["ventosa"]})
    assert res.productos
    assert not res.relajado, res.relajado
