"""El clasificador LLM ahora devuelve subtipo y atributo, no solo categoria.

Antes devolvía {categoria, genero} y perdía la información fina: "para demorar"
llegaba a lubricantes-y-cuidado pero sin el atributo desensibilizante, así que
el SQL devolvía lubricantes cualesquiera. Ahora el LLM clasifica también el
subtipo (variante nombrada) y el atributo (característica funcional), siempre
dentro de un vocabulario cerrado: si inventa algo que el catálogo no sabe
filtrar, se descarta en vez de dar 0 resultados falsos.

Estos tests mockean el cliente de OpenAI para no depender de la red: verifican
que el PARSEO y la VALIDACIÓN de la salida del LLM sean correctos.
"""
import asyncio
import json
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

for _m in ("asyncpg", "httpx", "openai", "qdrant_client", "redis", "redis.asyncio",
           "tiktoken", "PIL", "PIL.Image"):
    _mod = types.ModuleType(_m)
    _mod.__getattr__ = lambda _n: type("_Any", (), {"__init__": lambda *a, **k: None})  # type: ignore[attr-defined]
    sys.modules.setdefault(_m, _mod)

import importlib
from app import openai_client  # noqa: E402

# Otros tests de la suite reasignan openai_client.clasificar_intencion_llm a
# nivel de módulo (para silenciar el LLM) al importarse — y como el runner
# carga los módulos en orden alfabético, para cuando este módulo se importa,
# la función ya puede estar reemplazada por _sin_llm. Recargamos el módulo
# para recuperar la implementación real antes de guardar la referencia.
importlib.reload(openai_client)
_LLM_REAL = openai_client.clasificar_intencion_llm


class _Respuesta:
    """Simula la respuesta de OpenAI."""
    def __init__(self, content):
        self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=content))]


def _mock_llm(respuesta_dict):
    """Devuelve un callable async que simula chat.completions.create."""
    async def _create(**kwargs):
        return _Respuesta(json.dumps(respuesta_dict))
    return _create


def _clasificar_mensaje(texto, respuesta_dict):
    """Llama a clasificar_intencion_llm con el cliente mockeado."""
    # Restaurar la implementación real: otros tests la pueden haber silenciado.
    openai_client.clasificar_intencion_llm = _LLM_REAL
    openai_client._cache_clasif.clear()
    cliente = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=_mock_llm(respuesta_dict))
        )
    )
    orig = openai_client._get_client
    openai_client._get_client = lambda: cliente
    try:
        return asyncio.run(openai_client.clasificar_intencion_llm(texto))
    finally:
        openai_client._get_client = orig


def test_demorar_llega_a_desensibilizante():
    """La frase que antes se perdía: ahora el LLM aporta el atributo."""
    r = _clasificar_mensaje(
        "algo para demorar y durar mas",
        {"categoria": "lubricantes-y-cuidado", "genero": "hombre",
         "subtipo": None, "atributo": "desensibilizante"})
    assert r["categoria"] == "lubricantes-y-cuidado", r
    assert r["atributo"] == "desensibilizante", r
    assert r["subtipo"] is None, r


def test_para_una_buena_ereccion_llega_a_desensibilizante():
    """Conversación real del transcript: 'para una buena ereccion'."""
    r = _clasificar_mensaje(
        "que productos tienen para una buena ereccion",
        {"categoria": "lubricantes-y-cuidado", "genero": "hombre",
         "subtipo": None, "atributo": "desensibilizante"})
    assert r["categoria"] == "lubricantes-y-cuidado", r
    assert r["atributo"] == "desensibilizante", r


def test_disfraz_de_policia_clasifica_subtipo():
    """El subtipo nombrado viaja en el campo subtipo."""
    r = _clasificar_mensaje(
        "tienen disfraz de policia",
        {"categoria": "lenceria", "genero": "mujer",
         "subtipo": "policia", "atributo": None})
    assert r["categoria"] == "lenceria", r
    assert r["subtipo"] == "policia", r


