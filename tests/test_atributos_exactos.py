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
        {"id": 1, "nombre": "Dildo Ultra Realista Burgo Camtoyz",
         "descripcion": "", "categoria": "Dildo"},
        {"id": 2, "nombre": "Consolador King Cock con Squirting",
         "descripcion": "Textura piel, acabado realista", "categoria": "Dildo"},
    ]
    res = clasificacion.auditar_filas(filas)
    assert res["productos"] == 2
    assert res["atributos"]["realista"]["por_nombre"] == 1
    assert res["atributos"]["realista"]["solo_descripcion"] == 1
    assert res["atributos"]["realista"]["ejemplos"] == ["Consolador King Cock con Squirting"]


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

    async def fake_consultar(restricciones, exclude_ids, limit, user_text="", subtipo=None):
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
    async def fake_consultar(restricciones, exclude_ids, limit, user_text="", subtipo=None):
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
    async def fake_consultar(restricciones, exclude_ids, limit, user_text="", subtipo=None):
        if restricciones.get("control"):
            return []
        return [dict(DILDOS[1])]

    with parchar(catalog, _consultar_restricciones=fake_consultar):
        res = asyncio.run(catalog.buscar_por_restricciones(
            {"tipo": "vibrador", "control": "remoto"}, limit=5,
            user_text="vibrador con control remoto"))
    assert res.relajado == "control"
    assert res.productos


# ── Tarea 2: los atributos no se pegan a productos que no son ──

def test_doble_densidad_no_convierte_un_dildo_en_doble():
    """El falso positivo del 2/08: cuatro ultrarrealistas marcados como dobles
    porque su ficha dice 'silicona de doble densidad'."""
    f = facetas.clasificar_por_reglas(
        "Dildo Ultra Realista Burgo Camtoyz",
        "Fabricado en silicona de doble densidad, tacto piel", "Dildo")
    assert "doble" not in f.atributos


def test_un_dildo_doble_de_verdad_sigue_marcandose():
    f = facetas.clasificar_por_reglas("Dildo Doble Niel 38 cm", "", "Dildo")
    assert "doble" in f.atributos


def test_los_dobles_que_lo_dicen_en_ingles_no_se_pierden():
    """La auditoría del catálogo: tres dobles reales no usan la palabra
    española. Sin la clave inglesa, acotar al nombre los habría borrado."""
    for nombre in ("Satisfyer Double Joy Vibrador Negro",
                   "Satisfyer Double Classic Partner Vibrador Morado"):
        f = facetas.clasificar_por_reglas(nombre, "", "Vibrador")
        assert "doble" in f.atributos, nombre


def test_doble_sigue_siendo_vocabulario_publico():
    """El panel valida contra F.ATRIBUTOS: si `doble` desaparece de ahí, deja
    de poder corregirse a mano."""
    assert "doble" in facetas.ATRIBUTOS


def test_un_arnes_no_es_un_lubricante_a_base_de_agua():
    """Auditoría: 12 de 14 productos con `agua` lo debían a la descripción, y
    entre ellos un arnés y una funda cuya ficha dice 'usar lubricante a base
    de agua'."""
    f = facetas.clasificar_por_reglas(
        "Arnes Tanos Camtoyz",
        "Se recomienda usar lubricante a base de agua", "Arneses")
    assert "agua" not in f.atributos


def test_un_dildo_no_tiene_efecto_calor():
    f = facetas.clasificar_por_reglas(
        "Dildo Ultrarealista Grigor CamToyz",
        "Lavar con agua caliente y jabón neutro", "Dildo")
    assert "calor" not in f.atributos
    assert "neutro" not in f.atributos


def test_el_lubricante_conserva_sus_propios_atributos():
    """La poda es por tipo: en un lubricante esos atributos son su razón de ser."""
    f = facetas.clasificar_por_reglas(
        "Lubricante Electrizante Mango X 30 ML",
        "Lubricante con sabor a mango, base de agua", "Lubricantes")
    assert "sabor" in f.atributos
    assert "agua" in f.atributos


def test_pequeno_no_significa_para_principiantes():
    """Auditoría: por la clave 'pequeño' entraban un baby doll y un arnés."""
    f = facetas.clasificar_por_reglas(
        "Baby Doll Bluma Lerot", "Prenda de encaje con detalle pequeño", "Lencería")
    assert "principiante" not in f.atributos


def test_lo_que_de_verdad_es_para_principiantes_se_mantiene():
    f = facetas.clasificar_por_reglas(
        "Plug Anal Mikel CamToyz", "Ideal para principiantes", "Plug")
    assert "principiante" in f.atributos


# ── Tarea 4: cero coincidencias ──

