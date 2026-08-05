"""Una imagen que no es comprobante de pago debe escalar a asesor, no ignorarse.

Regla del negocio (ver transcripts): "Las personas que envíen una imagen con la
foto de un producto escalar por ahora."

Antes, el handler hacía `return` silencioso: el cliente enviaba una foto y el
bot no respondía. Peor, handle_inbound_image SIEMPRE llamaba a GPT-4o vision y
registraba un abono espurio en la DB, incluso sin contexto de pago.

Ahora: (a) handle_inbound_image devuelve False sin llamar a vision cuando no hay
pedido pendiente ni contexto de pago; (b) el handler de imagen responde, escala
y agenda follow-up, igual que el Caso C (video/documento/sticker).
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

from app import payments  # noqa: E402


def test_contexto_habla_de_pago_detecta_palabras_clave():
    assert payments._contexto_habla_de_pago("les envio el comprobante", [])
    assert payments._contexto_habla_de_pago("ya pague", [])
    assert payments._contexto_habla_de_pago("transferencia nequi", [])
    assert payments._contexto_habla_de_pago(None, [{"role": "user", "content": "ya pagué"}])


def test_contexto_sin_pago_no_dispara():
    assert not payments._contexto_habla_de_pago("vi este producto en instagram", [])
    assert not payments._contexto_habla_de_pago(None, [])
    assert not payments._contexto_habla_de_pago(None, [{"role": "user", "content": "hola"}])


def test_contexto_normaliza_tildes():
    """'consigné' con tilde debe casar con 'consignación' normalizado."""
    assert payments._contexto_habla_de_pago("ya consigné", [])
    assert payments._contexto_habla_de_pago("hago la consignación", [])


async def _handle_inbound_image_sin_pedido(caption, history):
    """Llama handle_inbound_image mockeando _get_monto_esperado para que no haya
    pedido pendiente, y espiando si se llama a _analyze_comprobante."""
    llamadas_vision = {"n": 0}

    async def _sin_pedido(_wa_id):
        return None, None

    async def _download(_url):
        return "fake_b64"

    async def _analyze(_b64, _monto):
        llamadas_vision["n"] += 1
        return {"valido": False, "razon": "mock", "monto": None}

    orig_monto = payments._get_monto_esperado
    orig_download = payments._download_image_as_b64
    orig_analyze = payments._analyze_comprobante
    orig_wa = payments.whatsapp_client
    orig_db = payments.db
    payments._get_monto_esperado = _sin_pedido
    payments._download_image_as_b64 = _download
    payments._analyze_comprobante = _analyze
    # Silenciar el flujo de abono (no es el foco de este test): mockea
    # whatsapp_client.send_text y db.insert_abono para que no toquen la red.
    async def _noop(*a, **k):
        return None

    async def _count(*a, **k):
        return 0

    payments.whatsapp_client = types.SimpleNamespace(send_text=_noop)
    payments.db = types.SimpleNamespace(
        insert_abono=_noop, count_abonos_fallidos=_count)
    try:
        resultado = await payments.handle_inbound_image(
            wa_id="57300", image_url="http://x/img.jpg",
            caption=caption, message_id="m1", history=history)
        return resultado, llamadas_vision
    finally:
        payments._get_monto_esperado = orig_monto
        payments._download_image_as_b64 = orig_download
        payments._analyze_comprobante = orig_analyze
        payments.whatsapp_client = orig_wa
        payments.db = orig_db


def test_imagen_sin_pedido_ni_contexto_de_pago_devuelve_false():
    """No hay pedido pendiente y el caption no habla de pago: la imagen no es
    comprobante. Debe devolver False sin llamar a GPT-4o vision."""
    resultado, llamadas = asyncio.run(
        _handle_inbound_image_sin_pedido("vi este producto", []))
    assert resultado is False, "debe devolver False sin contexto de pago"
    assert llamadas["n"] == 0, "NO debe llamar a GPT-4o vision"


def test_imagen_sin_pedido_pero_con_contexto_de_pago_procesa():
    """No hay pedido pendiente pero el caption habla de pago: la imagen podría
    ser comprobante (el cliente paga antes de que se cree el pedido). Debe
    procesarse con vision. Verificamos solo que se llama a vision (el flujo
    completo de abono requiere más mocks y no es el foco de este test)."""
    _, llamadas = asyncio.run(
        _handle_inbound_image_sin_pedido("les envio el comprobante de pago", []))
    assert llamadas["n"] == 1, "con contexto de pago debe llamar a GPT-4o vision"


def test_imagen_sin_caption_ni_historial_no_procesa():
    """Foto aislada sin nada: foto de producto, escalar."""
    resultado, llamadas = asyncio.run(_handle_inbound_image_sin_pedido(None, []))
    assert resultado is False, resultado
    assert llamadas["n"] == 0, llamadas