def test_consolador_con_ventosa_clasifica_subtipo_y_atributo():
    """La intersección es esperada: ventosa es subtipo (filtro por nombre) y
    atributo (filtro SQL). El LLM puede aportar ambos."""
    r = _clasificar_mensaje(
        "consolador con ventosa",
        {"categoria": "dildos", "genero": "mujer",
         "subtipo": "ventosa", "atributo": "ventosa"})
    assert r["subtipo"] == "ventosa", r
    assert r["atributo"] == "ventosa", r


def test_saludo_llega_como_ninguna():
    """Un mensaje que no busca producto no debe disparar búsqueda."""
    r = _clasificar_mensaje(
        "hola buenas noches",
        {"categoria": "ninguna", "genero": None,
         "subtipo": None, "atributo": None})
    assert r["categoria"] == "ninguna", r


def test_atributo_inventado_por_el_llm_se_descarta():
    """Si el LLM devuelve un atributo que el catálogo no sabe filtrar, se
    descarta (null) en vez de propagarlo: un atributo inválido daría 0 resultados
    falsos y un escalado sin motivo."""
    r = _clasificar_mensaje(
        "algo chiquito",
        {"categoria": "dildos", "genero": None,
         "subtipo": None, "atributo": "chiquito"})
    assert r["categoria"] == "dildos", r
    assert r["atributo"] is None, "atributo inventado debe descartarse"


def test_subtipo_inventado_por_el_llm_se_descarta():
    """Igual que el atributo: un subtipo que no está en el vocabulario cerrado
    se descarta para no romper el filtro por nombre."""
    r = _clasificar_mensaje(
        "algo raro",
        {"categoria": "dildos", "genero": None,
         "subtipo": "flotante", "atributo": None})
    assert r["subtipo"] is None, "subtipo inventado debe descartarse"


def test_categoria_invalida_se_descarta_toda_la_clasificacion():
    """Una categoría fuera del conjunto cerrado invalida todo el resultado,
    igual que antes: no queremos que el LLM invente categorías."""
    r = _clasificar_mensaje(
        "algo",
        {"categoria": "vehiculos", "genero": None,
         "subtipo": None, "atributo": None})
    assert r is None, "categoría inválida debe invalidar toda la clasificación"


def test_json_sin_subtipo_ni_atributo_es_retrocompatible():
    """El LLM (o un mock viejo) puede omitir subtipo/atributo. La función debe
    rellenarlos con None, no romper."""
    r = _clasificar_mensaje(
        "vibradores",
        {"categoria": "vibradores", "genero": "mujer"})
    assert r["categoria"] == "vibradores", r
    assert r["subtipo"] is None, r
    assert r["atributo"] is None, r


def test_el_cache_devuelve_los_4_campos():
    """La segunda llamada (cacheada) debe traer los 4 campos, no solo 2."""
    openai_client.clasificar_intencion_llm = _LLM_REAL
    openai_client._cache_clasif.clear()
    cliente = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=_mock_llm(
                {"categoria": "dildos", "genero": None,
                 "subtipo": "ventosa", "atributo": "ventosa"}))
        )
    )
    orig = openai_client._get_client
    openai_client._get_client = lambda: cliente
    try:
        r1 = asyncio.run(openai_client.clasificar_intencion_llm("consolador con ventosa"))
        r2 = asyncio.run(openai_client.clasificar_intencion_llm("consolador con ventosa"))
    finally:
        openai_client._get_client = orig
    assert r1 == r2, (r1, r2)
    assert r2["subtipo"] == "ventosa", r2
    assert r2["atributo"] == "ventosa", r2


# ── Integración: el atributo del LLM llega a las restricciones ──
# Estos tests mockean clasificar_intencion_llm y llaman a
# clasificar_intencion_cliente, que es el orquestador real. Verifican que el
# atributo funcional detectado por el LLM se propaga al dict de restricciones,
# que es lo que el SQL usa con `atributos @> [...]`.

from app import catalog  # noqa: E402


