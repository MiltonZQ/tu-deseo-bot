"""Regresión: los productos mostrados deben persistirse también en el INSERT.

Bug observado el 2026-08-01 tras vaciar `conversation_state`: el cliente pedía
"ver más" y recibía otra vez las fotos de la primera ronda. Los logs del servidor
mostraban `Filtro final [...]: ya_mostrados=0 ([])` justo después de haber
enviado 5 fotos 80 segundos antes.

Causa raíz: `upsert_conversation_state` construía

    INSERT INTO conversation_state (wa_id, calificado, productos_mostrados, updated_at)
    VALUES ($1, FALSE, '{}', now())
    ON CONFLICT (wa_id) DO UPDATE SET <campos>

Los `<campos>` solo corren si hay conflicto. En el PRIMER turno de un contacto no
existe fila, así que se ejecuta el INSERT y los ids se pierden: se guarda `'{}'`.
El filtro anti-repetición del turno siguiente se queda sin datos con los que
filtrar. Afecta a todo contacto nuevo, y volvió a aparecer al borrar la tabla.

Estos tests capturan el SQL y los parámetros que se le pasan a asyncpg, sin
necesidad de una base de datos real.
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

from app import db  # noqa: E402


class _ConnEspia:
    """Captura el SQL y los parámetros de cada execute()."""

    def __init__(self):
        self.llamadas: list[tuple[str, tuple]] = []

    async def execute(self, sql, *params):
        self.llamadas.append((" ".join(sql.split()), params))
        return "INSERT 0 1"


class _PoolEspia:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


def _upsert(**kwargs) -> tuple[str, tuple]:
    conn = _ConnEspia()
    original, db._pool = getattr(db, "_pool", None), _PoolEspia(conn)
    try:
        asyncio.run(db.upsert_conversation_state("573001112233", **kwargs))
    finally:
        db._pool = original
    assert conn.llamadas, "no se ejecutó ninguna query"
    return conn.llamadas[-1]


def test_el_insert_lleva_los_productos_mostrados():
    """El caso del bug: contacto nuevo, sin fila previa."""
    sql, params = _upsert(add_productos_mostrados=[31303, 31320, 31329])
    cabecera = sql.split("ON CONFLICT")[0]
    assert "productos_mostrados" in cabecera, (
        "productos_mostrados debe ir en las columnas del INSERT; si solo está en el "
        f"ON CONFLICT, el primer turno de cada contacto pierde los ids.\nSQL: {sql}")
    assert "'{}'" not in cabecera, (
        f"el INSERT no puede escribir un array vacío fijo.\nSQL: {sql}")
    assert [31303, 31320, 31329] in [list(p) if isinstance(p, list) else p for p in params]


def test_el_update_sigue_acumulando_sin_duplicados():
    """El turno 2 en adelante debe UNIR con lo ya guardado, no reemplazar."""
    sql, _ = _upsert(add_productos_mostrados=[999])
    conflicto = sql.split("ON CONFLICT")[1]
    assert "conversation_state.productos_mostrados ||" in conflicto, (
        f"el UPDATE debe acumular sobre lo existente.\nSQL: {sql}")
    assert "DISTINCT" in conflicto, f"el UPDATE debe deduplicar.\nSQL: {sql}"


def test_el_insert_lleva_categoria_genero_y_calificado():
    """Mismo fallo para el resto de campos: un contacto nuevo perdía la categoría."""
    sql, params = _upsert(categoria_busqueda="bombas para el pene",
                          categoria_funcional="bombas-pene",
                          genero="hombre", calificado=True)
    cabecera = sql.split("ON CONFLICT")[0]
    for col in ("categoria_busqueda", "categoria_funcional", "genero", "calificado"):
        assert col in cabecera, f"{col} debe ir en el INSERT.\nSQL: {sql}"
    assert "FALSE" not in cabecera.upper().replace("DEFAULT", ""), (
        f"el INSERT no puede fijar calificado=FALSE ignorando el valor recibido.\nSQL: {sql}")
    assert "bombas-pene" in params and "hombre" in params and True in params


def test_los_placeholders_coinciden_con_los_parametros():
    """Cada $N del SQL debe tener su parámetro: un desfase rompe el execute entero."""
    import re
    sql, params = _upsert(categoria_funcional="lubricantes-y-cuidado",
                          genero="mujer", calificado=True,
                          add_productos_mostrados=[1, 2])
    usados = {int(n) for n in re.findall(r"\$(\d+)", sql)}
    assert usados == set(range(1, len(params) + 1)), (
        f"placeholders {sorted(usados)} vs {len(params)} parámetros.\nSQL: {sql}")


def test_reset_sigue_vaciando_el_estado():
    """No romper el camino de reset (cambio de tema)."""
    sql, params = _upsert(reset=True)
    assert "productos_mostrados = '{}'" in sql, f"el reset debe vaciar la lista.\nSQL: {sql}"
    assert params == ("573001112233",)


# ── Facetas de producto ──
# El sync de WooCommerce corre periódicamente y reclasifica. Si pisara las
# correcciones hechas a mano desde el panel, el operador tendría que volver a
# corregir el mismo producto después de cada sync y perdería la confianza en el
# panel. Por eso `revisado_por_humano` es una barrera dura.

def _set_facetas(**kwargs) -> tuple[str, tuple]:
    from app import facetas as F
    conn = _ConnEspia()
    original, db._pool = getattr(db, "_pool", None), _PoolEspia(conn)
    try:
        f = F.Facetas(tipo="plug", zona="anal", vibra=True, control="app",
                      genero_uso="unisex", atributos=["silicona"])
        asyncio.run(db.set_facetas_producto(123, f, **kwargs))
    finally:
        db._pool = original
    assert conn.llamadas, "no se ejecutó ninguna query"
    return conn.llamadas[-1]


def test_el_sync_no_pisa_una_correccion_manual():
    sql, _ = _set_facetas(origen="reglas")
    assert "revisado_por_humano" in sql and "FALSE" in sql.upper(), (
        f"el UPDATE automático debe excluir los productos revisados a mano.\nSQL: {sql}")


def test_la_correccion_manual_si_puede_escribir_siempre():
    sql, _ = _set_facetas(origen="manual")
    cuerpo = sql.split("WHERE")[1] if "WHERE" in sql else ""
    assert "revisado_por_humano = FALSE" not in cuerpo.replace("  ", " "), (
        f"una edición manual no puede bloquearse a sí misma.\nSQL: {sql}")
    assert "revisado_por_humano = TRUE" in sql, (
        f"editar a mano debe marcar el producto como revisado.\nSQL: {sql}")


def test_se_guardan_todas_las_facetas():
    sql, params = _set_facetas(origen="reglas")
    for col in ("tipo", "zona", "vibra", "control", "genero_uso", "atributos",
                "clasificado_por"):
        assert col in sql, f"falta {col}.\nSQL: {sql}"
    assert "plug" in params and "anal" in params and True in params
