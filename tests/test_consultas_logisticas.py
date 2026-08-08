"""Preguntar por el domicilio no es preguntar por un producto.

Incidente de producción del 2026-08-08, 16:28. El diálogo:

    Cliente: "Deseo un domicilio"
    Bot:     "Déjame confirmar con el equipo si tenemos ese producto disponible 😊
              Mientras tanto, ¿te gustaría que te muestre otras opciones? Tenemos
              vibradores, dildos, lencería, lubricantes y más."
    Cliente: "Deseo un domicilio No he preguntado ningún producto"
    Bot:     "¡Perfecto! Para hacerte el domicilio, primero necesito saber qué
              producto te interesa..."

El LLM SÍ redactó una respuesta sobre domicilios. La borró una regex: el texto
contenía "Te muestro", que estaba en `_OFRECE_PRODUCTOS_RE`, y sin categoría el
guardia anti-invención no tenía pregunta que inyectar, así que sustituyó la
respuesta entera por `_SIN_RESULTADO_MSG`.

Dos arreglos, y el orden importa:

1. Quién decide de qué habla el cliente es el LLM, con el historial delante.
   `"logistica"` es ahora una clave del clasificador, no una lista de palabras:
   una lista se equivoca en cuanto el cliente escribe "me lo pueden traer".
2. El regex pierde la facultad de borrar. Solo una lista de nombre + precio sin
   candidatos (inventario fabricado, incidente del 2026-08-05) sigue
   reemplazando el texto; una frase suelta, nunca.
"""
import asyncio
import importlib
import json
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tests import stubs  # noqa: E402

stubs.stub_drivers()
stubs.stub_web()

from app import catalog, main, openai_client  # noqa: E402

# Otros módulos de la suite silencian el clasificador a nivel de módulo al
# importarse, y el runner carga en orden alfabético. Recargar para recuperar la
# implementación real (mismo truco que test_clasificador_llm.py).
importlib.reload(openai_client)
_LLM_REAL = openai_client.clasificar_intencion_llm


class _Respuesta:
    def __init__(self, content):
        self.choices = [types.SimpleNamespace(
            message=types.SimpleNamespace(content=content))]


def _cliente_mock(respuesta_dict, capturados=None):
    """Cliente de OpenAI simulado. Si se le pasa `capturados`, anota los
    `messages` de cada llamada para poder comprobar qué contexto recibió."""
    async def _create(**kwargs):
        if capturados is not None:
            capturados.append(kwargs.get("messages"))
        return _Respuesta(json.dumps(respuesta_dict))
    return types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=_create)))


def _clasificar(texto, respuesta_dict, history=None, capturados=None):
    openai_client.clasificar_intencion_llm = _LLM_REAL
    openai_client._cache_clasif.clear()
    orig = openai_client._get_client
    openai_client._get_client = lambda: _cliente_mock(respuesta_dict, capturados)
    try:
        return asyncio.run(openai_client.clasificar_intencion_llm(texto, history))
    finally:
        openai_client._get_client = orig


_LOGISTICA = {"categoria": "logistica", "genero": None,
              "subtipo": None, "atributo": None}


# ── El LLM clasifica la intención, con contexto ─────────────────────────────

def test_el_llm_clasifica_domicilio_como_logistica():
    """"logistica" es una respuesta VÁLIDA del clasificador. Antes caía dentro de
    "ninguna" junto a los saludos, y esa mezcla es la que impedía distinguir al
    cliente que pregunta por la entrega."""
    r = _clasificar("Deseo un domicilio", _LOGISTICA)
    assert r is not None, "el clasificador descartó 'logistica' como inválida"
    assert r["categoria"] == "logistica", r


def test_logistica_no_se_toma_como_categoria_de_producto():
    """La clave viaja hasta el llamador SIN fijar categoría ni intención: no
    cambia qué se busca, solo evita que se le responda con productos."""
    openai_client.clasificar_intencion_llm = _LLM_REAL
    openai_client._cache_clasif.clear()
    orig = openai_client._get_client
    openai_client._get_client = lambda: _cliente_mock(_LOGISTICA)
    try:
        clasif = asyncio.run(catalog.clasificar_intencion_cliente("Deseo un domicilio"))
    finally:
        openai_client._get_client = orig
    assert clasif["es_logistica"] is True, clasif
    assert clasif["categoria_funcional"] is None, clasif
    assert clasif["intencion"] is None, clasif


