"""El resolvedor de selección: a qué producto se refiere el cliente.

Sustituye a cuatro heurísticas que vivían dispersas y no se hablaban entre sí
(`_SELECCION_NUMERICA_RE`, `_SELECCION_ORDINAL_RE`, `_pide_comprar_sin_numero` en
main.py, y la Prioridad 0 por índice en pedidos.py).

La clave del diseño es el MUNDO CERRADO: no se resuelve contra el catálogo entero
sino contra las últimas rondas mostradas, un puñado de productos. Eso es lo que
permite bajar el umbral de matching por nombre —"euforia" basta— sin el riesgo de
falso positivo que tendría contra 300 referencias.

`catalog.get_productos_en_texto` NO sirve aquí: exige el nombre completo como
subcadena o ≥70% de cobertura de tokens. Funciona en pedidos.py porque se le pasa
el reply del BOT, que trae el nombre entero. Contra el mensaje del cliente se cae:
"Multiorgasmos Euforia X 30 Ml" tiene los tokens {multiorgasmos, euforia, ml}, y
"quiero el multiorgasmos euforia" cubre 2/3 = 0.67 < 0.70.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.stubs import stub_drivers  # noqa: E402

stub_drivers()  # app.seleccion importa app.catalog, que importa asyncpg

from app import seleccion  # noqa: E402


# Tres rondas, como en el caso que motivó el cambio: el cliente recorre disfraces,
# luego masturbadores, luego juegos, y acaba queriendo uno de varias categorías.
RONDAS = [
    {"categoria": "disfraces", "productos": [
        {"id": 1, "nombre": "Disfraz Policia Sexy Mujer", "precio": 89000},
        {"id": 2, "nombre": "Disfraz Enfermera Rojo", "precio": 75000},
    ]},
    {"categoria": "masturbadores", "productos": [
        {"id": 3, "nombre": "Masturbador Multiorgasmos Euforia X 30 Ml", "precio": 40000},
        {"id": 4, "nombre": "Masturbador Satisfyer Men", "precio": 120000},
    ]},
    {"categoria": "juegos", "productos": [
        {"id": 5, "nombre": "Juego De Dados Eroticos", "precio": 25000},
        {"id": 6, "nombre": "Juego Kamasutra Cartas", "precio": 30000},
    ]},
]


def test_nombre_completo_resuelve():
    r = seleccion.resolver("quiero el Juego De Dados Eroticos", RONDAS)
    assert r.ids == [5], r


def test_nombre_parcial_resuelve():
    """El caso que `get_productos_en_texto` no cubre: el cliente nunca escribe
    el nombre entero de un producto de cinco palabras."""
    r = seleccion.resolver("quiero el multiorgasmos euforia", RONDAS)
    assert r.ids == [3], r


def test_una_sola_palabra_distintiva_basta():
    assert seleccion.resolver("me llevo el euforia", RONDAS).ids == [3]
    assert seleccion.resolver("quiero la enfermera", RONDAS).ids == [2]


def test_productos_de_RONDAS_DISTINTAS_en_un_mismo_mensaje():
    """El objetivo del cambio entero. Antes era imposible: un índice "2" no dice
    de qué lista, y el estado se reseteaba al cambiar de categoría."""
    r = seleccion.resolver("quiero el disfraz de policia y el juego de dados", RONDAS)
    assert sorted(r.ids) == [1, 5], r


def test_una_palabra_compartida_es_ambigua_no_una_apuesta():
    """'el juego' lo comparten dos productos. Resolver uno al azar es peor que
    preguntar: el cliente no se entera de que se eligió por él."""
    r = seleccion.resolver("quiero el juego", RONDAS)
    assert r.ids == []
    assert sorted(p["id"] for p in r.ambiguos) == [5, 6], r
    assert r.hay_intencion_compra


def test_el_numero_se_resuelve_contra_la_ULTIMA_ronda():
    """Aquí muere el bug. El '2' es el segundo de lo último que vio, no el
    segundo del acumulado de las tres rondas (que sería el disfraz de enfermera).
    """
    r = seleccion.resolver("el 2", RONDAS)
    assert r.ids == [6], f"esperaba el 2º de la ronda de juegos, no del acumulado: {r}"


def test_el_ordinal_tambien_se_resuelve_contra_la_ultima_ronda():
    assert seleccion.resolver("quiero el primero", RONDAS).ids == [5]


def test_un_numero_suelto_en_una_frase_normal_no_es_una_seleccion():
    """'tengo 2 hijos', una dirección, una edad. El patrón exige que el mensaje
    sea esencialmente la selección y nada más."""
    for texto in ("tengo 2 hijos", "vivo en la calle 2 con 45",
                  "primero quiero saber si es impermeable", "mi cedula es 2"):
        r = seleccion.resolver(texto, RONDAS)
        assert r.ids == [], f"{texto!r} no debería resolver nada: {r}"


def test_seleccion_por_precio():
    """'el de 40 mil' — el precio no está en el nombre, así que ninguna regla de
    tokens lo cubre."""
    assert seleccion.resolver("quiero el de 40 mil", RONDAS).ids == [3]
    assert seleccion.resolver("me llevo el de 25.000", RONDAS).ids == [5]


def test_intencion_de_compra_sin_objeto():
    """El cliente cierra pero no dice cuál. No resuelve nada, pero la intención
    se señala para poder pedirle el nombre en vez de reenviarle el catálogo."""
    for texto in ("quiero comprar", "como hago para pedir", "quiero hacer un pedido"):
        r = seleccion.resolver(texto, RONDAS)
        assert r.ids == [], texto
        assert r.hay_intencion_compra, texto


def test_una_pregunta_normal_no_es_intencion_de_compra():
    for texto in ("cuanto cuesta el envio", "tienen mas colores", "hola buenas"):
        assert not seleccion.resolver(texto, RONDAS).hay_intencion_compra, texto


def test_sin_rondas_no_resuelve_nada():
    """Sin lista viva no hay nada contra qué resolver, y un número no significa
    nada. Antes esto se inferia buscando keycaps en el texto del historial."""
    r = seleccion.resolver("el 2", [])
    assert r.ids == [] and r.ambiguos == []


def test_los_typos_del_cliente_se_corrigen_antes_de_resolver():
    """"dizfras" no casa con ningún token de "Disfraz Policia Sexy Mujer".

    La ronda es de un solo disfraz a propósito: con dos, "disfraz" no sería
    distintivo y el test pasaría por otro motivo (el token "policia"), sin llegar
    a ejercitar la corrección.
    """
    una_ronda = [{"categoria": "disfraces", "productos": [
        {"id": 1, "nombre": "Disfraz Policia Sexy Mujer", "precio": 89000},
        {"id": 9, "nombre": "Body Encaje Negro", "precio": 55000},
    ]}]
    assert seleccion.resolver("quiero el dizfras", una_ronda).ids == [1]


def test_no_se_repite_un_id_aunque_se_nombre_dos_veces():
    r = seleccion.resolver("el euforia, si, el euforia ese", RONDAS)
    assert r.ids == [3], r
