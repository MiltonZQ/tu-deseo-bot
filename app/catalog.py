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


async def search_with_stock(query: str, limit: int = 6) -> list[dict]:
    """Búsqueda RAG para inyectar al LLM: productos que coinciden con la consulta
    del cliente, con stock disponible, priorizando los que tienen imagen.

    A diferencia de search(): filtra por stock_status (solo disponibles), NO exige
    imagen_url (encuentra productos aunque falte foto), y ordena los que sí tienen
    imagen primero (para que el bot pueda enviar fotos). Usada para que el bot
    encuentre productos que NO están en el catalogo.md del prompt.
    """
    if not query or len(query.strip()) < 3:
        return []
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        rows = await conn.fetch(
            """
            SELECT id, nombre, descripcion, categoria, precio, imagen_url, stock_status
            FROM productos
            WHERE (stock_status IS NULL OR stock_status <> 'outofstock')
              AND (nombre ILIKE '%' || $1 || '%' OR categoria ILIKE '%' || $1 || '%'
                   OR descripcion ILIKE '%' || $1 || '%')
            ORDER BY (imagen_url IS NULL) ASC, LENGTH(nombre) ASC
            LIMIT $2
            """,
            query.strip(), limit,
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


async def get_productos_en_texto(text: str, limit: int = 3) -> list[dict]:
    """Extrae productos únicos del catálogo mencionados en el texto.

    Estrategia de dos niveles:
      1. Match literal: el nombre completo del producto aparece como subcadena.
      2. Match por cobertura de tokens del producto: si la mayoría (≥70%) de los
         tokens del nombre del producto (ej. 'blix', 'lubricante', 'h2o') están
         presentes en el texto.
    """
    if not text:
        return []

    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        rows = await conn.fetch(
            """
            SELECT id, nombre, descripcion, categoria, precio, imagen_url, galeria_urls, permalink
            FROM productos
            WHERE (stock_status IS NULL OR stock_status <> 'outofstock')
              AND imagen_url IS NOT NULL AND imagen_url != ''
            ORDER BY LENGTH(nombre) DESC
            """
        )

    import unicodedata
    text_clean = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    matched: list[dict] = []
    seen_ids: set[int] = set()

    # 1. Match literal
    for r in rows:
        name_clean = unicodedata.normalize("NFKD", r["nombre"].lower()).encode("ascii", "ignore").decode()
        if name_clean in text_clean and r["id"] not in seen_ids:
            seen_ids.add(r["id"])
            matched.append(dict(r))
            if len(matched) >= limit:
                return matched

    # 2. Match por cobertura de tokens del nombre del producto en el texto
    scored: list[tuple[float, dict]] = []
    for r in rows:
        if r["id"] in seen_ids:
            continue
        p_tokens = _extract_search_tokens(r["nombre"])
        if not p_tokens:
            continue
        matches = sum(1 for t in p_tokens if t in text_clean)
        coverage = matches / len(p_tokens)
        if coverage >= 0.7:  # Al menos el 70% de las palabras del producto aparecen en el texto
            scored.append((coverage, dict(r)))

    scored.sort(key=lambda x: (x[0], -len(x[1]["nombre"])), reverse=True)
    for cov, r in scored:
        if r["id"] not in seen_ids:
            seen_ids.add(r["id"])
            matched.append(r)
            if len(matched) >= limit:
                break

    return matched


# ── Capa de categorías funcional (normalización durable) ──
#
# Las categorías de origen (WooCommerce) están mal normalizadas: hay 25 categorías
# superpuestas, marcas como categorías (Calexotics, Sex Shop Bogota) y un cajón de
# sastre "Juguetes" con 42 productos de tipos muy distintos.
#
# Esta capa clasifica cada producto en una de ~10 categorías funcionales mediante
# reglas sobre nombre + categoría origen. Al vivir en runtime, sobrevive a las
# re-sincronizaciones de WooCommerce sin degradarse.

CATEGORIAS_FUNCIONALES = [
    "vibradores",
    "succionadores",
    "dildos",
    "anal",
    "masturbadores",
    "anillos-y-fundas",
    "pareja-y-bondage",
    "lubricantes-y-cuidado",
    "lenceria",
    "juegos-y-accesorios",
]

# (palabras clave en nombre/descripción, categoría funcional) — orden importa:
# se evalúa de arriba a abajo y la primera coincidencia gana.
_REGLAS_CATEGORIA = [
    # Higiene/cuidado (sin ambigüedad con juguetes)
    (("limpiador", "limpia juguete", "toallitas"), "lubricantes-y-cuidado"),
    # Lubricantes y cosmética íntima
    (("lubricant", "lubric", "estimulant", "retardant", "spray", "vela ", "aceite ",
      "friction", "estrechant", "booster", "serum", "crema "), "lubricantes-y-cuidado"),
    # Succionadores de clítoris (antes que vibradores genéricos)
    (("succionador", "suction", "air pulse", "succión de clítoris", "succio"), "succionadores"),
    # Anal: plugs, bolas anales, estimuladores de próstata, dilatadores, arneses
    (("plug", "anal", "prostat", "próstata", "bolas anal", "dilatador", "arnes",
      "arnés", "strap on", "strap-on", "cola ", "entrenamiento anal"), "anal"),
    # Masturbadores masculinos
    (("masturbador", "huevo masturb", "vagina "), "masturbadores"),
    # Dildos / consoladores (realistas, con ventosa, dobles)
    (("dildo", "consolador", "realista", "ventosa"), "dildos"),
    # Anillos y fundas para pene (incluye bombas de vacío)
    (("anillo", "funda", "bomba para", "bomba pene", "bomba automatic",
      "bomba automática", "potenciador"), "anillos-y-fundas"),
    # Vibradores (rabbit, bala, huevo vibr, tipo hitachi, panty vibr, app)
    (("vibrador", "vibr ", "rabbit", "bala vibr", "huevo vibr", "hitachi", "panty vibr",
      "con app", "control remoto", "con vibrac"), "vibradores"),
    # Lencería y disfraces
    (("body ", "baby doll", "babydoll", "conjunto ", "lencería", "lenceria",
      "disfra", "pantuflas", "pezonera", "ligero", "encaje", "suspensorio"), "lenceria"),
    # Bondage / BDSM / pareja
    (("bondage", "bdsm", "esposas", "antifaz", "amarre", "fusta", "latigo", "látigo",
      "kit ", "vendas", "mordaza"), "pareja-y-bondage"),
    # Juegos de mesa y accesorios varios
    (("juego", "jenga", "cartas", "dado", "dados"), "juegos-y-accesorios"),
]


def _normalizar_texto(texto: str | None) -> str:
    """ASCII sin acentos ni mayúsculas, para matching robusto."""
    if not texto:
        return ""
    import unicodedata
    norm = unicodedata.normalize("NFKD", texto.lower()).encode("ascii", "ignore").decode()
    return norm


def _categoria_normalizada(nombre: str, descripcion: str | None = "",
                           cat_origen: str | None = "") -> str:
    """Clasifica un producto en una categoría funcional mediante reglas de matching.

    Evalúa nombre + descripción + categoría origen contra _REGLAS_CATEGORIA.
    Devuelve la primera categoría funcional que coincida, o 'juegos-y-accesorios'
    como fallback (cajón de sastre explícito para lo no clasificable).
    """
    haystack = _normalizar_texto(f"{nombre or ''} {descripcion or ''} {cat_origen or ''}")
    if not haystack.strip():
        return "juegos-y-accesorios"
    for claves, cat_funcional in _REGLAS_CATEGORIA:
        for clave in claves:
            if clave in haystack:
                return cat_funcional
    # Mapeo por categoría origen para casos sin palabra clave en el nombre
    cat_o = _normalizar_texto(cat_origen)
    if "bondage" in cat_o:
        return "pareja-y-bondage"
    if "lencer" in cat_o:
        return "lenceria"
    if "cosmetica" in cat_o or "cosmética" in (cat_origen or "").lower():
        return "lubricantes-y-cuidado"
    return "juegos-y-accesorios"


async def get_producto_by_id(producto_id: int) -> dict | None:
    """Devuelve un producto por su ID (para resolver marcadores [FOTO:ID] del LLM)."""
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        row = await conn.fetchrow(
            """
            SELECT id, nombre, descripcion, categoria, precio, imagen_url, galeria_urls, permalink
            FROM productos
            WHERE id = $1 AND (stock_status IS NULL OR stock_status <> 'outofstock')
            """,
            producto_id,
        )
    return dict(row) if row else None


async def get_productos_por_categoria_origen(categoria: str, limit: int = 5) -> list[dict]:
    """Productos por la categoría de origen de WooCommerce (la columna 'categoria').

    A diferencia de get_productos_por_categoria (que usa la clasificación funcional
    normalizada), esta usa la categoría tal como viene de la web. Permite "dame los
    de Punto G" y obtener exactamente esos productos. Prioriza los que tienen imagen.
    """
    if not categoria:
        return []
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        rows = await conn.fetch(
            """
            SELECT id, nombre, descripcion, categoria, precio, imagen_url, galeria_urls, permalink
            FROM productos
            WHERE (stock_status IS NULL OR stock_status <> 'outofstock')
              AND categoria ILIKE '%' || $1 || '%'
            ORDER BY (imagen_url IS NULL) ASC, LENGTH(nombre) DESC
            LIMIT $2
            """,
            categoria.strip(), limit,
        )
    return [dict(r) for r in rows]


async def list_categorias() -> dict[str, int]:
    """Devuelve {categoria_funcional: cantidad} de productos activos con imagen."""
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        rows = await conn.fetch(
            "SELECT nombre, descripcion, categoria FROM productos WHERE activo = TRUE"
        )
    conteo: dict[str, int] = {c: 0 for c in CATEGORIAS_FUNCIONALES}
    for r in rows:
        cat = _categoria_normalizada(r["nombre"], r["descripcion"], r["categoria"])
        conteo[cat] = conteo.get(cat, 0) + 1
    # Excluir categorías vacías
    return {c: n for c, n in conteo.items() if n > 0}


async def get_productos_por_categoria(cat_funcional: str, limit: int = 6) -> list[dict]:
    """Productos representativos de una categoría funcional (con imagen si la hay).

    Prefiere productos CON imagen_url para que el bot pueda enviar fotos.
    """
    cat_norm = _normalizar_texto(cat_funcional).replace(" ", "-")
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        rows = await conn.fetch(
            """
            SELECT id, nombre, descripcion, categoria, precio, imagen_url, galeria_urls, permalink
            FROM productos
            WHERE (stock_status IS NULL OR stock_status <> 'outofstock')
              AND imagen_url IS NOT NULL AND imagen_url != ''
            ORDER BY LENGTH(nombre) DESC
            """
        )
    matches = []
    for r in rows:
        if _categoria_normalizada(r["nombre"], r["descripcion"], r["categoria"]) == cat_norm:
            matches.append(dict(r))
            if len(matches) >= limit:
                break
    return matches


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

    Útil para que el bot conozca el catálogo vía el system prompt. Incluye el ID
    de cada producto (#123) para que el bot pueda emitir marcadores [FOTO:123].
    """
    out = Path(path) if path else (config.PROMPTS_DIR / "knowledge" / "catalogo.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        rows = await conn.fetch(
            "SELECT id, nombre, descripcion, categoria, precio FROM productos WHERE (stock_status IS NULL OR stock_status <> 'outofstock') ORDER BY categoria, nombre"
        )
    lines = ["# Catálogo de productos", ""]
    current_cat = None
    for r in rows:
        cat = r["categoria"] or "Sin categoría"
        if cat != current_cat:
            lines.append(f"\n## {cat}\n")
            current_cat = cat
        desc = f" — {r['descripcion']}" if r["descripcion"] else ""
        # El #ID al final permite al bot emitir [FOTO:ID] para enviar fotos fiables.
        lines.append(f"- **{r['nombre']}** — ${r['precio']:,}{desc}  #{r['id']}")
    out.write_text("\n".join(lines), encoding="utf-8")
    log.info("Catálogo exportado a %s (%d productos)", out, len(rows))
    return out
