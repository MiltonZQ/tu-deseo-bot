"""El carrito: la selección se REGISTRA cuando ocurre, no se adivina al final.

Antes, lo que el cliente había elegido no se guardaba en ningún sitio. Se
reconstruía al cerrar la venta releyendo el texto del historial con
`pedidos.py::_resolver_productos_y_total`. Eso funciona mientras la conversación
sea una sola lista y una sola elección; con tres categorías por medio, el
historial trae tres listas y las elecciones dispersas entre turnos.

Estos tests cubren la escritura: qué entra al carrito y cuándo.
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

from app import seleccion  # noqa: E402

main = importar_main()


_CATALOGO = {
    1: {"id": 1, "nombre": "Disfraz Policia Sexy Mujer", "precio": 89000},
    2: {"id": 2, "nombre": "Disfraz Enfermera Rojo", "precio": 75000},
    5: {"id": 5, "nombre": "Juego De Dados Eroticos", "precio": 25000},
    6: {"id": 6, "nombre": "Juego Kamasutra Cartas", "precio": 30000},
    7: {"id": 7, "nombre": "Dildo Uno Realista", "precio": 65000},
}

_ESTADO_TRES_RONDAS = {
    "rondas": [
        {"categoria": "disfraces", "ids": [1, 2]},
        {"categoria": "juegos", "ids": [5, 6]},
    ]
}


def _resolver(user_text, estado=None, respuesta_llm=None):
    """Ejecuta `_resolver_seleccion` con el catálogo y el LLM fingidos."""
    async def _get_producto(pid):
        return _CATALOGO.get(pid)

    async def _llm(_texto, _productos):
        return respuesta_llm

    orig_cat = main.catalog.get_producto_by_id
    orig_llm = main.openai_client.seleccionar_producto_llm
    main.catalog.get_producto_by_id = _get_producto
    main.openai_client.seleccionar_producto_llm = _llm
    try:
        return asyncio.run(main._resolver_seleccion(
            user_text, _ESTADO_TRES_RONDAS if estado is None else estado))
    finally:
        main.catalog.get_producto_by_id = orig_cat
        main.openai_client.seleccionar_producto_llm = orig_llm


# ── agregar_al_carrito ──

def test_el_carrito_congela_nombre_y_precio():
    """Si WooCommerce resincroniza a mitad de conversación, al cliente se le
    cobra lo que se le dijo."""
    carrito = seleccion.agregar_al_carrito([], [_CATALOGO[1]])
    assert carrito == [{"producto_id": 1, "nombre": "Disfraz Policia Sexy Mujer",
                        "precio": 89000}]


def test_el_carrito_acumula_entre_turnos():
    """El caso multi-categoría: el disfraz entra en un turno, el juego en otro."""
    carrito = seleccion.agregar_al_carrito([], [_CATALOGO[1]])
    carrito = seleccion.agregar_al_carrito(carrito, [_CATALOGO[5]])
    assert [i["producto_id"] for i in carrito] == [1, 5]


def test_confirmar_dos_veces_no_duplica():
    """'sí, el de dados' dicho dos veces es confirmación, no dos artículos."""
    carrito = seleccion.agregar_al_carrito([], [_CATALOGO[5]])
    carrito = seleccion.agregar_al_carrito(carrito, [_CATALOGO[5]])
    assert len(carrito) == 1


# ── _resolver_seleccion ──

def test_resuelve_productos_de_rondas_distintas():
    r = _resolver("quiero el disfraz de policia y el juego de dados")
    assert sorted(r.ids) == [1, 5], r


def test_el_numero_apunta_a_la_ultima_ronda_no_al_acumulado():
    """Con dos rondas de dos productos, "el 2" es el segundo de juegos (id 6).
    Con el índice sobre el acumulado habría sido el disfraz de enfermera."""
    assert _resolver("el 2").ids == [6]


def test_sin_rondas_no_resuelve_nada():
    assert _resolver("el 2", estado={}).ids == []


def test_el_llm_entra_solo_si_las_reglas_no_resolvieron():
    """Si la cascada determinística acierta, no se gasta una llamada — y el
    resultado del LLM no puede pisarla."""
    r = _resolver("quiero el juego de dados", respuesta_llm=[6])
    assert r.ids == [5], f"las reglas resolvieron el 5; el LLM no debe pisarlo: {r}"


def test_el_llm_rescata_una_referencia_por_atributo():
    """"el rojo" no es un token distintivo aquí porque va contra el nombre; el
    LLM sí puede resolverlo mirando el conjunto mostrado."""
    r = _resolver("quiero el de color rojo intenso", respuesta_llm=[2])
    assert r.ids == [2], r


def test_si_el_llm_no_resuelve_se_queda_sin_ids():
    """Y el llamador pregunta. Nunca se elige por el cliente a ciegas."""
    r = _resolver("quiero comprar algo bonito", respuesta_llm=None)
    assert r.ids == []
    assert r.hay_intencion_compra


def test_refinar_una_busqueda_no_es_elegir():
    """"quiero un dildo realista" nombra el TIPO y un calificador del tipo:
    describe una clase de producto, no señala uno.

    Sin esta guarda, "realista" casa con "Dildo Uno Realista" en pantalla y una
    búsqueda legítima se convierte en selección — el bot deja de mostrar
    productos justo cuando el cliente estaba afinando qué quiere ver.
    """
    estado = {"rondas": [{"categoria": "dildos", "ids": [7]}]}
    r = _resolver("quiero un dildo realista", estado=estado)
    assert r.ids == [] and r.ambiguos == [], r


def test_sin_el_tipo_el_mismo_atributo_si_elige():
    """"el realista" nombra una cosa sola: señala, no describe."""
    estado = {"rondas": [{"categoria": "dildos", "ids": [7]}]}
    assert _resolver("quiero el realista", estado=estado).ids == [7]


def test_señalar_uno_concreto_si_es_elegir():
    """La contraparte: nombrar UNA cosa sola señala, no describe una clase."""
    estado = {"rondas": [{"categoria": "disfraces", "ids": [1, 2]}]}
    assert _resolver("quiero el disfraz de policia", estado=estado).ids == [1]


def test_la_ambiguedad_no_se_resuelve_al_azar():
    r = _resolver("quiero el juego", respuesta_llm=None)
    assert r.ids == []
    assert sorted(p["id"] for p in r.ambiguos) == [5, 6], r
