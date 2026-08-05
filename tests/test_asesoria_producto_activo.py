"""Cuando el cliente pregunta sobre el producto que ya vio, el LLM debe recibir
su ficha real.

Sin esto, preguntas como "¿tiene sabor?", "¿frío y calor?", "¿para quién es?"
se contestaban con sentido común: el LLM no tenía la descripción del producto
y respondía "sí, tiene sabor" a un lubricante que no lo tiene.

Estos tests mockean el cliente de OpenAI para interceptar el system_prompt y
verificar que la ficha del producto activo llega completa (con descripción, no
solo nombre+precio).
"""
import asyncio
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

from app import openai_client  # noqa: E402


class _EncoderFalso:
    """tiktoken está mockeado como _Any en la suite; su encode no existe.
    Este encoder devuelve 0 tokens para que fit_history no trunque el historial."""
    def encode(self, _texto):
        return []


# Garantizar que count_tokens tenga un encoder válido durante los tests.
openai_client._encoder = lambda: _EncoderFalso()


def _capturar_system_prompt():
    """Mockea el cliente de OpenAI y devuelve el system_prompt que se le pasó."""
    capturado = {"system": None}

    class _Respuesta:
        def __init__(self):
            self.choices = [types.SimpleNamespace(
                message=types.SimpleNamespace(
                    content="respuesta del bot", tool_calls=None))]

    async def _create(**kwargs):
        msgs = kwargs.get("messages") or []
        if msgs and msgs[0].get("role") == "system":
            capturado["system"] = msgs[0]["content"]
        return _Respuesta()

    cliente = types.SimpleNamespace(
        chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=_create)
        )
    )
    orig = openai_client._get_client
    openai_client._get_client = lambda: cliente
    return capturado, orig


def _complete_con_producto(user_message, producto_activo, candidatos=None):
    capturado, orig = _capturar_system_prompt()
    try:
        asyncio.run(openai_client.complete(
            user_message, history=[],
            candidatos=candidatos or [],
            producto_activo=producto_activo))
    finally:
        openai_client._get_client = orig
    return capturado["system"]


PRODUCTO = {
    "id": 123, "nombre": "Lubricante Sabor Fresa Hot",
    "precio": 45900,
    "descripcion": "Lubricante comestible sabor fresa con efecto calor. A base de agua, compatible con preservativo. Aplica para hombre y mujer.",
    "tipo": "lubricante", "zona": None, "vibra": False,
    "atributos": ["sabor", "calor", "agua"],
}


def test_la_ficha_del_producto_activo_llega_al_prompt():
    """La descripción COMPLETA del producto debe estar en el system_prompt,
    no truncada a 70 chars como en candidatos_block."""
    prompt = _complete_con_producto("ese tiene sabor?", PRODUCTO)
    assert prompt is not None, "no se capturó el system_prompt"
    assert "Lubricante Sabor Fresa Hot" in prompt, "falta el nombre"
    assert "45.900" in prompt, "falta el precio"
    # La descripción completa, con la frase que el cliente pregunta:
    assert "efecto calor" in prompt, "falta la descripción completa"
    assert "A base de agua" in prompt, "falta la descripción completa"


def test_los_atributos_del_producto_llegan_al_prompt():
    """El cliente puede preguntar por un atributo; el LLM necesita verlos."""
    prompt = _complete_con_producto("es de silicona?", PRODUCTO)
    assert "sabor" in prompt, "faltan los atributos"
    assert "calor" in prompt, "faltan los atributos"
    assert "agua" in prompt, "faltan los atributos"


def test_sin_producto_activo_no_hay_bloque_ficha():
    """Si no hay producto activo (primer turno, o no hay productos mostrados),
    el prompt no debe incluir la sección de ficha."""
    prompt = _complete_con_producto("hola", producto_activo=None)
    assert prompt is not None
    assert "Producto que el cliente está viendo" not in prompt, (
        "no debe haber bloque de ficha sin producto activo")


def test_producto_con_vibra_muestra_el_campo():
    """Un vibrador debe mostrar que vibra, para que el LLM pueda responder
    preguntas sobre modos de vibración."""
    vibrador = dict(PRODUCTO, nombre="Lovense Lush 3", vibra=True,
                    descripcion="Vibrador con app", tipo="vibrador",
                    atributos=["recargable", "impermeable"])
    prompt = _complete_con_producto("cuantos modos tiene?", vibrador)
    assert "Vibra: sí" in prompt, "falta el campo vibra"


def test_el_bloque_ficha_es_distinto_del_bloque_candidatos():
    """Un producto puede estar AMBOS bloques (candidato nuevo Y activo), o solo
    en uno. El de ficha lleva descripción completa; el de candidatos, truncada."""
    prompt = _complete_con_producto(
        "tiene sabor?", producto_activo=PRODUCTO, candidatos=[PRODUCTO])
    assert prompt is not None
    # El bloque de ficha (descripción completa) debe estar:
    assert "A base de agua" in prompt