def _clasificar_con_llm(user_text, respuesta_llm, history=None, estado=None,
                        productos_en_catalogo=7):
    """Llama a clasificar_intencion_cliente con el LLM mockeado.

    `productos_en_catalogo` es lo que devolvería `contar_por_restricciones`: el
    atributo del LLM solo se inyecta si la búsqueda resultante tiene productos,
    así que sin este stub la suite mediría el camino de "no se pudo comprobar".
    """
    openai_client.clasificar_intencion_llm = _LLM_REAL
    openai_client._cache_clasif.clear()

    async def _llm_mock(_texto):
        return respuesta_llm

    async def _contar_mock(_restricciones, subtipo=None):
        return productos_en_catalogo

    orig_llm = openai_client.clasificar_intencion_llm
    orig_contar = catalog.contar_por_restricciones
    openai_client.clasificar_intencion_llm = _llm_mock
    catalog.contar_por_restricciones = _contar_mock
    try:
        return asyncio.run(catalog.clasificar_intencion_cliente(
            user_text, history or [], estado))
    finally:
        openai_client.clasificar_intencion_llm = orig_llm
        catalog.contar_por_restricciones = orig_contar


def test_para_demorar_el_atributo_llega_a_restricciones():
    """La frase que antes se perdía: el LLM la rescata y el atributo llega al
    dict de restricciones, que es lo que filtra el SQL."""
    r = _clasificar_con_llm(
        "algo para demorar y durar mas",
        {"categoria": "lubricantes-y-cuidado", "genero": "hombre",
         "subtipo": None, "atributo": "desensibilizante"})
    assert r["categoria_funcional"] == "lubricantes-y-cuidado", r
    restricciones = r["restricciones"]
    assert "desensibilizante" in (restricciones.get("atributos") or []), restricciones
    # No se fija `tipo`: "lubricantes-y-cuidado" cubre lubricante Y cosmetica, y
    # elegir uno dejaba fuera los retardantes en spray/crema, que son cosmetica.
    assert not restricciones.get("tipo"), restricciones


def test_para_una_buena_ereccion_llega_a_restricciones():
    """Conversación real del transcript (Conv 3)."""
    r = _clasificar_con_llm(
        "que productos tienen para una buena ereccion",
        {"categoria": "lubricantes-y-cuidado", "genero": "hombre",
         "subtipo": None, "atributo": "desensibilizante"})
    assert "desensibilizante" in (r["restricciones"].get("atributos") or []), r["restricciones"]


def test_un_atributo_del_llm_que_deja_la_busqueda_vacia_se_descarta():
    """El cliente no dijo ese atributo: el LLM lo dedujo. No puede pausar el bot.

    Medido en producción: "quiero algo para una buena erección" devolvía 5
    productos, 1, o CERO —bot pausado y escalado— según si el modelo rellenaba
    `atributo` con nada, con `principiante` o con `realista`, para el mismo
    mensaje. Como `_ESCALERA_RELAJACION` excluye `atributos` a propósito, un
    atributo inventado no tenía forma de recuperarse.
    """
    r = _clasificar_con_llm(
        "quiero algo para una buena ereccion",
        {"categoria": "anillos-y-fundas", "genero": "hombre",
         "subtipo": None, "atributo": "realista"},
        productos_en_catalogo=0)
    assert "realista" not in (r["restricciones"].get("atributos") or []), r["restricciones"]


def test_si_el_catalogo_tiene_productos_el_atributo_si_entra():
    """La contraparte: el guard no debe tragarse los atributos que sí sirven."""
    r = _clasificar_con_llm(
        "quiero algo para una buena ereccion",
        {"categoria": "anillos-y-fundas", "genero": "hombre",
         "subtipo": None, "atributo": "realista"},
        productos_en_catalogo=4)
    assert "realista" in (r["restricciones"].get("atributos") or []), r["restricciones"]


def test_una_categoria_de_un_solo_tipo_si_fija_el_tipo():
    """Sin ambigüedad, el `tipo` sigue anclando la búsqueda al catálogo correcto."""
    r = _clasificar_con_llm(
        "algo que se pegue en la pared",
        {"categoria": "dildos", "genero": None,
         "subtipo": None, "atributo": "ventosa"})
    assert r["restricciones"].get("tipo") == "dildo", r["restricciones"]


