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


def test_un_producto_que_es_un_conjunto_no_es_un_juego_de_mesa():
    f = facetas.clasificar_por_reglas(
        "Anillos para Pene Donut Stay Hard Kit x3", "", "Anillos")
    assert f.tipo == "anillo"


def test_juego_no_es_clave_del_lado_del_producto():
    """Medido contra el catálogo de producción: añadir 'juego' a las reglas de
    producto producía 3 regresiones y ninguna mejora.

    El nombre se evalúa en una pasada propia que gana con confianza 1.0 y corta
    antes de mirar la descripción, así que la palabra en el nombre secuestra la
    clasificación aunque la descripción diga lo correcto.
    """
    f = facetas.clasificar_por_reglas(
        "Juego de Kegel con Pesas Intercambiables She-ology",
        "Bolas chinas de ejercicio para el suelo pélvico", "Bolas")
    assert f.tipo == "bolas", "'juego de X' es un conjunto de X, no un juego"


def test_los_juegos_de_verdad_entran_por_su_forma_concreta():
    for nombre, esperado in (("Juego Jenga Erotico", "juego"),
                             ("Juego de Cartas Verdad o Se Atreve", "juego"),
                             ("Wana Dados Juego Erotico", "juego")):
        f = facetas.clasificar_por_reglas(nombre, "", "Juego")
        assert f.tipo == esperado, nombre


def test_un_aceite_con_dados_sigue_siendo_cosmetica():
    """Lo salva el orden: la regla de cosmética va antes que la de juegos."""
    f = facetas.clasificar_por_reglas(
        "Aceite Caliente Saborizado con Dados", "", "Cosmeticos")
    assert f.tipo == "cosmetica"


# ── Tarea 4: la marca curada ──

def test_calexotics_es_una_marca_conocida():
    """Estar en la lista hace es_especifico=True, que es lo que abre el camino
    de búsqueda por nombre incluso con una conversación en curso."""
    assert "calexotics" in catalog._MARCAS_CONOCIDAS


def test_la_marca_se_reconoce_dentro_de_una_frase():
    norm = catalog._normalizar_texto("tienen quizas productos Calexotics")
    assert any(m in norm for m in catalog._MARCAS_CONOCIDAS)


# ── Tarea 5: la marca desconocida en mitad de la conversación ──

CALEXOTICS = [
    {"id": 1, "nombre": "Bala Vibradora Conejito Pixies Calexotics",
     "precio": 50000, "imagen_url": "http://x/1.jpg", "tipo": "vibrador",
     "zona": "clitoris", "atributos": []},
]


def _estado_vibradores():
    return {"categoria_busqueda": "vibradores", "categoria_funcional": "vibradores",
            "genero": "pareja", "calificado": True,
            "productos_mostrados": [11, 12, 13, 14, 15],
            "restricciones": {"tipo": "vibrador"}, "preguntas_hechas": [],
            "texto_busqueda": "tienen vibradores"}


def _sin_catalogo(**extra):
    async def sin_restricciones(restricciones, exclude_ids=None, limit=5,
                                permitir_relajar=True, user_text="", subtipo=None):
        return catalog.Resultado(relajado="sin_resultado", restricciones=restricciones)

    async def contar(_r, **kwargs):
        return 20

    async def sin_facetas(_r):
        return {"atributos": {}, "zonas": {}, "generos": {}}

    base = dict(buscar_por_restricciones=sin_restricciones,
                contar_por_restricciones=contar,
                facetas_disponibles=sin_facetas)
    base.update(extra)
    return parchar(catalog, **base)


def test_la_marca_curada_se_busca_con_conversacion_activa():
    """El turno del 2/08: 'tienen quizas productos Calexotics' llegó con una
    charla de vibradores en curso.

    Lo resuelve estar en `_MARCAS_CONOCIDAS`, que pone `es_especifico=True` y
    abre el camino de [main.py:1050]. El bloque de primer contacto sigue
    gateado a propósito (ver el test siguiente)."""
    buscados = []

    async def fake_especifico(user_text, limit=5, exclude_ids=None, tipo=None):
        buscados.append((user_text, tipo))
        return [dict(CALEXOTICS[0])]

    with _sin_catalogo(buscar_producto_especifico=fake_especifico):
        candidatos, _info = asyncio.run(main._recuperar_candidatos(
            "tienen quizas productos Calexotics", [], _estado_vibradores()))
    assert buscados, "la búsqueda por nombre no llegó a ejecutarse"
    assert [p["id"] for p in candidatos] == [1]


def test_un_color_no_puede_secuestrar_el_listado():
    """Por qué el bloque de primer contacto sigue gateado: 'los rojos' produce
    un token discriminante igual que una marca, y casaría con 'Suspensorio
    Insolent Rojo' por la regla de plurales. Es el bug19."""
    assert catalog._tokens_no_reconocidos("los rojos") == ["rojos"]

    buscados = []

    async def fake_especifico(user_text, limit=5, exclude_ids=None, tipo=None):
        buscados.append(user_text)
        return [dict(CALEXOTICS[0])]

    async def con_productos(restricciones, exclude_ids=None, limit=5,
                            permitir_relajar=True, user_text="", subtipo=None):
        return catalog.Resultado(productos=[dict(CALEXOTICS[0])],
                                 restricciones=restricciones)

    with _sin_catalogo(buscar_producto_especifico=fake_especifico,
                       buscar_por_restricciones=con_productos):
        asyncio.run(main._recuperar_candidatos(
            "los rojos", [], _estado_vibradores()))
    assert buscados == [], f"un color no debe buscarse por nombre: {buscados}"


def test_una_respuesta_sin_marca_no_dispara_busqueda_por_nombre():
    """'sí' o 'el 2' no traen tokens discriminantes: el bloque debe seguir
    quieto para no secuestrar la conversación."""
    buscados = []

    async def fake_especifico(user_text, limit=5, exclude_ids=None, tipo=None):
        buscados.append(user_text)
        return []

    async def con_productos(restricciones, exclude_ids=None, limit=5,
                            permitir_relajar=True, user_text="", subtipo=None):
        return catalog.Resultado(productos=[dict(CALEXOTICS[0])],
                                 restricciones=restricciones)

    with _sin_catalogo(buscar_producto_especifico=fake_especifico,
                       buscar_por_restricciones=con_productos):
        asyncio.run(main._recuperar_candidatos(
            "sí, muéstrame", [], _estado_vibradores()))
    assert buscados == [], f"no debía buscar por nombre: {buscados}"
