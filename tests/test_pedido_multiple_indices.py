"""Bug reportado el 2026-08-02: el cliente nombraba 3 productos por indice de
una lista mostrada ("1, 2 y 3") y el pedido persistido en la base de datos
solo incluia el primero — el total no sumaba los tres.

Causa raiz: pedidos.py::_resolver_productos_y_total solo sabia extraer UN
numero de la seleccion (regex que exige que el mensaje completo sea un solo
digito). Un mensaje con varios indices no matcheaba nada y caia al fallback
"solo el primero de productos_mostrados_ids".
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

from app import catalog, db, pedidos  # noqa: E402


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


def _resolver(history, ids_mostrados):
    original, db._pool = getattr(db, "_pool", None), _FakePool(_CATALOGO)
    try:
        return asyncio.run(pedidos._resolver_productos_y_total(history, ids_mostrados))
    finally:
        db._pool = original


def test_seleccion_de_un_solo_indice_sigue_funcionando():
    history = [{"role": "user", "content": "2"}]
    items, total = _resolver(history, [101, 102, 103])
    assert [i["producto_id"] for i in items] == [102]
    assert total == 29800


def test_seleccion_de_dos_indices_con_y():
    history = [{"role": "user", "content": "1 y 3"}]
    items, total = _resolver(history, [101, 102, 103])
    assert sorted(i["producto_id"] for i in items) == [101, 103]
    assert total == 629800 + 55000


def test_seleccion_de_tres_indices_con_comas():
    history = [{"role": "user", "content": "1, 2 y 3"}]
    items, total = _resolver(history, [101, 102, 103])
    assert sorted(i["producto_id"] for i in items) == [101, 102, 103]
    assert total == 629800 + 29800 + 55000


def test_indices_fuera_de_rango_se_ignoran_sin_reventar():
    history = [{"role": "user", "content": "1 y 9"}]
    items, total = _resolver(history, [101, 102, 103])
    assert sorted(i["producto_id"] for i in items) == [101]
    assert total == 629800
