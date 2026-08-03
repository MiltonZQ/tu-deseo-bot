"""Después de la lista, el cliente tiene que saber qué contestar.

El CTA decía "¿Cuál te gusta?" y el cliente contestaba "ese", "el dildo" o
"quiero pedir". Ahora pide el número, y un mensaje de compra sin número recibe
la petición del número en vez de otra página de catálogo.
"""
from __future__ import annotations

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


LISTA = [{"role": "assistant",
          "content": "1️⃣ *Dildo Uno* — $50.000\n2️⃣ *Dildo Dos* — $60.000"}]
SIN_LISTA = [{"role": "assistant", "content": "¿Buscas algo realista o con ventosa?"}]


# ── Tarea 3: el ordinal también es elegir ──

def test_el_ordinal_es_una_seleccion():
    for texto in ("el primero", "la segunda", "quiero el tercero",
                  "me llevo la primera", "dame el quinto"):
        assert main._es_seleccion_de_lista_mostrada(texto, LISTA), texto


def test_primero_como_adverbio_no_es_una_seleccion():
    """'primero quiero saber si es impermeable' es una duda, no una elección.
    Por eso el patrón exige artículo definido y va anclado al principio."""
    for texto in ("primero quiero saber si es impermeable",
                  "primero dime el precio",
                  "necesito saber primero el envio"):
        assert not main._es_seleccion_de_lista_mostrada(texto, LISTA), texto


def test_sin_lista_previa_el_ordinal_no_selecciona_nada():
    assert not main._es_seleccion_de_lista_mostrada("el primero", SIN_LISTA)


def test_la_seleccion_numerica_sigue_funcionando_igual():
    """Lo que ya funcionaba no se toca: el ordinal se AÑADE."""
    for texto in ("el 2", "el 1 y el 3", "dame el 2", "2 y 4"):
        assert main._es_seleccion_de_lista_mostrada(texto, LISTA), texto
    assert not main._es_seleccion_de_lista_mostrada("tengo 25 años", SIN_LISTA)


# ── Tarea 2: el pedido sin número ──

def test_quiere_pedir_sin_numero_recibe_la_peticion_del_numero():
    """El caso del documento: 'quiero pedir' no lleva a otra página de catálogo."""
    for texto in ("quiero pedir", "quiero ordenar", "como puedo comprar",
                  "me gustaria comprar", "deseo ordenar", "quiero llevar",
                  "como hago para pedir", "dame ese", "quiero ese"):
        assert main._pide_comprar_sin_numero(texto, LISTA, {}, "dildo"), texto


def test_sin_lista_previa_no_se_pide_ningun_numero():
    """Sin lista no hay número que pedir: toca preguntarle qué busca."""
    assert not main._pide_comprar_sin_numero("quiero comprar", SIN_LISTA, {}, None)


def test_nombrar_otra_categoria_es_una_busqueda_nueva():
    """Se mira la faceta del MENSAJE, no la fusionada con el estado."""
    assert not main._pide_comprar_sin_numero(
        "quiero comprar lubricantes", LISTA, {"tipo": "lubricante"}, "dildo")


def test_repetir_el_tipo_en_pantalla_si_es_una_seleccion():
    """'quiero el dildo' con dildos en pantalla no es una búsqueda nueva."""
    assert main._pide_comprar_sin_numero(
        "quiero el dildo", LISTA, {"tipo": "dildo"}, "dildo")


def test_un_atributo_nuevo_es_una_busqueda_aunque_repita_el_tipo():
    """'quiero un dildo doble' está refinando, no eligiendo."""
    assert not main._pide_comprar_sin_numero(
        "quiero un dildo doble", LISTA,
        {"tipo": "dildo", "atributos": ["doble"]}, "dildo")


def test_los_implicitos_de_facetas_no_cuentan_como_faceta_nombrada():
    """`interpretar_mensaje` devuelve claves internas con guion bajo."""
    assert main._pide_comprar_sin_numero(
        "quiero pedir", LISTA, {"_implicitos": [("doble", (), None)]}, "dildo")


def test_una_duda_no_se_confunde_con_un_pedido():
    for texto in ("cuanto vale el envio", "son impermeables", "hacen envios a cali"):
        assert not main._pide_comprar_sin_numero(texto, LISTA, {}, "dildo"), texto


def test_si_ya_dio_el_numero_no_se_le_vuelve_a_pedir():
    """'quiero el 2' trae el número: lo atiende la selección numérica, intacta."""
    assert not main._pide_comprar_sin_numero("quiero el 2", LISTA, {}, "dildo")


def test_el_ordinal_no_se_confunde_con_un_pedido_sin_numero():
    """'quiero el primero' casa el patrón de compra vaga por el 'el': gana la
    selección, que es más específica."""
    assert not main._pide_comprar_sin_numero("quiero el primero", LISTA, {}, "dildo")


def test_pedir_un_listado_en_plural_no_es_elegir():
    """'quiero los vibradores' pide ver, no comprar: el plural queda fuera."""
    assert not main._pide_comprar_sin_numero(
        "quiero los vibradores", LISTA, {"tipo": "vibrador"}, "vibrador")


def test_la_copia_no_vuelve_a_listar_productos():
    assert "️⃣" not in main.PEDIR_NUMERO_DE_LISTA
    assert "[FOTO:" not in main.PEDIR_NUMERO_DE_LISTA
    assert "indícame el número o los números" in main.PEDIR_NUMERO_DE_LISTA


def test_el_bot_mostro_lista_reconoce_el_keycap():
    assert main._bot_mostro_lista(LISTA)
    assert not main._bot_mostro_lista(SIN_LISTA)
    assert not main._bot_mostro_lista([])
    assert not main._bot_mostro_lista([{"role": "user", "content": "1️⃣ me gusta"}]), \
        "la lista la tiene que haber enviado el BOT, no el cliente"


# ── El bloque de precios que recibe el LLM ──

def test_el_bloque_de_precios_va_numerado_como_la_lista():
    """El cliente elige por número; el bloque que ve el LLM iba con viñetas y
    tenía que contar para saber cuál era '1'."""
    lineas = main._detalle_productos_mostrados(
        [{"nombre": "Esposas Lois", "precio": 29900},
         {"nombre": "Esposas Kratos", "precio": 45900}]).splitlines()
    assert lineas[0].strip().startswith("1️⃣"), lineas
    assert "Esposas Lois" in lineas[0]
    assert "29.900" in lineas[0] or "29,900" in lineas[0], lineas
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
