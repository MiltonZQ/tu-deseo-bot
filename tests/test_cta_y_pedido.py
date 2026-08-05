"""Después de la lista, el cliente tiene que saber qué contestar.

El CTA decía "¿Cuál te gusta?" y el cliente contestaba "ese", "el dildo" o
"quiero pedir". Ahora pide el número, y un mensaje de compra sin número recibe
la petición del número en vez de otra página de catálogo.
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

main = importar_main()

PRODS = [{"id": i, "nombre": f"Producto {i}", "precio": 50000} for i in (1, 2, 3)]


# ── Tarea 1: el CTA ──

def test_con_mas_opciones_el_cta_pide_numero_y_ofrece_ver_mas():
    txt = main._texto_desde_candidatos(PRODS, {"intencion": "vibradores", "hay_mas": True})
    assert "indícame el número o los números" in txt, txt
    assert "ver más diseños" in txt, txt


def test_sin_mas_opciones_el_cta_pide_numero_y_no_ofrece_ver_mas():
    txt = main._texto_desde_candidatos(PRODS, {"intencion": "vibradores", "hay_mas": False})
    assert "indícame el número o los números" in txt, txt
    assert "ver más" not in txt.lower(), txt


def test_el_cta_pide_el_numero_tambien_cuando_se_cedio_en_algo():
    """Con `relajado` el aviso sustituye la entrada, pero el CTA no cambia."""
    txt = main._texto_desde_candidatos(PRODS, {
        "intencion": "anal", "relajado": "zona", "hay_mas": False,
        "restricciones": {"tipo": "vibrador", "zona": "anal"}})
    assert "indícame el número o los números" in txt, txt


# ── Elegir de la lista mostrada ──
#
# La decisión de si el cliente está eligiendo ya no se infiere del TEXTO del
# historial (antes se buscaban keycaps con `_bot_mostro_lista`) sino de las
# rondas persistidas en el estado, y la resuelve `app.seleccion`. Aquí se
# comprueba el contrato desde el punto de vista de main.py: qué apaga las fotos
# y qué provoca pedirle el nombre al cliente.

from app import seleccion  # noqa: E402

RONDA = [{"categoria": "dildos", "productos": [
    {"id": 1, "nombre": "Dildo Uno Realista", "precio": 50000},
    {"id": 2, "nombre": "Dildo Dos Ventosa", "precio": 60000},
]}]
SIN_RONDAS: list[dict] = []


def test_el_ordinal_es_una_seleccion():
    for texto in ("el primero", "la segunda", "quiero el segundo",
                  "me llevo la primera", "dame el primero"):
        assert seleccion.resolver(texto, RONDA).ids, texto


def test_primero_como_adverbio_no_es_una_seleccion():
    """'primero quiero saber si es impermeable' es una duda, no una elección.
    Por eso el patrón exige artículo definido y va anclado al principio."""
    for texto in ("primero quiero saber si es impermeable",
                  "primero dime el precio",
                  "necesito saber primero el envio"):
        assert not seleccion.resolver(texto, RONDA).ids, texto


def test_sin_lista_previa_el_ordinal_no_selecciona_nada():
    assert not seleccion.resolver("el primero", SIN_RONDAS).ids


def test_la_seleccion_numerica_multiple_sigue_funcionando():
    """El caso del bug 17: 'El 2 y 3' es una elección de dos productos, no una
    petición de ver el catálogo otra vez."""
    for texto in ("el 2", "el 1 y el 2", "dame el 2", "1 y 2", "1,2"):
        assert seleccion.resolver(texto, RONDA).ids, texto
    assert sorted(seleccion.resolver("el 1 y el 2", RONDA).ids) == [1, 2]


def test_un_numero_suelto_no_es_una_seleccion():
    """Una edad, un teléfono o una dirección no eligen nada. Por eso el patrón
    consume el mensaje entero y limita a dos dígitos."""
    for texto in ("tengo 25 años", "mi telefono es 3216549870", "vivo en bogota"):
        assert not seleccion.resolver(texto, RONDA).ids, texto


# ── El pedido sin producto identificado ──

def test_quiere_pedir_sin_decir_cual_se_detecta_como_intencion_de_compra():
    """El caso del documento: 'quiero pedir' no lleva a otra página de catálogo,
    sino a pedirle al cliente que diga cuál."""
    for texto in ("quiero pedir", "quiero ordenar", "como puedo comprar",
                  "me gustaria comprar", "deseo ordenar", "quiero llevar",
                  "como hago para pedir", "dame ese", "quiero ese"):
        r = seleccion.resolver(texto, RONDA)
        assert r.hay_intencion_compra and not r.ids, texto


def test_una_duda_no_se_confunde_con_un_pedido():
    for texto in ("cuanto vale el envio", "son impermeables", "hacen envios a cali"):
        assert not seleccion.resolver(texto, RONDA).hay_intencion_compra, texto


def test_pedir_un_listado_en_plural_no_es_elegir():
    """'quiero los vibradores' pide ver, no comprar: el plural queda fuera del
    patrón de compra a propósito."""
    assert not seleccion.resolver("quiero los vibradores", RONDA).hay_intencion_compra


def test_si_ya_dijo_cual_no_hay_nada_que_pedirle():
    """'quiero el 2' y 'quiero el primero' casan el patrón de compra vaga por el
    'el', pero resuelven un producto: gana la selección, que es más específica.
    main.py solo pide el nombre cuando NO se resolvió nada."""
    for texto in ("quiero el 2", "quiero el primero", "quiero el realista"):
        r = seleccion.resolver(texto, RONDA)
        assert r.ids, texto


def test_la_copia_no_vuelve_a_listar_productos():
    assert "[FOTO:" not in main.PEDIR_NUMERO_DE_LISTA


def test_la_desambiguacion_nombra_las_opciones_concretas():
    """Repetir "¿cuál?" en abstracto deja al cliente donde estaba: ya dijo "el
    dildo". Se le dan los nombres entre los que elegir."""
    r = seleccion.resolver("quiero el dildo", RONDA)
    assert not r.ids and len(r.ambiguos) == 2, r
    pregunta = main._pregunta_desambiguacion(r.ambiguos)
    assert "Dildo Uno Realista" in pregunta, pregunta
    assert "Dildo Dos Ventosa" in pregunta, pregunta


