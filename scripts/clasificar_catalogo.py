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

from app import clasificacion, db  # noqa: E402


async def main(dry_run: bool, solo_nuevos: bool, sin_llm: bool) -> int:
    """Envoltorio de consola sobre `app.clasificacion.reclasificar_catalogo`.

    El núcleo vive en la app porque también lo usa el endpoint de mantenimiento
    `/maintenance/reclasificar-facetas` (útil cuando no hay shell en el
    contenedor). Aquí solo se imprime el informe.
    """
    await db.init_pool()
    res = await clasificacion.reclasificar_catalogo(
        dry_run=dry_run, solo_nuevos=solo_nuevos, permitir_llm=not sin_llm)

    if not res["total"]:
        print("No hay productos que clasificar.")
        return 0

    print(f"Clasificando {res['total']} productos"
          f"{' (solo reglas)' if sin_llm else ''}"
          f"{' — SIN ESCRIBIR' if dry_run else ''}…\n")

    if res["detalle"]:
        print(f"── CAMBIOS ({res['cambios']}) " + "─" * 50)
        por_campo: dict[str, int] = collections.defaultdict(int)
        for c in res["detalle"]:
            for campo in c["campos"]:
                por_campo[campo] += 1
            cambios_txt = ", ".join(
                f"{campo}: {c['antes'][campo]!r}→{c['despues'][campo]!r}"
                for campo in c["campos"])
            print(f"  {c['nombre'][:46]:48} {cambios_txt}")
        print("\n  por campo: " + "  ".join(f"{k}={v}" for k, v in
                                            sorted(por_campo.items())))
    else:
        print("Sin cambios respecto a lo que ya está guardado.")

    print("\n" + "=" * 78)
    print(f"total {res['total']}   por reglas {res['por_reglas']}   "
          f"por LLM {res['por_llm']}   sin clasificar {res['sin_clasificar']}   "
          f"protegidos por revisión humana {res['protegidos_por_revision_humana']}")
    if dry_run:
        print("\nDRY-RUN: no se escribió nada. Quita --dry-run para guardar.")
    return res["sin_clasificar"]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="solo informe, no escribe")
    ap.add_argument("--solo-nuevos", action="store_true", help="solo los que no tienen tipo")
    ap.add_argument("--sin-llm", action="store_true", help="solo reglas, sin llamadas al LLM")
    args = ap.parse_args()
    sys.exit(0 if asyncio.run(
        main(args.dry_run, args.solo_nuevos, args.sin_llm)) == 0 else 0)
