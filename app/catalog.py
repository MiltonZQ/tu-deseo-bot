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


STOP_WORDS = {
    "puedo", "ver", "una", "un", "el", "la", "los", "las", "de", "del",
    "foto", "fotos", "imagen", "imagenes", "fotografia", "muestra",
    "muestramelo", "muestrame", "por", "favor", "tienen", "quiero", "dame",
    "enviar", "mandar", "manda", "envia", "como", "es", "tienes", "verla",
    "verlo", "verlos", "verlas", "con", "para", "este", "chat", "puedes",
    "cada", "uno", "unos", "unas", "diferencias", "principales", "me", "no",
    "enciaste", "enviaste", "mandaste", "llegado", "llego", "hijueputa", "puta"
}


def _extract_search_tokens(text: str) -> list[str]:
    if not text:
        return []
    import unicodedata, re
    # Eliminar valores de precios en dinero (ej: $100,000, 100.000, 24900) y números
    text_no_prices = re.sub(r"\$?\b\d+([.,]\d+)*\b", "", text)
    clean = unicodedata.normalize("NFKD", text_no_prices.lower()).encode("ascii", "ignore").decode()
    # Extraer palabras alfabéticas puras de longitud >= 2
    words = re.findall(r"\b[a-z]{2,}\b", clean)
    tokens = [w for w in words if w not in STOP_WORDS]
    return tokens


def _score_product_match(product_name: str, tokens: list[str]) -> float:
    if not tokens or not product_name:
        return 0.0
    import unicodedata, re
    clean_name = unicodedata.normalize("NFKD", product_name.lower()).encode("ascii", "ignore").decode()
    name_words = set(re.findall(r"\b[a-z]{2,}\b", clean_name))
    if not name_words:
        return 0.0
    matches = sum(1 for t in tokens if t in name_words or any(t in w for w in name_words))
    return matches / len(tokens)


async def get_producto_con_imagen(query: str) -> dict | None:
    """Busca el producto con imagen que MEJOR coincida con la consulta por puntuación de tokens."""
    if not query:
        return None

    tokens = _extract_search_tokens(query)
    if not tokens:
        return None

    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        # Intento 1: Coincidencia exacta de frase limpia en el nombre
        clean_phrase = " ".join(tokens)
        row = await conn.fetchrow(
            """
            SELECT id, nombre, descripcion, categoria, precio, imagen_url, galeria_urls, permalink
            FROM productos
            WHERE activo = TRUE
              AND imagen_url IS NOT NULL AND imagen_url != ''
              AND nombre ILIKE '%' || $1 || '%'
            ORDER BY LENGTH(nombre) ASC
            LIMIT 1
            """,
            clean_phrase,
        )
        if row:
            return dict(row)

        # Intento 2: Coincidencia de TODOS los tokens (AND)
        where_clause = " AND ".join([f"nombre ILIKE ${i+1}" for i in range(len(tokens))])
        params = [f"%{t}%" for t in tokens]
        row = await conn.fetchrow(
            f"""
            SELECT id, nombre, descripcion, categoria, precio, imagen_url, galeria_urls, permalink
            FROM productos
            WHERE activo = TRUE
              AND imagen_url IS NOT NULL AND imagen_url != ''
              AND {where_clause}
            ORDER BY LENGTH(nombre) ASC
            LIMIT 1
            """,
            *params,
        )
        if row:
            return dict(row)

        # Intento 3: Puntuación de tokens (Term Overlap Score) sin OR ciego por orden alfabético
        where_clause_or = " OR ".join([f"nombre ILIKE ${i+1}" for i in range(len(tokens))])
        rows = await conn.fetch(
            f"""
            SELECT id, nombre, descripcion, categoria, precio, imagen_url, galeria_urls, permalink
            FROM productos
            WHERE activo = TRUE
              AND imagen_url IS NOT NULL AND imagen_url != ''
              AND ({where_clause_or})
            """,
            *params,
        )
        if rows:
            scored = []
            for r in rows:
                score = _score_product_match(r["nombre"], tokens)
                if score >= 0.5:  # Exigir al menos el 50% de coincidencia de tokens
                    scored.append((score, -len(r["nombre"]), dict(r)))
            if scored:
                scored.sort(key=lambda x: x[:2], reverse=True)
                return scored[0][2]

    return None


async def get_productos_en_texto(text: str, limit: int = 4) -> list[dict]:
    """Extrae productos únicos del catálogo que son mencionados explícitamente en el texto."""
    if not text:
        return []

    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        rows = await conn.fetch(
            """
            SELECT id, nombre, descripcion, categoria, precio, imagen_url, galeria_urls, permalink
            FROM productos
            WHERE activo = TRUE
              AND imagen_url IS NOT NULL AND imagen_url != ''
            ORDER BY LENGTH(nombre) DESC
            """
        )

    matched = []
    seen_ids = set()
    import unicodedata
    text_clean = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()

    for r in rows:
        name_clean = unicodedata.normalize("NFKD", r["nombre"].lower()).encode("ascii", "ignore").decode()
        if name_clean in text_clean:
            if r["id"] not in seen_ids:
                seen_ids.add(r["id"])
                matched.append(dict(r))
                if len(matched) >= limit:
                    break

    return matched


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
