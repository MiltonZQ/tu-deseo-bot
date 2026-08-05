"""De qué se compone el pedido al cerrar la venta.

Historia de este fichero: el bug original (2026-08-02) era que el cliente elegía
tres productos por índice ("1, 2 y 3") y el pedido guardaba solo el primero,
porque `_resolver_productos_y_total` sabía extraer un único número.

Se arregló extrayendo varios, pero el arreglo heredaba el problema de fondo: la
selección se reconstruía AQUÍ, al cerrar, releyendo el texto del historial e
indexando `productos_mostrados`. Ese array es un conjunto sin orden garantizado
—se acumula con `ARRAY(SELECT DISTINCT unnest(...))`— y es plano: un "2" no dice
de qué lista. Con tres categorías por medio, el índice apuntaba a cualquier cosa.

Ahora la selección se registra en el carrito cuando el cliente elige, y aquí solo
se lee. La resolución por nombre sobre el historial queda como red para las
conversaciones que estaban en vuelo cuando se desplegó el cambio.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

for _m in ("asyncpg", "httpx", "openai", "qdrant_client", "redis", "redis.asyncio",
           "tiktoken", "PIL", "PIL.Image"):
    _mod = types.ModuleType(_m)
    _mod.__getattr__ = lambda _n: type("_Any", (), {"__init__": lambda *a, **k: None})  # type: ignore[attr-defined]
    sys.modules.setdefault(_m, _mod)

from app import db, pedidos  # noqa: E402


class _FakeConn:
    def __init__(self, rows_by_id):
        self._rows_by_id = rows_by_id

    async def fetchrow(self, query, *params):
        if "WHERE id = $1" in query:
            return self._rows_by_id.get(params[0])
        return None

    async def fetch(self, *_a, **_k):
        return list(self._rows_by_id.values())


class _FakePool:
    def __init__(self, rows_by_id):
        self._rows_by_id = rows_by_id

    def acquire(self):
        rows = self._rows_by_id

        class _Ctx:
            async def __aenter__(self):
                return _FakeConn(rows)

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


_CATALOGO = {
    101: {"id": 101, "nombre": "Vibrador Lush 3 Lovense", "descripcion": "",
          "categoria": "Juguetes", "precio": 629800, "imagen_url": "http://x/1.jpg",
          "galeria_urls": None, "permalink": None},
    102: {"id": 102, "nombre": "Lubricante BliX H2O", "descripcion": "",
          "categoria": "Lubricantes", "precio": 29800, "imagen_url": "http://x/2.jpg",
          "galeria_urls": None, "permalink": None},
    103: {"id": 103, "nombre": "Esposas Kratos Plateado", "descripcion": "",
          "categoria": "Bondage", "precio": 55000, "imagen_url": "http://x/3.jpg",
          "galeria_urls": None, "permalink": None},
}


def _resolver(history, ids_mostrados=None, carrito=None):
    original, db._pool = getattr(db, "_pool", None), _FakePool(_CATALOGO)
    try:
        return asyncio.run(pedidos._resolver_productos_y_total(
            history, productos_mostrados_ids=ids_mostrados, carrito=carrito))
    finally:
        db._pool = original


# ── El carrito es la fuente autoritativa ──

def test_el_pedido_sale_del_carrito():
    """Lo que el cliente eligió, con el precio que se le dijo al elegirlo."""
    carrito = [
        {"producto_id": 101, "nombre": "Vibrador Lush 3 Lovense", "precio": 629800},
        {"producto_id": 103, "nombre": "Esposas Kratos Plateado", "precio": 55000},
    ]
    items, total = _resolver([], carrito=carrito)
    assert sorted(i["producto_id"] for i in items) == [101, 103]
    assert total == 629800 + 55000


def test_productos_de_categorias_distintas_entran_los_dos():
    """El caso que motivó el cambio: el cliente recorrió varias categorías y
    eligió de cada una, en turnos distintos."""
    carrito = [
        {"producto_id": 102, "nombre": "Lubricante BliX H2O", "precio": 29800},
        {"producto_id": 103, "nombre": "Esposas Kratos Plateado", "precio": 55000},
    ]
    items, _ = _resolver([{"role": "user", "content": "listo, esos dos"}],
                         ids_mostrados=[101], carrito=carrito)
    assert sorted(i["producto_id"] for i in items) == [102, 103]


def test_el_carrito_gana_sobre_el_historial():
    """El historial menciona el Lush porque se mostró; el carrito dice que el
    cliente eligió las esposas. Manda lo que eligió."""
    history = [{"role": "assistant", "content": "Vibrador Lush 3 Lovense — $629.800"}]
    carrito = [{"producto_id": 103, "nombre": "Esposas Kratos Plateado", "precio": 55000}]
    items, total = _resolver(history, ids_mostrados=[101], carrito=carrito)
    assert [i["producto_id"] for i in items] == [103]
    assert total == 55000


def test_el_precio_del_carrito_es_el_que_se_le_dijo_al_cliente():
    """Si WooCommerce resincroniza a mitad de conversación, se cobra lo
    prometido — no el precio nuevo."""
    carrito = [{"producto_id": 101, "nombre": "Vibrador Lush 3 Lovense", "precio": 599000}]
    items, total = _resolver([], carrito=carrito)
    assert total == 599000, "el precio congelado al elegir manda sobre el del catálogo"


# ── Red para conversaciones en vuelo (sin carrito todavía) ──

def test_sin_carrito_se_resuelve_por_nombre_en_el_historial():
    history = [{"role": "user", "content": "quiero el Lubricante BliX H2O"}]
    items, total = _resolver(history, ids_mostrados=[101, 102, 103])
    assert [i["producto_id"] for i in items] == [102]
    assert total == 29800


def test_sin_carrito_un_typo_en_la_marca_sigue_resolviendo():
    """La resolución del pedido no corregía typos: "Lovense" mal escrito no
    encontraba nada y el pedido caía al fallback del primer producto mostrado."""
    history = [{"role": "user", "content": "quiero el vibrador Lovenese"}]
    items, _ = _resolver(history, ids_mostrados=[101, 102, 103])
    assert [i["producto_id"] for i in items] == [101], items


def test_sin_carrito_ni_nombres_cae_al_primero_mostrado():
    """Último recurso: mejor un pedido con un producto que uno vacío, que el
    equipo completa desde el panel."""
    history = [{"role": "user", "content": "listo"}]
    items, _ = _resolver(history, ids_mostrados=[102])
    assert [i["producto_id"] for i in items] == [102]


def test_un_indice_suelto_ya_no_resuelve_nada_por_posicion():
    """La resolución posicional se eliminó: `productos_mostrados` es un conjunto
    sin orden garantizado, así que indexarlo era leer un orden accidental."""
    items, _ = _resolver([{"role": "user", "content": "2"}],
                         ids_mostrados=[101, 102, 103])
    assert [i["producto_id"] for i in items] == [101], (
        "sin carrito debe caer al fallback del primero, no al índice 2")
