"""Bug reportado el 2026-08-02: 'Pido dizfraz colegiala' devolvía lencería
genérica en vez del disfraz pedido.

Causa raíz doble:
  1. El typo "dizfraz" nunca se corregía a "disfraz" (no estaba en _ALIASES_TYPO).
  2. Aunque se escribiera bien, "disfraz" era el único subtipo reconocido: no
     había forma de distinguir "colegiala" de "policía" o "enfermera", así que
     todos los disfraces del catálogo empataban en el ranking.
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

from app import catalog, db, openai_client  # noqa: E402


async def _sin_llm(_texto, _history=None):
    return None


openai_client.clasificar_intencion_llm = _sin_llm


def _clasificar(user_text, history=None, estado=None):
    return asyncio.run(
        catalog.clasificar_intencion_cliente(user_text, history or [], estado))


def test_dizfraz_se_corrige_a_disfraz():
    assert catalog._corregir_typos("dizfraz colegiala") == "disfraz colegiala"


def test_colegiala_se_detecta_como_subtipo_propio():
    r = _clasificar("Pido dizfraz colegiala")
    assert r["categoria_funcional"] == "lenceria"
    assert r["subtipo_detectado"] == "colegiala"


def test_cada_disfraz_real_del_catalogo_tiene_subtipo_propio():
    casos = {
        "quiero el disfraz de enfermera": "enfermera",
        "tienen disfraz de policia": "policia",
        "el disfraz de conejita": "conejita",
        "disfraz de mucama": "mucama",
        "disfraz playboy": "playboy",
        "disfraz de diabla": "diabla",
        "disfraz sailor moon": "sailor moon",
    }
    for msg, subtipo_esperado in casos.items():
        r = _clasificar(msg)
        assert r["subtipo_detectado"] == subtipo_esperado, f"{msg!r} → {r['subtipo_detectado']}"
        assert r["categoria_funcional"] == "lenceria", msg


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, *_a, **_k):
        return list(self._rows)

    async def fetchrow(self, *_a, **_k):
        return self._rows[0] if self._rows else None


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    def acquire(self):
        rows = self._rows

        class _Ctx:
            async def __aenter__(self):
                return _FakeConn(rows)

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


_DISFRACES_CATALOGO = [
    {"id": 1, "nombre": "Disfraz Colegiala Inocente Lerot",
     "descripcion": "Volver a ser colegiala siempre es una posibilidad.",
     "categoria": "Lenceria", "precio": 119800, "imagen_url": "http://x/1.jpg",
     "galeria_urls": None, "permalink": None},
    {"id": 2, "nombre": "Disfraz Policía Lerot",
     "descripcion": "Pon las reglas en la habitación con este Disfraz Policía Lerot.",
     "categoria": "Lenceria", "precio": 109800, "imagen_url": "http://x/2.jpg",
     "galeria_urls": None, "permalink": None},
    {"id": 3, "nombre": "Disfraz Enfermera Sexy Dulce Tentación",
     "descripcion": "Espectacular lencería sensual.",
     "categoria": "Lenceria", "precio": 109800, "imagen_url": "http://x/3.jpg",
     "galeria_urls": None, "permalink": None},
]


def _recomendar(**kwargs):
    original, db._pool = getattr(db, "_pool", None), _FakePool(_DISFRACES_CATALOGO)
    qdrant, catalog.config.QDRANT_ENABLED = catalog.config.QDRANT_ENABLED, False
    try:
        return asyncio.run(catalog.get_productos_para_recomendar(**kwargs))
    finally:
        db._pool = original
        catalog.config.QDRANT_ENABLED = qdrant


def test_colegiala_prioriza_el_disfraz_de_colegiala_sobre_los_demas():
    res = _recomendar(categoria_funcional="lenceria", genero=None,
                       user_text="dizfraz colegiala", subtipo="colegiala")
    assert res, "debe devolver al menos un producto"
    assert res[0]["nombre"] == "Disfraz Colegiala Inocente Lerot", [p["nombre"] for p in res]
