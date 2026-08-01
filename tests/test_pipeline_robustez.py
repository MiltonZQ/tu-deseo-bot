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


# ── Corrección de typos contra los nombres reales del catálogo ──────────────
# Caso reportado: "multiorgarmo" (typo de multiorgasmo) no matcheaba nada, el
# turno se quedaba sin candidatos y el LLM se inventó 5 productos con precios.
# Estos tests inyectan el cache de palabras del catálogo para no necesitar DB.

def _con_cache_catalogo(palabras: set[str]):
    """Precarga el cache de palabras del catálogo para evitar tocar la DB."""
    import time
    catalog._PALABRAS_CATALOGO_CACHE = frozenset(palabras)
    catalog._PALABRAS_CATALOGO_TS = time.monotonic()


# Palabras tomadas de nombres reales del catálogo semilla (prompts/knowledge/catalogo.md).
_PALABRAS_REALES = {
    "multiorgasmos", "multiorgasmo", "electrizante", "euforia", "original",
    "intimo", "consolador", "king", "cock", "prepucio", "squirting",
    "lovense", "satisfyer", "icicles", "elixir", "limpiador", "juguetes",
    "vibrador", "dildo", "realista", "camtoyz", "ayami",
}


def test_typo_multiorgarmo_se_corrige_a_multiorgasmo():
    """El caso EXACTO del reporte: el typo debe resolver al producto real."""
    _con_cache_catalogo(_PALABRAS_REALES)
    corregido = asyncio.run(catalog.corregir_typos_contra_catalogo("Tienen multiorgarmo"))
    assert "multiorgasmo" in corregido.lower(), corregido


def test_typos_de_marcas_conocidas_se_corrigen():
    _con_cache_catalogo(_PALABRAS_REALES)
    for typo, esperado in (("lovence", "lovense"), ("satisfayer", "satisfyer"),
                           ("iciclees", "icicles"), ("consolodor", "consolador")):
        corregido = asyncio.run(
            catalog.corregir_typos_contra_catalogo(f"tienen {typo}"))
        assert esperado in corregido.lower(), f"{typo!r} → {corregido!r}"


def test_texto_sin_typos_no_se_altera():
    """Si el cliente escribe bien, el texto debe pasar intacto."""
    _con_cache_catalogo(_PALABRAS_REALES)
    for texto in ("Tienen multiorgasmos", "quiero un consolador king cock",
                  "tienen lubricantes"):
        assert asyncio.run(catalog.corregir_typos_contra_catalogo(texto)) == texto


def test_palabras_sin_parecido_no_se_corrigen():
    """Ante algo que no se parece a nada del catálogo, NO inventar una
    corrección: es preferible no encontrar nada a mostrar el producto errado."""
    _con_cache_catalogo(_PALABRAS_REALES)
    for texto in ("tienen asdfghjk", "quiero qwertyui"):
        assert asyncio.run(catalog.corregir_typos_contra_catalogo(texto)) == texto


def test_sin_cache_de_catalogo_no_rompe():
    """Si el cache está vacío (DB caída al refrescar), la corrección debe ser
    un no-op silencioso en vez de propagar el error."""
    catalog._PALABRAS_CATALOGO_CACHE = frozenset()
    catalog._PALABRAS_CATALOGO_TS = 0.0
    try:
        resultado = asyncio.run(catalog.corregir_typos_contra_catalogo("Tienen multiorgarmo"))
    except Exception as exc:
        raise AssertionError(f"No debe propagar excepciones: {exc!r}")
    assert resultado == "Tienen multiorgarmo"


def test_variantes_singular_plural_no_bloquean_la_correccion():
    """'multiorgasmo'/'multiorgasmos' son la misma palabra: no deben tratarse
    como ambigüedad (era lo que impedía corregir el caso del reporte)."""
    assert catalog._misma_familia("multiorgasmo", "multiorgasmos") is True
    assert catalog._misma_familia("lovense", "satisfyer") is False


# ── "ver más" mantiene el subtipo del turno anterior ────────────────────────
# Caso reportado: el cliente pidió lencería → "Suspensorio" (5 suspensorios
# masculinos correctos) → "Mas diseños" y recibió 5 bodies de MUJER. El mensaje
# "Mas diseños" no lleva la palabra "suspensorio", así que el subtipo se perdía y
# la búsqueda traía cualquier producto de la categoría.

_HIST_SUSPENSORIO = [
    {"role": "user", "content": "Tienen lenceria"},
    {"role": "assistant", "content": (
        "¿buscas lencería para ella (body, baby doll, disfraz) o para él "
        "(suspensorio, pechera, conjunto masculino)? 😊")},
    {"role": "user", "content": "Suspensorio"},
    {"role": "assistant", "content": "Mira estos suspensorios masculinos 👇"},
]

_HIST_VIDRIO = [
    {"role": "user", "content": "Tienen dildos"},
    {"role": "assistant", "content": "¿realista, con ventosa, de vidrio/cristal, o doble?"},
    {"role": "user", "content": "vidrio"},
    {"role": "assistant", "content": "Te muestro una opción de dildo de vidrio 👇"},
]


def test_ver_mas_hereda_subtipo_caso_reportado():
    """'Mas diseños' tras 'Suspensorio' debe seguir buscando suspensorios."""
    r = _clasificar("Mas diseños", _HIST_SUSPENSORIO)
    assert r["categoria_funcional"] == "lenceria"
    assert r["subtipo_detectado"] == "suspensorio", (
        "el 'ver más' perdió el subtipo y traería cualquier lencería")


def test_ver_mas_hereda_subtipo_en_otras_categorias():
    """Mismo bug con dildos: 'vidrio' → 'Mas diseños' no debe traer otros dildos."""
    r = _clasificar("Mas diseños", _HIST_VIDRIO)
    assert r["categoria_funcional"] == "dildos"
    assert r["subtipo_detectado"] == "vidrio"