# ── El bloque de precios que recibe el LLM ──

def test_el_bloque_de_precios_va_numerado_como_la_lista():
    """El cliente elige por número; el bloque que ve el LLM iba con viñetas y
    tenía que contar para saber cuál era '1'. El formato del precio es el mismo
    que ve el cliente ($29.900, punto como separador de miles)."""
    lineas = main._detalle_productos_mostrados(
        [{"nombre": "Esposas Lois", "precio": 29900},
         {"nombre": "Esposas Kratos", "precio": 45900}]).splitlines()
    assert lineas[0].strip().startswith("1️⃣"), lineas
    assert "Esposas Lois" in lineas[0]
    assert "29.900" in lineas[0], lineas
    assert "29,900" not in lineas[0], lineas
    assert lineas[1].strip().startswith("2️⃣"), lineas


def test_la_numeracion_del_bloque_no_reinicia_tras_ver_mas():
    """Con offset, el sexto producto es 6️⃣ para el cliente y para el LLM."""
    prods = [{"nombre": f"P{i}", "precio": 1000} for i in range(1, 4)]
    lineas = main._detalle_productos_mostrados(prods, offset=5).splitlines()
    assert lineas[0].strip().startswith("6️⃣"), lineas


def test_un_producto_que_ya_no_existe_no_corre_los_numeros():
    """Si el 2 no se resuelve por ID, el 3 sigue siendo el 3. Si corriera, el
    '3' del cliente y el '3' del LLM serían productos distintos."""
    lineas = main._detalle_productos_mostrados(
        [{"nombre": "Uno", "precio": 1000}, None,
         {"nombre": "Tres", "precio": 3000}]).splitlines()
    assert len(lineas) == 2, lineas
    assert lineas[1].strip().startswith("3️⃣"), lineas
    assert "Tres" in lineas[1]


