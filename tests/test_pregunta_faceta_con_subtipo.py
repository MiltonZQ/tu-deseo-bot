"""Regresion del 2026-08-03 (prueba real por WhatsApp):

    Cliente: "Me gustaría un disfraz de colegiala"
    Bot:     "¡Claro que sí! Cuéntame, ¿la buscas para ella o para él? 😊"

El cliente ya dijo EXACTAMENTE que quiere; preguntarle "¿para ella o para él?"
es pedirle que repita lo que acaba de decir.

Causa raiz: `peticion_amplia` (app/main.py) decide preguntar mirando solo las
facetas de app/facetas.py, cuyo vocabulario no conoce "colegiala" —
`facetas.interpretar_mensaje` devuelve {"tipo": "lenceria"} sin ninguna faceta
discriminante— asi que arma la pregunta de app/preguntas.py. Nunca miraba
`subtipo_detectado`, que el clasificador SI resuelve ("colegiala").

Y como `pregunta_faceta` tiene prioridad sobre el turno que muestra productos,
la pregunta cortocircuita el pipeline entero: la recuperacion nunca corre.

El arreglo NO toca el filtro duro de atributos (que exigiria reclasificar el
catalogo y, sin eso, daria 0 filas -> handoff). Solo deja de preguntar cuando
el cliente ya nombro un subtipo concreto; el orden por `_score_candidato` ya
pone delante lo que pidio.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.stubs import importar_main, stub_drivers  # noqa: E402

stub_drivers()
main = importar_main()

from app import catalog, db, facetas, openai_client, preguntas  # noqa: E402


async def _sin_llm(_texto, _history=None):
    return None


async def _sin_reorden(_texto, _candidatos):
    return None


def _prod(pid, nombre, genero_uso, atributos=()):
    return {
        "id": pid, "nombre": nombre, "descripcion": "lenceria sensual",
        "categoria": "Lenceria", "precio": 109800,
        "imagen_url": f"http://x/{pid}.jpg", "galeria_urls": None, "permalink": None,
        "tipo": "lenceria", "zona": "ninguna", "vibra": False, "control": "ninguno",
        "genero_uso": genero_uso, "atributos": list(atributos), "activo": True,
        "stock_status": "instock",
    }


# Dos condiciones para reproducir el bug:
#   - dos ramas vivas (mujer y hombre), o `preguntas.construir` no arma nada;
#   - MAS de UMBRAL_PREGUNTA_CLARIFICACION (8) productos ofrecibles, o
#     `vale_la_pena` es False y no se pregunta por otro motivo.
_CATALOGO = [
    _prod(1, "Disfraz Colegiala Inocente Lerot", "mujer"),
    _prod(2, "Disfraz Colegiala Negro Dulce Tentacion", "mujer"),
    _prod(3, "Disfraz Enfermera Sexy Dulce Tentacion", "mujer"),
    _prod(4, "Disfraz Policia Lerot", "mujer"),
    _prod(5, "Disfraz Mucama Lerot", "mujer"),
    _prod(6, "Disfraz Coneja Juguetona Lerot", "mujer"),
    _prod(7, "Body Encaje Negro Transparente", "mujer"),
    _prod(8, "Baby Doll Rojo Satinado", "mujer"),
    _prod(9, "Conjunto Encaje Dos Piezas", "mujer"),
    _prod(10, "Suspensorio Masculino Negro", "hombre"),
    _prod(11, "Pechera Masculina Arnes", "hombre"),
    _prod(12, "Conjunto Masculino Malla", "hombre"),
]


class _FakeConn:
    async def fetch(self, *_a, **_k):
        return [dict(p) for p in _CATALOGO]

    async def fetchrow(self, *_a, **_k):
        return dict(_CATALOGO[0])

    async def fetchval(self, *_a, **_k):
        return len(_CATALOGO)


class _FakePool:
    def acquire(self):
        class _Ctx:
            async def __aenter__(self):
                return _FakeConn()

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


def _recuperar(user_text, history=None, estado=None):
    """Ejecuta el pipeline real contra un catálogo falso.

    Los dos parches al cliente de OpenAI se restauran siempre: `tests/run.py`
    importa todos los módulos en el MISMO proceso, así que dejarlos puestos
    rompía los tests de `reordenar_candidatos_por_relevancia`.
    """
    original, db._pool = getattr(db, "_pool", None), _FakePool()
    qdrant, catalog.config.QDRANT_ENABLED = catalog.config.QDRANT_ENABLED, False
    clasif_llm = openai_client.clasificar_intencion_llm
    reorden = openai_client.reordenar_candidatos_por_relevancia
    openai_client.clasificar_intencion_llm = _sin_llm
    openai_client.reordenar_candidatos_por_relevancia = _sin_reorden
    try:
        return asyncio.run(main._recuperar_candidatos(user_text, history or [], estado))
    finally:
        db._pool = original
        catalog.config.QDRANT_ENABLED = qdrant
        openai_client.clasificar_intencion_llm = clasif_llm
        openai_client.reordenar_candidatos_por_relevancia = reorden


# ── Lo que hace falta para que el bug exista (si esto cambia, el test de abajo
#    podria pasar por el motivo equivocado) ──

def test_facetas_no_conoce_colegiala():
    """El vocabulario de facetas no distingue el disfraz: de ahi la pregunta."""
    r = facetas.interpretar_mensaje("Me gustaría un disfraz de colegiala")
    assert r.get("tipo") == "lenceria"
    assert not any(r.get(c) for c in main._FACETAS_DISCRIMINANTES)


def test_el_clasificador_si_resuelve_el_subtipo():
    """El dato que faltaba mirar ya estaba calculado."""
    clasif = asyncio.run(
        catalog.clasificar_intencion_cliente("Me gustaría un disfraz de colegiala", [], None))
    assert clasif.get("subtipo_detectado") == "colegiala"


def test_la_pregunta_del_reporte_existe_tal_cual():
    """El texto exacto que recibio el cliente sale de preguntas.construir."""
    disponibles = {"atributos": {}, "zonas": {}, "generos": {"mujer": 4, "hombre": 2}}
    assert preguntas.construir("lenceria", disponibles) == (
        "¡Claro que sí! Cuéntame, ¿la buscas para *ella* o para *él*? 😊")


# ── La regresion ──

def test_no_pregunta_genero_si_el_cliente_ya_nombro_el_disfraz():
    _, info = _recuperar("Me gustaría un disfraz de colegiala")
    assert info["pregunta_faceta"] is None, info["pregunta_faceta"]


def test_muestra_los_disfraces_de_colegiala_primero():
    candidatos, info = _recuperar("Me gustaría un disfraz de colegiala")
    assert info["debe_mostrar"], "tiene que mostrar productos, no preguntar"
    assert candidatos, "no puede quedarse sin candidatos"
    assert "colegiala" in candidatos[0]["nombre"].lower(), [p["nombre"] for p in candidatos]


def test_una_peticion_amplia_de_verdad_sigue_preguntando():
    """El arreglo no puede cargarse la pregunta cuando SI es util: 'lencería'
    a secas no dice si es para ella o para el."""
    _, info = _recuperar("quiero ver lencería")
    assert info["pregunta_faceta"], "sin subtipo la pregunta sigue valiendo la pena"
