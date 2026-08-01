"""Tests de robustez del pipeline de clasificación/recuperación de productos.

A diferencia de test_calificacion_categorias.py (que evita importar app.catalog
para no depender de asyncpg), estos tests SÍ importan app.catalog de verdad y
llaman la función async real `clasificar_intencion_cliente`. Requieren
`asyncpg` instalado (ya es dependencia de requirements.txt) pero NO requieren
una base de datos corriendo: nada aquí toca la conexión, solo el camino
determinístico (sin respaldo LLM) de la clasificación.

Cubren dos pedidos explícitos del negocio:
  1. Cuando hace falta calificar (categorías amplias: dildos, lencería,
     vibradores, anal, lubricantes-y-cuidado, pareja-y-bondage), después de la
     respuesta del cliente el sistema debe clasificar EXACTAMENTE a la
     categoría/subtipo correspondiente — probado para todas las categorías
     amplias, no solo el bug puntual que se reportó.
  2. Si el cliente llega preguntando por una marca/modelo específico (no
     necesariamente en la lista blanca curada), debe poder encontrarse por
     nombre sin depender de mantener esa lista al día.
"""
from __future__ import annotations

import asyncio

from app import catalog


def _clasificar(user_text: str, history: list[dict] | None = None) -> dict:
    return asyncio.run(catalog.clasificar_intencion_cliente(user_text, history))


# ── Calificación de 2 pasos: categoría amplia → respuesta de subtipo ────────

def test_calificacion_dildos_realista():
    history = [
        {"role": "user", "content": "tienen dildos"},
        {"role": "assistant", "content": (
            "¿buscas un dildo realista (textura piel), con ventosa (para "
            "superficie), de vidrio/cristal, o doble?")},
    ]
    r = _clasificar("realista", history)
    assert r["categoria_funcional"] == "dildos"
    assert r["subtipo_detectado"] == "realista"


def test_calificacion_dildos_vidrio():
    history = [
        {"role": "user", "content": "tienen dildos"},
        {"role": "assistant", "content": (
            "¿buscas un dildo realista, con ventosa, de vidrio/cristal, o doble?")},
    ]
    r = _clasificar("vidrio", history)
    assert r["categoria_funcional"] == "dildos"
    assert r["subtipo_detectado"] == "vidrio"


def test_calificacion_lenceria_body():
    history = [
        {"role": "user", "content": "tienen lenceria"},
        {"role": "assistant", "content": (
            "¿buscas un body, un disfraz, un conjunto, o un suspensorio?")},
    ]
    r = _clasificar("body", history)
    assert r["categoria_funcional"] == "lenceria"
    assert r["subtipo_detectado"] == "body"


def test_calificacion_vibradores_punto_g():
    history = [
        {"role": "user", "content": "quiero un vibrador"},
        {"role": "assistant", "content": (
            "¿lo buscas tipo rabbit, con estimulación de punto G, con app, o control remoto?")},
    ]
    r = _clasificar("punto g", history)
    assert r["categoria_funcional"] == "vibradores"
    assert r["subtipo_detectado"] == "punto g"


def test_calificacion_anal_prostata():
    history = [
        {"role": "user", "content": "tienen juguetes anales"},
        {"role": "assistant", "content": (
            "¿buscas un plug, bolas anales, un estimulador de próstata, o una ducha anal?")},
    ]
    r = _clasificar("prostata", history)
    assert r["categoria_funcional"] == "anal"
    assert r["subtipo_detectado"] == "prostat"


def test_calificacion_anal_ducha():
    history = [
        {"role": "user", "content": "tienen juguetes anales"},
        {"role": "assistant", "content": (
            "¿buscas un plug, bolas anales, un estimulador de próstata, o una ducha anal?")},
    ]
    r = _clasificar("ducha", history)
    assert r["categoria_funcional"] == "anal"
    assert r["subtipo_detectado"] == "ducha"


def test_calificacion_lubricantes_silicona():
    history = [
        {"role": "user", "content": "tienen lubricantes"},
        {"role": "assistant", "content": (
            "¿lo buscas a base de agua, de silicona, anal desensibilizante, o con sabores?")},
    ]
    r = _clasificar("silicona", history)
    assert r["categoria_funcional"] == "lubricantes-y-cuidado"
    assert r["subtipo_detectado"] == "silicona"