def test_fase_venta_no_se_pierde_tras_varias_preguntas_intermedias():
    """El cliente pidio datos de envio, luego hizo varias preguntas de
    seguimiento (horario, envio, quien recibe, empaque) antes de confirmar —
    la fase de venta no debe revertir a exploracion solo porque el mensaje de
    checkout salio de una ventana de 3 turnos de asistente."""
    history = [
        {"role": "assistant", "content": "¡Perfecto! Por favor indícame tu nombre completo, ciudad y dirección de entrega."},
        {"role": "user", "content": "¿A qué hora llega?"},
        {"role": "assistant", "content": "El envío llega en el transcurso del día, en horas de la tarde."},
        {"role": "user", "content": "¿Y si no estoy puedo dejar a alguien más?"},
        {"role": "assistant", "content": "Claro, cualquier persona mayor de edad puede recibirlo."},
        {"role": "user", "content": "¿Viene en empaque discreto?"},
        {"role": "assistant", "content": "Sí, siempre en bolsa negra sellada, sin ningún logo."},
        {"role": "user", "content": "listo, ya está: Ana, Calle 1#32a-47"},
    ]
    assert main._es_fase_venta("listo, ya está: Ana, Calle 1#32a-47", history)


# ── El caption de cada foto LLEVA el precio ──
# La lista visible se envía troceada (intro → fotos → CTA), así que la foto es el
# único sitio donde el cliente ve cada precio junto a su producto. El número
# también va en el caption: es lo que sostiene la selección por índice.

def test_el_caption_de_la_foto_lleva_el_precio():
    enviados = []

    class _WA:
        @staticmethod
        async def send_image(wa_id, url, caption):
            enviados.append(caption)

    prods = [{"id": 1, "nombre": "Disfraz Colegiala Inocente Lerot",
              "precio": 119800, "imagen_url": "http://x/1.jpg"}]
    orig = main.whatsapp_client
    main.whatsapp_client = _WA
    try:
        asyncio.run(main._enviar_fotos_productos("57300", prods))
    finally:
        main.whatsapp_client = orig

    assert len(enviados) == 1, enviados
    assert "Disfraz Colegiala Inocente Lerot" in enviados[0], enviados[0]
    assert "1️⃣" in enviados[0], enviados[0]
    assert "119.800" in enviados[0], enviados[0]


def test_la_lista_del_texto_sigue_mostrando_el_precio():
    """La lista completa (con precios) se persiste en el historial aunque al
    cliente se le envíe troceada: es lo que mantiene `_bot_mostro_lista` y lo
    que ve el LLM. Si el precio se cayera de ahí, sería una regresión."""
    txt = main._texto_desde_candidatos(
        [{"id": 1, "nombre": "Disfraz Colegiala Inocente Lerot", "precio": 119800}],
        {"intencion": "lenceria", "hay_mas": False})
    assert "119.800" in txt, txt


# ── El rótulo sale de lo que se buscó, no del historial ──
# Pasó en producción: el cliente había pedido consoladores y al pedir disfraces
# el bot dijo "Te muestro estas opciones de consoladores" sobre fotos de
# disfraces. "disfraz" no es clave del mapa de intenciones, así que la intención
# se heredaba del historial.

def test_el_rotulo_no_usa_una_intencion_heredada_de_otra_categoria():
    txt = main._texto_desde_candidatos(PRODS, {
        "intencion": "consoladores", "intencion_heredada": True,
        "categoria_funcional": "dildos", "subtipo_detectado": "colegiala",
        "restricciones": {"tipo": "lenceria"}, "hay_mas": False})
    assert "consolador" not in txt.lower(), txt
    assert "colegiala" in txt.lower(), txt


