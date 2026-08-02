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


# ── Tarea 5: cómo se nombra lo que pidió el cliente ──

def test_el_pedido_se_describe_con_el_atributo():
    assert main._describir_pedido(
        {"tipo": "dildo", "atributos": ["doble"]}) == "dildos dobles"
    assert main._describir_pedido(
        {"tipo": "lubricante", "atributos": ["sabor"]}) == "lubricantes con sabor"


def test_el_pedido_se_describe_con_la_zona():
    assert main._describir_pedido(
        {"tipo": "vibrador", "zona": "anal"}) == "vibradores anales"


def test_sin_restricciones_no_se_inventa_nada():
    assert main._describir_pedido({}) == "productos"


def test_el_aviso_de_agotado_nombra_el_atributo():
    """Decir 'te mostré todas las opciones de dildos' teniendo 22 es falso: lo
    que se agotó fueron los dobles."""
    info = {"categoria_agotada": True, "agotado_por_facetas": True,
            "intencion": "dildos", "categoria_funcional": "dildos",
            "restricciones": {"tipo": "dildo", "atributos": ["doble"]}}
    assert "dildos dobles" in main._texto_agotado(info)


def test_el_aviso_de_agotado_sin_facetas_sigue_igual():
    """El camino legacy no tiene restricciones: no debe quedarse sin texto."""
    info = {"categoria_agotada": True, "agotado_por_facetas": False,
            "intencion": "lubricantes-y-cuidado", "restricciones": {}}
    assert "lubricantes y cuidado" in main._texto_agotado(info)


# ── Tarea 3: la escalera de relajación ──

DILDOS = [
    {"id": 20, "nombre": "Dildo Doble Niel 38 cm", "descripcion": ""},
    {"id": 21, "nombre": "Dildo Realista Daian 17 cm", "descripcion": ""},
    {"id": 22, "nombre": "Raw Dildo Realista Denzel 19 cm", "descripcion": ""},
]


def test_no_se_rellena_soltando_el_atributo_que_pidio_el_cliente():
    """El turno 3 del 2/08: 'Restricción relajada: atributos (quedan
    {tipo: dildo}) → 5 productos'. El cliente pidió dobles y recibió
    realistas."""
    consultas = []

    async def fake_consultar(restricciones, exclude_ids, limit, user_text=""):
        consultas.append(dict(restricciones))
        return [] if restricciones.get("atributos") else [dict(DILDOS[1])]

    with parchar(catalog, _consultar_restricciones=fake_consultar):
        res = asyncio.run(catalog.buscar_por_restricciones(
            {"tipo": "dildo", "atributos": ["doble"]}, limit=5,
            user_text="tienen mas dobles?"))
    assert res.productos == []
    assert res.relajado == "sin_resultado"
    assert all(c.get("atributos") for c in consultas), \
        f"nunca debe consultarse sin el atributo pedido: {consultas}"


def test_un_solo_producto_se_muestra_solo_el():
    """El principio, literal: si solo hay uno de lo que pidió, se muestra ese."""
    async def fake_consultar(restricciones, exclude_ids, limit, user_text=""):
        return [dict(DILDOS[0])]

    with parchar(catalog, _consultar_restricciones=fake_consultar):
        res = asyncio.run(catalog.buscar_por_restricciones(
            {"tipo": "dildo", "atributos": ["doble"]}, limit=5,
            user_text="doble"))
    assert [p["id"] for p in res.productos] == [20]
    assert res.relajado is None


def test_sin_atributos_la_escalera_sigue_cediendo():
    """La relajación existe por una razón: un vibrador 'con control remoto' que
    no existe debe poder devolver vibradores, avisando."""
    async def fake_consultar(restricciones, exclude_ids, limit, user_text=""):
        if restricciones.get("control"):
            return []
        return [dict(DILDOS[1])]

    with parchar(catalog, _consultar_restricciones=fake_consultar):
        res = asyncio.run(catalog.buscar_por_restricciones(
            {"tipo": "vibrador", "control": "remoto"}, limit=5,
            user_text="vibrador con control remoto"))
    assert res.relajado == "control"
    assert res.productos