def test_calificacion_lubricantes_sabores():
    history = [
        {"role": "user", "content": "tienen lubricantes"},
        {"role": "assistant", "content": (
            "¿lo buscas a base de agua, de silicona, anal desensibilizante, o con sabores?")},
    ]
    r = _clasificar("sabores", history)
    assert r["categoria_funcional"] == "lubricantes-y-cuidado"
    assert r["subtipo_detectado"] == "sabores"


def test_calificacion_lubricantes_anal_no_salta_a_juguetes():
    """Caso reportado del bug: 'anal' tras preguntar subtipo de lubricante NO
    debe saltar a la categoría de juguetes anales."""
    history = [
        {"role": "user", "content": "Quenlubricantes tienen"},
        {"role": "assistant", "content": (
            "¡Genial! Para recomendarte el ideal, cuéntame: ¿lo buscas a base de "
            "agua (seguro con juguetes), de silicona (duradero), anal "
            "desensibilizante, o con sabores/sensaciones (calor/frío)? 😊")},
    ]
    r = _clasificar("anal", history)
    assert r["categoria_funcional"] == "lubricantes-y-cuidado"
    assert r["subtipo_detectado"] == "desensibiliz"


def test_calificacion_bondage_esposas():
    history = [
        {"role": "user", "content": "tienen kits de bondage"},
        {"role": "assistant", "content": (
            "¿buscas esposas, antifaz, fustas, o vendas?")},
    ]
    r = _clasificar("esposas", history)
    assert r["categoria_funcional"] == "pareja-y-bondage"
    assert r["subtipo_detectado"] == "esposas"


# ── Categorías puntuales: sin pregunta de calificación, directo a mostrar ───

def test_puntual_succionadores_califica_directo():
    r = _clasificar("tienen succionadores")
    assert r["categoria_funcional"] == "succionadores"
    assert r["calificado"] is True


def test_puntual_masturbadores_califica_directo():
    r = _clasificar("tienen masturbadores")
    assert r["categoria_funcional"] == "masturbadores"
    assert r["calificado"] is True


# ── Búsqueda por marca/modelo específico SIN lista blanca ───────────────────

def test_marca_no_listada_king_cock_se_detecta_como_no_reconocida():
    """'King Cock' no está en _MARCAS_CONOCIDAS pero debe detectarse como
    término no reconocido (candidato a nombre de marca/modelo)."""
    tokens = catalog._tokens_no_reconocidos("tienen el King Cock de 7 pulgadas")
    assert "king" in tokens
    assert "cock" in tokens


def test_marca_no_listada_ayami_se_detecta():
    tokens = catalog._tokens_no_reconocidos("el dildo AYAMI 17CM CAMTOYZ")
    assert "ayami" in tokens
    assert "camtoyz" in tokens


def test_marca_conocida_lovense_no_es_leftover_pero_sigue_es_especifico():
    """Las marcas de la lista blanca original (_MARCAS_CONOCIDAS) siguen
    marcando es_especifico=True vía ese camino, independientemente del nuevo
    detector de tokens no reconocidos."""
    r = _clasificar("tienen el Lovense Lush")
    assert r["es_especifico"] is True


def test_categoria_generica_no_genera_falsos_positivos_de_marca():
    """Mensajes de categoría genérica (sin marca) no deben dejar tokens
    'no reconocidos' — evita búsquedas por nombre innecesarias."""
    for texto in ("quiero un dildo realista", "tienen lubricantes",
                  "un vibrador para ella", "lenceria para mi novia"):
        tokens = catalog._tokens_no_reconocidos(texto)
        assert tokens == [], f"{texto!r} no debería generar tokens no reconocidos: {tokens}"


def test_palabras_genericas_de_relleno_no_cuentan_como_marca():
    """'algo', 'ese producto', etc. no deben dispararse como posible marca."""
    for texto in ("tienen algo para probar", "ese producto se ve bien",
                  "cual opcion me recomiendas"):
        tokens = catalog._tokens_no_reconocidos(texto)
        assert tokens == [], f"{texto!r} no debería generar tokens no reconocidos: {tokens}"
