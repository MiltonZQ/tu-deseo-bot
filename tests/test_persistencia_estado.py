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


# ── Rondas y carrito ──
#
# `productos_mostrados` es un conjunto sin orden garantizado: se acumula con
# `ARRAY(SELECT DISTINCT unnest(...))` y Postgres puede reordenar. Sirve para el
# filtro anti-repetición (pertenencia), no para decir "el segundo". Las `rondas`
# guardan ESE dato: qué se envió, en qué orden y bajo qué categoría, de forma que
# "el 2" tenga un ámbito acotado y visible en vez de un índice sobre el acumulado.

def test_la_ronda_se_anade_en_las_dos_ramas_del_upsert():
    """Mismo fallo que tenía productos_mostrados: si la ronda solo estuviera en el
    ON CONFLICT, la primera ronda de cada contacto nuevo se perdería."""
    sql, params = _upsert(add_ronda={"categoria": "disfraces", "ids": [12, 45]})
    cabecera = sql.split("ON CONFLICT")[0]
    assert "rondas" in cabecera, (
        f"rondas debe ir en las columnas del INSERT.\nSQL: {sql}")
    assert "'[]'" not in cabecera, (
        f"el INSERT no puede escribir un array vacío fijo.\nSQL: {sql}")
    assert any("disfraces" in str(p) for p in params), params


def test_las_rondas_se_acumulan_conservando_el_orden():
    """El orden es el dato: 'el 2' de la última ronda depende de él. Por eso se
    concatena el array JSONB en vez de deduplicar como productos_mostrados."""
    sql, _ = _upsert(add_ronda={"categoria": "juegos", "ids": [7]})
    conflicto = sql.split("ON CONFLICT")[1]
    assert "conversation_state.rondas ||" in conflicto, (
        f"el UPDATE debe acumular sobre las rondas existentes.\nSQL: {sql}")
    assert "ORDER BY ord" in conflicto, (
        f"la reagregación debe fijar el orden explícitamente; sin ORDER BY, "
        f"Postgres no lo garantiza y 'el 2' pasa a ser una lotería.\nSQL: {sql}")


def test_solo_se_conservan_las_ultimas_rondas():
    """Más allá de unas pocas rondas el cliente ya no se acuerda, y el conjunto
    sobre el que se resuelve una referencia debe seguir siendo pequeño."""
    sql, _ = _upsert(add_ronda={"categoria": "juegos", "ids": [7]})
    conflicto = sql.split("ON CONFLICT")[1]
    assert "jsonb_array_length" in conflicto, (
        f"el UPDATE debe podar las rondas viejas.\nSQL: {sql}")


def test_el_carrito_se_escribe_en_las_dos_ramas():
    carrito = [{"producto_id": 12, "nombre": "Disfraz Policia", "precio": 40000}]
    sql, params = _upsert(carrito=carrito)
    cabecera = sql.split("ON CONFLICT")[0]
    assert "carrito" in cabecera, f"carrito debe ir en el INSERT.\nSQL: {sql}"
    # Se serializa con json.dumps, igual que `restricciones`: llega como texto
    # con los no-ASCII escapados y Postgres lo normaliza al parsear el JSONB.
    assert any("Disfraz Policia" in str(p) and "40000" in str(p) for p in params), params


def test_el_reset_no_toca_ni_las_rondas_ni_el_carrito():
    """El caso multi-categoría entero depende de esto.

    El reset por cambio de tema limpia `productos_mostrados` porque es la lista
    de EXCLUSIÓN: arrastrarla haría que productos válidos del tema nuevo se
    filtraran como "ya vistos".

    `rondas` no es eso: es la memoria de qué ha visto el cliente, y es contra lo
    que se resuelve "el disfraz de policía" tres categorías después. Vaciarla al
    cambiar de tema eliminaría exactamente la capacidad de elegir un producto de
    cada categoría, que es el caso que motivó todo esto. Su tamaño ya lo acota la
    poda a MAX_RONDAS.

    Y el carrito menos: preguntar por otra categoría no es retractarse.
    """
    sql, _ = _upsert(reset=True)
    assert "productos_mostrados = '{}'" in sql, f"SQL: {sql}"
    assert "rondas" not in sql, (
        f"el reset NO puede vaciar las rondas: el cliente perdería la capacidad "
        f"de elegir algo que vio antes de cambiar de tema.\nSQL: {sql}")
    assert "carrito" not in sql, (
        f"el reset NO puede tocar el carrito.\nSQL: {sql}")


def test_los_placeholders_coinciden_con_rondas_y_carrito():
    import re
    sql, params = _upsert(categoria_funcional="disfraces",
                          add_productos_mostrados=[1, 2],
                          add_ronda={"categoria": "disfraces", "ids": [1, 2]},
                          carrito=[{"producto_id": 1, "nombre": "X", "precio": 100}])
    usados = {int(n) for n in re.findall(r"\$(\d+)", sql)}
    assert usados == set(range(1, len(params) + 1)), (
        f"placeholders {sorted(usados)} vs {len(params)} parámetros.\nSQL: {sql}")


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