def _estado_dildos(mostrados):
    return {"categoria_busqueda": "dildos", "categoria_funcional": "dildos",
            "genero": None, "calificado": True, "productos_mostrados": mostrados,
            "restricciones": {"tipo": "dildo", "atributos": ["doble"]},
            "preguntas_hechas": [], "texto_busqueda": "doble"}


def _catalogo_vacio(total: int):
    """Ninguna búsqueda devuelve nada; `total` es cuántos existen en catálogo."""
    async def sin_productos(*a, **k):
        return []

    async def sin_restricciones(restricciones, exclude_ids=None, limit=5,
                                permitir_relajar=True, user_text=""):
        return catalog.Resultado(relajado="sin_resultado", restricciones=restricciones)

    async def contar(_r):
        return total

    async def sin_facetas(_r):
        return {"atributos": {}, "zonas": {}, "generos": {}}

    return parchar(catalog, buscar_por_restricciones=sin_restricciones,
                   contar_por_restricciones=contar,
                   facetas_disponibles=sin_facetas,
                   buscar_producto_especifico=sin_productos,
                   get_productos_para_recomendar=sin_productos)


def test_sin_ningun_producto_del_pedido_se_escala():
    """El cliente pide algo que no existe: nunca 'no hay', se escala."""
    with _catalogo_vacio(total=0):
        _c, info = asyncio.run(main._recuperar_candidatos(
            "tienen dildos dobles", [], _estado_dildos([])))
    assert info["sin_inventario"] is True
    assert info["agotado_por_facetas"] is False


def test_si_ya_los_vio_todos_no_se_escala():
    """Existe 1 y el cliente ya lo vio: eso no es falta de inventario, es que se
    acabó la lista. Escalar aquí llenaría el panel con cada 'ver más'."""
    with _catalogo_vacio(total=1):
        _c, info = asyncio.run(main._recuperar_candidatos(
            "tienen mas dobles?", [], _estado_dildos([20])))
    assert info["sin_inventario"] is False
    assert info["agotado_por_facetas"] is True


def test_no_se_rellena_con_la_categoria_legacy():
    """El corte tiene que apagar los cinco fallbacks: si alguno sigue vivo,
    vuelven los dildos genéricos."""
    llamadas = []

    async def recomendar(*a, **k):
        llamadas.append(k or a)
        return [dict(DILDOS[1])]

    with _catalogo_vacio(total=0):
        with parchar(catalog, get_productos_para_recomendar=recomendar):
            candidatos, _info = asyncio.run(main._recuperar_candidatos(
                "tienen dildos dobles", [], _estado_dildos([])))
    assert candidatos == [], f"se rellenó con {candidatos}"
    assert llamadas == [], f"no debe consultarse el camino legacy: {llamadas}"


def test_la_escalation_se_registra_sin_depender_de_frases():
    """`detect_reason` mira patrones en la respuesta del bot, y el handoff por
    falta de stock no casa con ninguno: hoy pausa sin registrar."""
    creadas = []

    async def sin_pendiente(_wa):
        return None

    async def crear(data):
        creadas.append(data)
        return 7

    from app import db as _db
    with parchar(_db, find_pending_escalation=sin_pendiente, create_escalation=crear):
        eid = asyncio.run(escalations.registrar(
            wa_id="573001112233", reason="sin_inventario",
            reason_detail="El cliente pidió dildos dobles y no hay ninguno.",
            issue_summary="tienen dildos dobles", history=[],
            bot_reply="Déjame validar con el equipo 🙌"))
    assert eid == 7
    assert creadas[0]["reason"] == "sin_inventario"
    assert creadas[0]["wa_id"] == "573001112233"


def test_dual_a_secas_no_significa_doble_penetracion():
    """Medido en el catálogo: en las bolas Kegel 'dual' son dos motores."""
    for nombre in ("Bolas Vaginales Kegel Dual Recargable - Rosa",
                   "Bolas Vaginales Remote Dual Motor Kegel System"):
        f = facetas.clasificar_por_reglas(nombre, "", "Bolas")
        assert "doble" not in f.atributos, nombre


def test_la_frase_completa_dual_penetrator_si_cuenta():
    f = facetas.clasificar_por_reglas(
        "Consolador con Anillos Accommodator Dual Penetrator Marfil", "", "Anillo")
    assert "doble" in f.atributos


def test_un_anillo_de_doble_penetracion_conserva_el_atributo():
    """Perdía el atributo solo porque `anillo` no estaba entre los tipos."""
    f = facetas.clasificar_por_reglas(
        "Anillo con Vibración Doble Penetración Diver Calexotics", "", "Anillo")
    assert "doble" in f.atributos