def test_pedir_otra_cosa_no_hereda_el_subtipo_anterior():
    """Si el cliente nombra otro producto, mandar lo que pidió — no lo del turno
    anterior. La herencia solo aplica a los 'ver más'."""
    for texto, esperado in (("muestrame bodies", "bodies"), ("quiero un body", "body")):
        r = _clasificar(texto, _HIST_SUSPENSORIO)
        assert r["subtipo_detectado"] == esperado, (
            f"{texto!r} no debe heredar 'suspensorio': dio {r['subtipo_detectado']!r}")


def test_cambio_de_categoria_no_arrastra_subtipo():
    """Tras ver suspensorios, pedir dildos no debe arrastrar 'suspensorio'."""
    r = _clasificar("tienen dildos", _HIST_SUSPENSORIO)
    assert r["categoria_funcional"] == "dildos"
    assert r["subtipo_detectado"] is None


def test_ver_mas_sin_historial_no_inventa_subtipo():
    assert _clasificar("Mas diseños", [])["subtipo_detectado"] is None


def test_suspensorio_implica_genero_hombre():
    """'Suspensorio' en el mensaje del cliente debe marcar género hombre, igual
    que _REGLAS_GENERO ya hace con esos productos. Antes daba None, y la
    categoría lencería podía devolver prendas de mujer."""
    for texto in ("Suspensorio", "quiero un suspensorio", "tienen pechera"):
        assert _clasificar(texto, [])["genero"] == "hombre", texto


def test_es_ver_mas_reconoce_mas_disenos():
    """Regresión de un bug preexistente: los patrones se comparan contra texto ya
    normalizado a ASCII, así que los que tenían 'ñ' nunca matcheaban."""
    for texto in ("Mas diseños", "más diseños", "mas modelos", "ver mas",
                  "quiero ver mas opciones", "otros diseños"):
        assert catalog._es_ver_mas(texto), f"{texto!r} debería ser un 'ver más'"
    for texto in ("quiero un body", "tienen lenceria", "hola"):
        assert not catalog._es_ver_mas(texto), f"{texto!r} NO es un 'ver más'"


# ── "si" tras succionadores traía vibradores Lovense ────────────────────────
# Caso reportado: el bot escribió "Succionador de Clítoris Tenera 2"; el motor de
# memoria adivinaba la categoría buscando palabras en ese texto y la rama de
# vibradores (que también busca "clítoris") se evaluaba ANTES que la de
# succionadores, cortando con break. Resultado: "si" → categoría vibradores →
# reset del estado → 5 vibradores Lovense con un texto de succionadores inventados.

_HIST_SUCCIONADORES = [
    {"role": "user", "content": "tienen succionadores"},
    {"role": "assistant", "content": (
        "¡Buena elección! Te muestro 5 opciones de succionadores para mujer 👇\n"
        "1️⃣ Succionador de Clítoris Tenera 2 — $629.800 — control por app\n"
        "2️⃣ Satisfyer Penguin Succionador Clitorial — $384.800 — diseño chic")},
]


def test_si_tras_succionadores_no_cambia_a_vibradores():
    """Caso EXACTO reportado: 'si' debe seguir en succionadores."""
    r = _clasificar("si", _HIST_SUCCIONADORES)
    assert r["categoria_funcional"] == "succionadores", (
        "'clítoris' en el texto del bot no debe clasificar la conversación como "
        "vibradores: un succionador de clítoris es un succionador")


def test_categoria_del_estado_manda_sobre_la_heuristica_de_texto():
    """La categoría persistida es el dato confiable; la heurística que lee el
    texto del bot es solo respaldo para cuando no hay estado."""
    r = asyncio.run(catalog.clasificar_intencion_cliente(
        "si", _HIST_SUCCIONADORES, "succionadores"))
    assert r["categoria_funcional"] == "succionadores"


def test_estado_evita_que_una_descripcion_ambigua_desvie_la_categoria():
    """Aunque el texto del bot mencione otra cosa, el estado guardado manda."""
    hist_ambiguo = [
        {"role": "user", "content": "tienen succionadores"},
        {"role": "assistant", "content": "Este modelo también funciona como vibrador de clítoris 😊"},
    ]
    r = asyncio.run(catalog.clasificar_intencion_cliente(
        "si", hist_ambiguo, "succionadores"))
    assert r["categoria_funcional"] == "succionadores"


def test_heuristica_de_respaldo_ordena_de_especifico_a_generico():
    """Sin estado, la heurística debe seguir acertando: succionadores antes que
    vibradores, y lencería (suspensorio) antes que la rama genérica."""
    casos = [
        ("Te muestro succionadores de clítoris 👇", "succionadores"),
        ("Mira estos suspensorios masculinos 👇", "lenceria"),
        ("Estos vibradores con punto G te van a gustar", "vibradores"),
        ("¿lo buscas a base de agua o de silicona?", "lubricantes-y-cuidado"),
    ]
    for texto_bot, esperado in casos:
        hist = [{"role": "user", "content": "hola"},
                {"role": "assistant", "content": texto_bot}]
        r = _clasificar("si", hist)
        assert r["categoria_funcional"] == esperado, (
            f"{texto_bot!r} → {r['categoria_funcional']!r}, esperado {esperado!r}")


def test_cambio_de_tema_explicito_sigue_funcionando_con_estado():
    """El estado no debe congelar la conversación: si el cliente nombra otra
    categoría, se le hace caso."""
    r = asyncio.run(catalog.clasificar_intencion_cliente(
        "ahora quiero ver dildos", _HIST_SUCCIONADORES, "succionadores"))
    assert r["categoria_funcional"] == "dildos"
