"""Cuándo el cliente está cerrando y cuándo sigue buscando.

Incidente de producción del 2026-08-05, 20:25 UTC. El diálogo:

    Cliente: "Puedo ver dildos"
    Bot:     ¿realista o con ventosa?
    Cliente: "Con ventosa lo quiero pequeno es para primera vez o que me recomiendas"
    Bot:     "Te muestro primero los dildos con ventosa pequeños... 👇
              •  Dildo Pequeño Con Ventosa 12 cm — $45.000 — tamaño compacto
              •  Dildo Mini Ventosa Silicona 10 cm — $55.000 — suave y seguro
              •  Dildo Pequeño Realista Ventosa 13 cm — $60.000 — textura piel"

Esos tres productos NO EXISTEN. Los inventó el LLM.

La cadena, según los logs del contenedor:

    "Fase de venta detectada — fotos desactivadas este turno"
      → la búsqueda nunca corre → cero candidatos
      → el LLM redacta libre y se inventa tres productos
      → "ID 50123 del LLM rechazado — alucinación evitada"  ×3   (el filtro de IDs SÍ funcionó)
      → pero la guardia anti-alucinación está desactivada en fase de venta
      → el TEXTO sale igual

El disparador es "lo quiero", que está en el patrón de checkout. El cliente
estaba refinando su búsqueda, no cerrando una compra.
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


# ── Refinar no es cerrar ──

def test_refinar_una_busqueda_no_es_fase_de_venta():
    """El mensaje exacto del incidente. Apagaba la búsqueda entera."""
    assert not main._es_fase_venta(
        "Con ventosa lo quiero pequeno es para primera vez o que me recomiendas", [])


def test_otros_refinamientos_con_verbo_de_compra():
    for texto in ("lo quiero con ventosa", "lo quiero realista",
                  "ese quiero pero de silicona", "lo quiero anal"):
        assert not main._es_fase_venta(texto, []), texto


def test_una_seleccion_sin_facetas_nuevas_si_es_fase_de_venta():
    """Lo que el detector debe seguir cogiendo: no nombra nada nuevo, así que no
    hay nada que buscar — está eligiendo."""
    for texto in ("lo quiero", "me lo llevo", "lo compro", "ese quiero"):
        assert main._es_fase_venta(texto, []), texto


def test_las_senales_de_pago_mandan_aunque_haya_facetas():
    """"ya pagué el lubricante" es checkout por mucho que nombre un tipo: ahí el
    cliente está pagando, no explorando."""
    for texto in ("ya pague el lubricante", "adjunto comprobante del dildo",
                  "transferi por nequi el vibrador"):
        assert main._es_fase_venta(texto, []), texto


def test_los_datos_de_envio_siguen_siendo_fase_de_venta():
    for texto in ("mi direccion es calle 1 #32a-47", "ciudad Bogota",
                  "mi telefono es 3216549870"):
        assert main._es_fase_venta(texto, []), texto


# ── La guardia distingue un resumen de pedido de una invención ──

_LISTA_INVENTADA = ("Te muestro primero los dildos con ventosa 👇\n"
                    "•  Dildo Pequeño Con Ventosa 12 cm — $45.000")


def test_una_lista_inventada_en_fase_de_venta_se_reemplaza():
    """El caso del incidente: fase de venta, cero candidatos, carrito vacío, y
    el LLM lista productos con precios. No hay nada que resumir — se inventó."""
    assert main._parece_invencion(
        reply=_LISTA_INVENTADA,
        info={"debe_mostrar": False, "en_fase_venta": True, "carrito": []},
        final_productos=[])


def test_un_resumen_de_pedido_con_carrito_no_se_toca():
    """Con carrito, la misma forma de texto es el resumen legítimo del pedido —
    y convertirlo en la pregunta de categoría es el bug que la exclusión de fase
    de venta vino a arreglar en su día."""
    assert not main._parece_invencion(
        reply="Perfecto, tengo tu pedido:\n📦 Huevo Masturbador Goggy — $24.900",
        info={"debe_mostrar": False, "en_fase_venta": True,
              "carrito": [{"producto_id": 1, "nombre": "Huevo Masturbador Goggy",
                           "precio": 24900}]},
        final_productos=[])


def test_con_productos_reales_que_se_van_a_enviar_no_hay_invencion():
    assert not main._parece_invencion(
        reply="Mira estas opciones 👇\n• *Dildo Real* — $50.000",
        info={"debe_mostrar": True, "en_fase_venta": False, "carrito": []},
        final_productos=[{"id": 1}])


def test_fuera_de_fase_de_venta_el_carrito_no_autoriza_nada():
    """El carrito solo justifica un RESUMEN durante el cierre. Ofrecer productos
    nuevos sin candidatos sigue siendo una invención, haya carrito o no."""
    assert main._parece_invencion(
        reply=_LISTA_INVENTADA,
        info={"debe_mostrar": False, "en_fase_venta": False,
              "carrito": [{"producto_id": 1, "nombre": "X", "precio": 1}]},
        final_productos=[])


# ── La guardia ve la lista aunque el LLM no ponga negritas ──

def test_la_guardia_ve_una_lista_sin_negritas():
    """El LLM escribió viñetas sin negritas. La regex exigía asteriscos, así que
    la lista inventada era invisible; aquí la salvó `_OFRECE_PRODUCTOS_RE`
    ("Te muestro"), que pudo no estar."""
    texto = ("•  Dildo Pequeño Con Ventosa 12 cm — $45.000 — compacto\n"
             "•  Dildo Mini Ventosa Silicona 10 cm — $55.000 — suave")
    assert main._LISTA_PRODUCTOS_RE.search(texto), texto


def test_la_guardia_sigue_viendo_el_formato_con_negritas():
    assert main._LISTA_PRODUCTOS_RE.search("• *Dildo Real* — $50.000")


def test_la_guardia_ve_tambien_el_formato_heredado_con_keycap():
    assert main._LISTA_PRODUCTOS_RE.search("1️⃣ Dildo Real — $50.000")


def test_una_frase_normal_con_un_precio_no_es_una_lista():
    """Una cifra suelta dentro de una frase no es un catálogo."""
    for texto in ("El envío cuesta $12.000 y llega mañana",
                  "El total de tu pedido es $169.700",
                  "Tenemos opciones desde $29.800 hasta $250.000"):
        assert not main._LISTA_PRODUCTOS_RE.search(texto), texto