def test_el_rotulo_de_una_intencion_fresca_conserva_la_palabra_del_cliente():
    """Si el cliente acaba de escribir 'consoladores', se le responde con SU
    palabra, no con la etiqueta interna."""
    txt = main._texto_desde_candidatos(PRODS, {
        "intencion": "consoladores", "intencion_heredada": False,
        "restricciones": {"tipo": "dildo"}, "hay_mas": True})
    assert "consoladores" in txt.lower(), txt


def test_el_rotulo_cae_al_tipo_buscado_cuando_no_hay_intencion():
    txt = main._texto_desde_candidatos(PRODS, {
        "intencion": None, "restricciones": {"tipo": "vibrador"}, "hay_mas": False})
    assert "vibradores" in txt.lower(), txt


# ── Envío troceado: intro → fotos → CTA ──
# Antes el bot mandaba intro + lista numerada + CTA en un solo texto, y después
# las fotos: el cliente veía cada precio dos veces y el CTA quedaba antes que
# las imágenes. Ahora el turno que arma el sistema se envía troceado: la intro
# sola, luego las fotos (cada una con su precio en el caption), y el CTA al
# final. El `reply` completo se sigue persistiendo en el historial: es lo que
# `_bot_mostro_lista` necesita para detectar la lista y lo que ve el LLM.

INFO_PROD = {"intencion": "lubricantes-y-cuidado", "hay_mas": False}
PRODS_CON_FOTO = [
    {"id": 10, "nombre": "Multiorgasmos Euforia X 30 Ml", "precio": 40000,
     "imagen_url": "http://x/10.jpg"},
]


def test_el_encabezado_no_incluye_la_lista_ni_el_cta():
    """La intro se envía sola antes de las fotos: si trajera la lista numerada
    o el CTA, duplicaría lo que ya va en las fotos y en el cierre."""
    encabezado = main._encabezado_lista(INFO_PROD)
    assert "1️⃣" not in encabezado, encabezado
    assert "40.000" not in encabezado, encabezado
    assert "indícame" not in encabezado, encabezado
    assert "👇" in encabezado, encabezado


def test_el_cuerpo_es_lo_que_se_envia_si_las_fotos_fallan():
    """Fallback: si ninguna foto llega, el cliente recibe la lista numerada con
    precios en texto. Tiene que tener keycap, precio y CTA para poder pedir."""
    cuerpo = main._cuerpo_lista(PRODS_CON_FOTO, INFO_PROD, offset=0)
    assert "1️⃣" in cuerpo, cuerpo
    assert "40.000" in cuerpo, cuerpo
    assert "indícame" in cuerpo, cuerpo
    assert "[FOTO:10]" in cuerpo, cuerpo


def test_el_reply_persistido_tiene_keycap_y_precio_para_el_historial():
    """Aunque al cliente le llegue troceado, en el historial se guarda el reply
    completo. `_bot_mostro_lista` busca el keycap ahí; el LLM lee ahí los
    productos con precio. Si faltaran, se rompería la selección por número."""
    reply = main._texto_desde_candidatos(PRODS_CON_FOTO, INFO_PROD)
    assert "1️⃣" in reply, reply
    assert "40.000" in reply, reply


def test_el_reply_persistido_conserva_nombres_y_precios():
    """El reply completo se guarda en el historial aunque al cliente le llegue
    troceado: es lo que lee el LLM para confirmar con el precio exacto.

    Detectar que hay una lista viva ya NO se hace leyendo este texto —antes se
    buscaban keycaps con `_bot_mostro_lista`—, sino consultando las rondas del
    estado. Un dato, no una inferencia sobre una cadena."""
    reply = main._texto_desde_candidatos(PRODS_CON_FOTO, INFO_PROD)
    assert "Multiorgasmos Euforia" in reply, reply
    assert "40.000" in reply, reply