def test_el_subtipo_del_llm_se_propaga_al_dict():
    """Si el LLM aporta un subtipo que las listas no detectaron, debe llegar al
    campo subtipo_detectado (que alimenta el filtro por nombre)."""
    r = _clasificar_con_llm(
        "tienen disfraz de policia",
        {"categoria": "lenceria", "genero": "mujer",
         "subtipo": "policia", "atributo": None})
    assert r["subtipo_detectado"] == "policia", r


def _claves_ofrecidas_en_el_prompt(etiqueta: str) -> set[str]:
    """Las claves que el prompt le enumera al LLM bajo `etiqueta`.

    El prompt lista las opciones en prosa, separadas por comas y cerradas con un
    punto; se recorta ese tramo y se parte por comas.
    """
    import re
    tramo = re.search(
        etiqueta + r".*?minúsculas, o null\):\n(.*?)\.\n", openai_client._CLASIFICADOR_PROMPT,
        re.S)
    assert tramo, f"no se encontró el listado de {etiqueta} en el prompt"
    return {c.strip() for c in tramo.group(1).replace("\n", " ").split(",") if c.strip()}


def test_lo_que_el_prompt_ofrece_es_lo_que_el_validador_acepta():
    """El prompt y el vocabulario cerrado tienen que decir lo mismo.

    Si el prompt ofrece una clave que el validador no acepta, el LLM obedece y
    la respuesta se descarta en silencio: el cliente pierde la clasificación fina
    y no queda rastro de por qué. Pasó con `sencilla`/`sencillo`. Al revés
    tampoco sirve: una clave válida que el prompt no menciona es vocabulario
    muerto que el LLM nunca va a usar.
    """
    subtipos = _claves_ofrecidas_en_el_prompt("Subtipo")
    assert subtipos == set(openai_client._SUBTIPOS_LLM_VALIDOS), {
        "solo en el prompt": sorted(subtipos - openai_client._SUBTIPOS_LLM_VALIDOS),
        "solo en el validador": sorted(openai_client._SUBTIPOS_LLM_VALIDOS - subtipos),
    }
    atributos = _claves_ofrecidas_en_el_prompt("Atributo")
    assert atributos == set(openai_client._ATRIBUTOS_LLM_VALIDOS), {
        "solo en el prompt": sorted(atributos - openai_client._ATRIBUTOS_LLM_VALIDOS),
        "solo en el validador": sorted(openai_client._ATRIBUTOS_LLM_VALIDOS - atributos),
    }


def test_el_vocabulario_del_llm_lo_sabe_filtrar_el_catalogo():
    """Cada clave que el LLM puede devolver tiene que existir aguas abajo.

    Un subtipo que `_SUBTIPO_KEYWORDS` no conoce, o un atributo que no está en
    `facetas.ATRIBUTOS`, no filtra nada: en modo estricto eso no muestra menos
    productos, escala a un asesor.
    """
    from app import catalog, facetas
    huerfanos = openai_client._SUBTIPOS_LLM_VALIDOS - set(catalog._SUBTIPO_KEYWORDS)
    assert not huerfanos, f"subtipos que el catálogo no sabe filtrar: {sorted(huerfanos)}"
    huerfanos = openai_client._ATRIBUTOS_LLM_VALIDOS - set(facetas.ATRIBUTOS)
    assert not huerfanos, f"atributos que no existen en facetas: {sorted(huerfanos)}"


def test_cuando_el_llm_dice_ninguna_las_listas_siguen_funcionando():
    """El LLM como respaldo: si devuelve 'ninguna', el flujo sigue con las
    listas. Un mensaje de saludo no debe disparar categoría."""
    r = _clasificar_con_llm(
        "hola buenas noches",
        {"categoria": "ninguna", "genero": None,
         "subtipo": None, "atributo": None})
    assert r["categoria_funcional"] is None, r
    assert r["subtipo_detectado"] is None, r
    assert not (r["restricciones"].get("atributos")), r["restricciones"]

