"""Recalcular las facetas de todo el catálogo.

Las reglas de `app/facetas.py` solo afectan a los productos ya cargados cuando
se reescriben sus facetas. Antes esto solo se podía hacer con
`scripts/clasificar_catalogo.py`, es decir con acceso al contenedor; ahora el
mismo núcleo lo usa también el endpoint de mantenimiento, para que un cambio de
reglas no dependa de tener una shell.

Respeta `revisado_por_humano`: lo corregido a mano desde el panel no se toca
(la barrera está en `db.set_facetas_producto`).
"""
from __future__ import annotations

import logging

from app import db, facetas

log = logging.getLogger("tu-deseo-bot")


def _comparable(f: facetas.Facetas) -> tuple:
    return (f.tipo, f.zona, bool(f.vibra), f.control, f.genero_uso,
            tuple(sorted(f.atributos)))


def _actual(producto: dict) -> tuple:
    return (producto.get("tipo"), producto.get("zona") or "ninguna",
            bool(producto.get("vibra")), producto.get("control") or "ninguno",
            producto.get("genero_uso") or "unisex",
            tuple(sorted(producto.get("atributos") or [])))


async def reclasificar_catalogo(dry_run: bool = False, solo_nuevos: bool = False,
                                permitir_llm: bool = True,
                                limite_cambios: int = 400) -> dict:
    """Recalcula y (salvo dry_run) guarda las facetas de todo el catálogo.

    Devuelve un resumen con los cambios producto a producto, para poder revisar
    el efecto ANTES de escribir. Es la comprobación que hace falta cuando se
    tocan las reglas: un cambio pensado para lubricantes no debería mover ni un
    vibrador, y aquí se ve.
    """
    sql = ("SELECT id, nombre, descripcion, categoria, tipo, zona, vibra, control, "
           "genero_uso, atributos, revisado_por_humano FROM productos")
    if solo_nuevos:
        sql += " WHERE tipo IS NULL"
    sql += " ORDER BY nombre"
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        productos = [dict(r) for r in await conn.fetch(sql)]

    cambios: list[dict] = []
    por_origen = {"reglas": 0, "llm": 0}
    sin_clasificar = 0
    protegidos = 0

    for p in productos:
        f, origen = await facetas.clasificar(
            p["nombre"], p.get("descripcion"), p.get("categoria"),
            permitir_llm=permitir_llm)
        if not f.tipo:
            sin_clasificar += 1
            continue
        por_origen[origen] = por_origen.get(origen, 0) + 1

        antes, despues = _actual(p), _comparable(f)
        if antes != despues:
            if p.get("revisado_por_humano"):
                # La escritura la bloquearía la barrera de set_facetas_producto;
                # se cuenta aparte para que el informe no mienta.
                protegidos += 1
            elif len(cambios) < limite_cambios:
                campos = [c for c, a, d in zip(
                    ("tipo", "zona", "vibra", "control", "genero_uso", "atributos"),
                    antes, despues) if a != d]
                cambios.append({"id": p["id"], "nombre": p["nombre"][:60],
                                "campos": campos,
                                "antes": dict(zip(("tipo", "zona", "vibra", "control",
                                                   "genero_uso", "atributos"), antes)),
                                "despues": f.as_dict()})
        if not dry_run:
            await db.set_facetas_producto(p["id"], f, origen=origen)

    log.info("Reclasificación%s: %d productos, %d cambios, %d sin clasificar",
             " (dry-run)" if dry_run else "", len(productos), len(cambios), sin_clasificar)
    return {
        "dry_run": dry_run,
        "total": len(productos),
        "por_reglas": por_origen.get("reglas", 0),
        "por_llm": por_origen.get("llm", 0),
        "sin_clasificar": sin_clasificar,
        "protegidos_por_revision_humana": protegidos,
        "cambios": len(cambios),
        "detalle": cambios,
    }