def test_el_clasificador_recibe_el_historial():
    """Sin contexto, "¿y ese?" o "me lo traen?" no se pueden clasificar. El
    clasificador solo veía el mensaje suelto."""
    capturados = []
    _clasificar("me lo pueden traer?", _LOGISTICA,
                history=[{"role": "user", "content": "quiero un vibrador"},
                         {"role": "assistant", "content": "¿de clítoris o punto G?"}],
                capturados=capturados)
    enviados = capturados[0]
    contenidos = [m["content"] for m in enviados]
    assert any("quiero un vibrador" in c for c in contenidos), enviados
    assert any("punto G" in c for c in contenidos), enviados
    assert enviados[-1]["content"] == "me lo pueden traer?", enviados


def test_la_cache_no_mezcla_conversaciones():
    """La caché se indexaba solo por el texto. Con contexto eso le devolvería a
    un cliente la clasificación de otro: el mismo "¿y ese?" no significa lo
    mismo en dos conversaciones."""
    openai_client._cache_clasif.clear()
    _clasificar("y ese?", {"categoria": "dildos", "genero": None,
                           "subtipo": None, "atributo": None},
                history=[{"role": "assistant", "content": "opciones de dildos"}])
    claves = list(openai_client._cache_clasif)
    assert len(claves) == 1, claves
    assert "dildos" in claves[0], "la clave de caché ignora el contexto"


def test_el_prompt_manda_clasificar_solo_el_ultimo_mensaje():
    """El contexto sirve para resolver referencias, no para heredar el tema: sin
    esta instrucción, un cliente que venía viendo dildos preguntaría por el
    domicilio y se clasificaría como dildos."""
    p = openai_client._CLASIFICADOR_PROMPT
    assert "ÚNICAMENTE el último mensaje" in p, p[-600:]
    assert "logistica" in p


def test_un_mensaje_mixto_prefiere_la_categoria_del_producto():
    """"quiero un vibrador, ¿hacen domicilio?" tiene las dos cosas. La búsqueda
    necesita la categoría; el envío se le responde igual."""
    assert "elige la categoría del PRODUCTO" in openai_client._CLASIFICADOR_PROMPT


# ── El guardia ya no borra la respuesta ─────────────────────────────────────

_RESPUESTA_DOMICILIO = ("¡Claro! Hacemos domicilios en Bogotá el mismo día 😊 "
                        "¿A qué dirección sería? Te muestro las opciones de "
                        "envío según tu zona.")

_INFO_LOGISTICA = {"debe_mostrar": False, "en_fase_venta": False,
                   "carrito": [], "es_logistica": True}


def test_el_incidente_del_domicilio_no_se_reemplaza():
    """El caso exacto del 08-08: sin candidatos, sin categoría, y una frase de
    plantilla dentro de una respuesta sobre el envío."""
    assert main._OFRECE_PRODUCTOS_RE.search(_RESPUESTA_DOMICILIO), (
        "el test dejó de reproducir el incidente: el texto ya ni casa")
    assert not main._parece_invencion(
        reply=_RESPUESTA_DOMICILIO, info=_INFO_LOGISTICA, final_productos=[])


def test_una_lista_inventada_en_turno_de_domicilio_si_se_mata():
    """La exoneración es SOLO para la evidencia blanda. Nombre + precio sigue
    siendo inventario fabricado, pregunte el cliente lo que pregunte."""
    assert main._parece_invencion(
        reply=("Te muestro las opciones 👇\n"
               "•  Dildo Pequeño Con Ventosa 12 cm — $45.000"),
        info=_INFO_LOGISTICA, final_productos=[])


def test_evidencia_dura_y_blanda_se_distinguen():
    assert main._evidencia_invencion("• *Dildo Real* — $50.000") == "lista"
    assert main._evidencia_invencion("Mira estas opciones 👇") == "plantilla"
    assert main._evidencia_invencion("¿En qué ciudad estás?") is None


