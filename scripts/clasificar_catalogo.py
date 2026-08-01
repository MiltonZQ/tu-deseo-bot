#!/usr/bin/env python
"""Calcula y guarda las facetas de todo el catálogo.

Las reglas resuelven la mayoría sin coste; el LLM entra solo en los productos que
las reglas no deciden. El resultado se guarda en la tabla `productos` y respeta
`revisado_por_humano`: lo que se corrigió a mano desde el panel no se toca.

Uso:
    python scripts/clasificar_catalogo.py --dry-run      # informe, sin escribir
    python scripts/clasificar_catalogo.py                # clasifica y guarda
    python scripts/clasificar_catalogo.py --solo-nuevos  # solo los que no tienen tipo
    python scripts/clasificar_catalogo.py --sin-llm      # solo reglas (sin coste)

Revisa el informe antes de dar por buena la clasificación: las líneas marcadas
con [LLM] y las de confianza baja son las que conviene mirar con calma.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, facetas  # noqa: E402


async def _cargar(solo_nuevos: bool) -> list[dict]:
    sql = "SELECT id, nombre, descripcion, categoria FROM productos"
    if solo_nuevos:
        sql += " WHERE tipo IS NULL"
    sql += " ORDER BY nombre"
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        return [dict(r) for r in await conn.fetch(sql)]


async def main(dry_run: bool, solo_nuevos: bool, sin_llm: bool) -> int:
    await db.init_pool()
    productos = await _cargar(solo_nuevos)
    if not productos:
        print("No hay productos que clasificar.")
        return 0

    print(f"Clasificando {len(productos)} productos"
          f"{' (solo reglas)' if sin_llm else ''}"
          f"{' — SIN ESCRIBIR' if dry_run else ''}…\n")

    resultados: list[tuple[dict, facetas.Facetas, str]] = []
    for p in productos:
        f, origen = await facetas.clasificar(
            p["nombre"], p.get("descripcion"), p.get("categoria"),
            permitir_llm=not sin_llm,
        )
        resultados.append((p, f, origen))
        if not dry_run and f.tipo:
            await db.set_facetas_producto(p["id"], f, origen=origen)

    # ── Informe agrupado, para revisar de un vistazo ──
    por_tipo: dict[str, list] = collections.defaultdict(list)
    for p, f, origen in resultados:
        por_tipo[f.tipo or "SIN CLASIFICAR"].append((p, f, origen))

    for tipo in sorted(por_tipo, key=lambda t: (t == "SIN CLASIFICAR", t)):
        items = por_tipo[tipo]
        print(f"\n── {tipo}  ({len(items)}) " + "─" * max(0, 46 - len(tipo)))
        for p, f, origen in sorted(items, key=lambda x: x[0]["nombre"]):
            marca = "[LLM]" if origen == "llm" else "     "
            vib = "vibra" if f.vibra else "     "
            attrs = ",".join(f.atributos[:3])
            print(f"  {marca} {f.zona:9} {vib} {f.genero_uso:7} "
                  f"{f.control:7} {p['nombre'][:44]:46} {attrs}")

    n_llm = sum(1 for _, _, o in resultados if o == "llm")
    n_sin = sum(1 for _, f, _ in resultados if not f.tipo)
    print("\n" + "═" * 78)
    print(f"total {len(resultados)}   por reglas {len(resultados) - n_llm}   "
          f"por LLM {n_llm}   sin clasificar {n_sin}")
    print("por zona: " + "  ".join(
        f"{z}={n}" for z, n in collections.Counter(
            f.zona for _, f, _ in resultados).most_common()))
    print(f"vibran: {sum(1 for _, f, _ in resultados if f.vibra)}")
    if dry_run:
        print("\nDRY-RUN: no se escribió nada. Quita --dry-run para guardar.")
    return n_sin


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="solo informe, no escribe")
    ap.add_argument("--solo-nuevos", action="store_true", help="solo los que no tienen tipo")
    ap.add_argument("--sin-llm", action="store_true", help="solo reglas, sin llamadas al LLM")
    args = ap.parse_args()
    sys.exit(0 if asyncio.run(
        main(args.dry_run, args.solo_nuevos, args.sin_llm)) == 0 else 0)
