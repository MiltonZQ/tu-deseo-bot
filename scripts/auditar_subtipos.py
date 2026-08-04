"""Cuánta cobertura real tiene cada subtipo en el catálogo. SOLO LECTURA.

Antes de hacer estricto el filtro por subtipo hay que saber cuáles se quedarían
en cero, porque en modo estricto un cero no muestra menos productos: pausa el
bot y abre un ticket. El CSV de `prompts/knowledge/` NO sirve para medir esto:
es un archivo de prompts congelado, sin descripciones y sin productos que el bot
sí muestra en producción (ventosa, playboy, sailor moon). La única fuente válida
es la tabla `productos`.

Uso:
    .venv/bin/python scripts/auditar_subtipos.py
"""
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from app import catalog, db, facetas  # noqa: E402

SQL = """
SELECT nombre, descripcion, tipo, zona, atributos
FROM productos
WHERE (stock_status IS NULL OR stock_status <> 'outofstock')
  AND imagen_url IS NOT NULL AND imagen_url != ''
"""


def _cumple_facetas(producto: dict, restricciones: dict) -> bool:
    if not restricciones:
        return False
    if restricciones.get("tipo") and producto.get("tipo") != restricciones["tipo"]:
        return False
    if restricciones.get("zona") and producto.get("zona") != restricciones["zona"]:
        return False
    atributos = producto.get("atributos") or []
    return all(a in atributos for a in (restricciones.get("atributos") or []))


async def main() -> None:
    try:
        await db.init_pool()
    except Exception as e:
        # `DATABASE_URL` apunta al host `db`, el nombre del servicio dentro de
        # la red de Docker/Coolify: desde un portátil no resuelve. Hay que
        # correr esto DENTRO del contenedor de la app, o con un túnel al
        # Postgres. Medir con el CSV de prompts/knowledge no es alternativa:
        # está congelado y da 27 subtipos en cero que sí existen en la DB.
        print(f"No se pudo conectar a la DB ({type(e).__name__}: {e}).\n"
              "Corre esto donde el host de DATABASE_URL resuelva — dentro del\n"
              "contenedor de la app en Coolify, o con un túnel a Postgres.",
              file=sys.stderr)
        raise SystemExit(1)
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        filas = [dict(r) for r in await conn.fetch(SQL)]
    con_desc = sum(1 for f in filas if (f.get("descripcion") or "").strip())
    pct = 100 * con_desc // max(len(filas), 1)
    print(f"Productos ofrecibles: {len(filas)}")
    print(f"Con descripción no vacía: {con_desc} ({pct}%)\n")

    lineas = ["| subtipo | por nombre | por faceta | soft | veredicto |",
              "|---|---|---|---|---|"]
    sin_cobertura = []
    for s in catalog._SUBTIPO_KEYWORDS:
        por_nombre = len(catalog._filtrar_por_subtipo(filas, s))
        # Qué entiende el sistema de facetas si el cliente escribe solo esto.
        interpretado = facetas.interpretar_mensaje(s) or {}
        por_faceta = sum(1 for f in filas if _cumple_facetas(f, interpretado))
        soft = catalog._es_subtipo_soft(s)
        if por_nombre or soft:
            veredicto = "ok"
        elif por_faceta:
            veredicto = "FACETA CUBRE"
        else:
            veredicto = "SIN COBERTURA"
            sin_cobertura.append(s)
        lineas.append(
            f"| `{s}` | {por_nombre} | {por_faceta} | {soft} | {veredicto} |")

    informe = "\n".join(lineas)
    print(informe)
    print(f"\nSIN COBERTURA (escalarían en modo estricto): {len(sin_cobertura)}")
    for s in sin_cobertura:
        print(f"  - {s!r}")

    destino = _ROOT / "docs" / "auditoria-subtipos.md"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(
        "# Auditoría de cobertura por subtipo\n\n"
        "Generado por `scripts/auditar_subtipos.py` contra la DB real.\n\n"
        f"Productos ofrecibles: {len(filas)} — con descripción no vacía: "
        f"{con_desc} ({pct}%)\n\n"
        + informe
        + "\n\n## Sin cobertura\n\n"
        + ("\n".join(f"- `{s}`" for s in sin_cobertura) or "_ninguno_")
        + "\n")
    print(f"\nEscrito: {destino}")


if __name__ == "__main__":
    asyncio.run(main())