def test_sin_categoria_y_sin_lista_el_texto_del_llm_sobrevive():
    """La rama que borraba. Fuera de logística también: una frase de plantilla
    sin precios no prueba que haya ningún producto inventado."""
    info = {"debe_mostrar": False, "en_fase_venta": False, "carrito": [],
            "es_logistica": False, "categoria_funcional": None}
    reply = "Mira estas opciones de pago que tenemos disponibles 😊"
    # La guardia sigue disparando (no hay categoría ni candidatos)...
    assert main._parece_invencion(reply, info, [])
    # ...pero la acción ya no es borrar: no hay lista que ocultar.
    assert main._evidencia_invencion(reply) == "plantilla"
    assert main._pregunta_de_calificacion(info) is None


def test_en_logistica_no_se_inyecta_pregunta_de_calificacion():
    """El cliente venía viendo dildos y pregunta por el domicilio. La categoría
    sigue pegada en memoria; responderle "¿realista o con ventosa?" es el mismo
    bug con otra cara."""
    assert main._pregunta_de_calificacion(
        {"categoria_funcional": "dildos", "es_logistica": True}) is None
    # Sin la bandera, esa misma info sí produce la pregunta.
    assert main._pregunta_de_calificacion(
        {"categoria_funcional": "dildos", "es_logistica": False}) is not None


# ── La regex ya no confunde castellano corriente con un catálogo ────────────

def test_frases_conversacionales_ya_no_disparan():
    for texto in ("te muestro cómo hacer la transferencia",
                  "aquí tienes el link de pago",
                  "estas son las formas de pago que manejamos",
                  "el domicilio en Bogotá se despacha el mismo día",
                  "¿Qué producto te gustaría? Te muestro cómo funciona"):
        assert not main._OFRECE_PRODUCTOS_RE.search(texto), f"falso positivo: {texto!r}"


def test_las_plantillas_de_los_incidentes_siguen_casando():
    """Los textos reales que la guardia existe para atrapar."""
    for texto in ("Mira estas opciones de anillos y vibradores para él que "
                  "tenemos disponibles",
                  "¡Genial! Aquí tienes 5 opciones de succionadores 👇",
                  "Te muestro las mejores opciones 👇",
                  "Te muestro primero los dildos con ventosa: estas son las "
                  "opciones disponibles"):
        assert main._OFRECE_PRODUCTOS_RE.search(texto), f"dejó de casar: {texto!r}"


def test_la_copia_de_la_regex_en_los_tests_sigue_sincronizada():
    """test_calificacion_categorias.py mantiene una copia literal a propósito
    (no importa `app`). Si se desincroniza, ese módulo prueba otra cosa."""
    fuente = (_ROOT / "app" / "main.py").read_text()
    copia = (_ROOT / "tests" / "test_calificacion_categorias.py").read_text()
    cuerpo = 'r"(te (las )?muestro|est[aá]s son|aqu[ií] tienes)[^\\n]{0,30}"'
    assert cuerpo in fuente and cuerpo in copia


# ── El prompt responde el envío primero y sin cifras ────────────────────────

def test_la_regla_de_logistica_esta_en_el_prompt():
    p = (_ROOT / "prompts" / "system.md").read_text()
    assert "RESPONDE ESO PRIMERO" in p
    assert "NUNCA des un valor de envío" in p


def test_no_se_pide_la_direccion_al_responder_el_envio():
    """Prueba real del 08-08 12:18: el bot contestó bien lo del domicilio pero
    remató con "¿A qué barrio o dirección sería?". La dirección se pide al cerrar
    el pedido (regla 7), no a quien todavía no ha elegido producto."""
    p = (_ROOT / "prompts" / "system.md").read_text()
    assert "NO le pidas la dirección todavía" in p
    # El mensaje de ejemplo es lo que el modelo copia: si vuelve a pedir la
    # dirección ahí, la regla de arriba no sirve de nada.
    ejemplo = p.split("Ejemplo del mensaje completo:")[1].split("\n")[0]
    for prohibido in ("¿A qué barrio", "dirección sería", "tu dirección"):
        assert prohibido not in ejemplo, ejemplo
    assert "qué producto te gustaría llevar" in ejemplo, ejemplo


def test_el_envio_nacional_menciona_el_tiempo_de_la_transportadora():
    """La cobertura nacional se responde en el mismo mensaje, sin esperar a que
    el cliente diga que está fuera de Bogotá."""
    p = (_ROOT / "prompts" / "system.md").read_text()
    ejemplo = p.split("Ejemplo del mensaje completo:")[1].split("\n")[0]
    assert "transportadora" in ejemplo, ejemplo
    assert "tiempo de entrega" in ejemplo, ejemplo
