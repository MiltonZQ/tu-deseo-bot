"""Cambiar de tema no puede responder "ya te mostré todo".

Sesión del 2/08, 18:23 UTC. El cliente vio 5 suspensorios, pidió vibradores, y
recibió "Te mostré todas las opciones de vibradores que tenemos disponibles"
sin haber visto ninguno.

Los logs muestran la secuencia entera: se detectó el cambio de tema y se
reseteó el estado, el LLM alucinó 5 IDs que el filtro descartó, el sistema
inyectó la pregunta de calificación correcta, y la última reescritura la pisó.

Dos defectos en las mismas cuatro líneas de main.py:
  - `ids_ya_mostrados` sale de estado_previo sin mirar si el tema cambió.
  - La condición se abre con `foto_ids`, los IDs BRUTOS del LLM, en vez de con
    los productos ya validados que de verdad se van a enviar.
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

PREGUNTA_VIBRADORES = ("¡Claro que sí! Para recomendarte lo ideal, cuéntame: "
                       "¿buscas estimulación para ella, para él, anal, o en pareja? 😊")


def test_tras_cambiar_de_tema_no_se_avisa_de_categoria_agotada():
    """Los 5 suspensorios que vio el cliente no son opciones de vibradores."""
    assert not main._debe_avisar_agotado(
        reply=PREGUNTA_VIBRADORES,
        ids_ya_mostrados={101, 102, 103, 104, 105},
        final_productos=[],
        foto_ids=[79032, 79037],
        info={"debe_mostrar": False, "reset_state": True, "tema_nuevo": False},
        pedido_creado_id=None,
    )


def test_los_ids_alucinados_no_abren_el_aviso():
    """El filtro ya los descartó: final_productos está vacío porque no eran
    reales, no porque la categoría se haya agotado.

    El texto de la respuesta NO debe casar con `_OFRECE_PRODUCTOS_RE`: si casa,
    el aviso se dispara por esa vía —que es legítima— y el test no probaría lo
    que dice probar. Aquí lo único que queda en pie es `foto_ids`.
    """
    assert not main._debe_avisar_agotado(
        reply="Claro, cuéntame qué estás buscando 😊",
        ids_ya_mostrados={101, 102},
        final_productos=[],
        foto_ids=[99999],
        info={"debe_mostrar": False, "reset_state": False, "tema_nuevo": False},
        pedido_creado_id=None,
    )


def test_el_aviso_sigue_saliendo_cuando_la_categoria_si_se_agoto():
    """Su razón de ser: el cliente pidió 'ver más' de lo mismo y no queda nada."""
    assert main._debe_avisar_agotado(
        reply="¡Perfecto! Te muestro más diseños 👇",
        ids_ya_mostrados={101, 102, 103},
        final_productos=[],
        foto_ids=[],
        info={"debe_mostrar": True, "reset_state": False, "tema_nuevo": False},
        pedido_creado_id=None,
    )


def test_con_productos_que_enviar_nunca_se_avisa():
    assert not main._debe_avisar_agotado(
        reply="¡Perfecto! Te muestro estas opciones 👇",
        ids_ya_mostrados={101},
        final_productos=[{"id": 202, "nombre": "Vibrador Pigly"}],
        foto_ids=[202],
        info={"debe_mostrar": True, "reset_state": False, "tema_nuevo": False},
        pedido_creado_id=None,
    )
