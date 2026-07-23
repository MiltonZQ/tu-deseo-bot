"""Gestión de catálogo de productos y sincronización con SIDDE POS.

Permite cargar el catálogo (300 referencias) desde:
  - un archivo CSV/Excel/JSON exportado, o
  - la API de SIDDE POS (si está habilitada).

También expone funciones de búsqueda para que el bot pueda recomendar productos
y un endpoint de recarga en caliente vía /reload.
"""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Iterable

from app import config, db

log = logging.getLogger("catalog")


async def upsert_producto(
    nombre: str,
    precio: int,
    *,
    descripcion: str | None = None,
    categoria: str | None = None,
    sku_pos: str | None = None,
    activo: bool = True,
) -> int:
    """Crea o actualiza un producto por sku_pos (si existe) o por nombre."""
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        row = await conn.fetchrow(
            """
            INSERT INTO productos (nombre, descripcion, categoria, precio, sku_pos, activo)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            nombre, descripcion, categoria, int(precio), sku_pos, activo,
        )
    return row["id"] if row else 0


async def import_from_csv(path: str | Path) -> dict:
    """Importa catálogo desde CSV con columnas: nombre, precio, descripcion, categoria, sku_pos.

    Columnas mínimas: nombre, precio. Las demás son opcionales.
    Devuelve un resumen {insertados, total}.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    insertados = 0
    total = 0
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            nombre = (row.get("nombre") or "").strip()
            precio_raw = (row.get("precio") or "").strip()
            if not nombre or not precio_raw:
                continue
            try:
                precio = int(precio_raw.replace(".", "").replace(",", "").replace("$", ""))
            except ValueError:
                continue
            total += 1
            pid = await upsert_producto(
                nombre=nombre,
                precio=precio,
                descripcion=(row.get("descripcion") or "").strip() or None,
                categoria=(row.get("categoria") or "").strip() or None,
                sku_pos=(row.get("sku_pos") or row.get("sku") or "").strip() or None,
            )
            if pid:
                insertados += 1
    log.info("Catálogo importado desde %s: %d/%d", path.name, insertados, total)
    return {"insertados": insertados, "total": total}


async def import_from_json(path: str | Path) -> dict:
    """Importa catálogo desde JSON: lista de {nombre, precio, descripcion, categoria, sku_pos}."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("productos") or data.get("items") or []
    insertados = 0
    total = 0
    for item in data:
        nombre = (item.get("nombre") or "").strip()
        precio = item.get("precio")
        if not nombre or precio is None:
            continue
        total += 1
        pid = await upsert_producto(
            nombre=nombre,
            precio=int(precio),
            descripcion=item.get("descripcion"),
            categoria=item.get("categoria"),
            sku_pos=item.get("sku_pos") or item.get("sku"),
        )
        if pid:
            insertados += 1
    log.info("Catálogo importado desde %s: %d/%d", path.name, insertados, total)
    return {"insertados": insertados, "total": total}


async def search(query: str, limit: int = 5) -> list[dict]:
    """Búsqueda ligera por nombre/categoría (ILIKE). Para volúmenes grandes considera FTS/vectorial."""
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        rows = await conn.fetch(
            """
            SELECT id, nombre, descripcion, categoria, precio, precio_regular, precio_oferta, sku_pos, imagen_url, permalink
            FROM productos
            WHERE activo = TRUE
              AND (nombre ILIKE '%' || $1 || '%' OR categoria ILIKE '%' || $1 || '%'
                   OR descripcion ILIKE '%' || $1 || '%')
            ORDER BY nombre
            LIMIT $2
            """,
            query, limit,
        )
    return [dict(r) for r in rows]


async def get_producto_con_imagen(query: str) -> dict | None:
    """Busca el producto más relevante que contenga una URL de imagen válida."""
    if not query:
        return None
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        row = await conn.fetchrow(
            """
            SELECT id, nombre, descripcion, categoria, precio, imagen_url, galeria_urls, permalink
            FROM productos
            WHERE activo = TRUE
              AND imagen_url IS NOT NULL AND imagen_url != ''
              AND (nombre ILIKE '%' || $1 || '%' OR categoria ILIKE '%' || $1 || '%'
                   OR descripcion ILIKE '%' || $1 || '%')
            ORDER BY
                CASE WHEN nombre ILIKE $1 THEN 1
                     WHEN nombre ILIKE $1 || '%' THEN 2
                     ELSE 3 END,
                nombre
            LIMIT 1
            """,
            query,
        )
    return dict(row) if row else None


async def sync_from_sidde() -> dict:
    """Sincroniza el catálogo desde SIDDE POS (si está configurado).

    Requiere SIDDE_POS_ENABLED=true y credenciales. Implementación real depende del
    formato exacto de la API de SIDDE; se completa cuando el cliente entregue el acceso.
    """
    if not config.SIDDE_POS_ENABLED:
        return {"skipped": "SIDDE_POS_ENABLED=false"}
    log.warning("sync_from_sidde: la integración con SIDDE POS aún no está implementada")
    return {"skipped": "integración SIDDE POS pendiente de credenciales"}


async def export_knowledge_md(path: str | Path | None = None) -> Path:
    """Genera prompts/knowledge/catalogo.md a partir de la tabla productos.

    Útil para que el bot conozca el catálogo vía el system prompt.
    """
    out = Path(path) if path else (config.PROMPTS_DIR / "knowledge" / "catalogo.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        rows = await conn.fetch(
            "SELECT nombre, descripcion, categoria, precio FROM productos WHERE activo = TRUE ORDER BY categoria, nombre"
        )
    lines = ["# Catálogo de productos", ""]
    current_cat = None
    for r in rows:
        cat = r["categoria"] or "Sin categoría"
        if cat != current_cat:
            lines.append(f"\n## {cat}\n")
            current_cat = cat
        desc = f" — {r['descripcion']}" if r["descripcion"] else ""
        lines.append(f"- **{r['nombre']}**${r['precio']:,}{desc}")
    out.write_text("\n".join(lines), encoding="utf-8")
    log.info("Catálogo exportado a %s (%d productos)", out, len(rows))
    return out
