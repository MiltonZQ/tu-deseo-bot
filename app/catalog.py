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
    # Bondage / BDSM / pareja — ANTES que lencería para que antifaz/esposas/bondage
    # no caigan en lencería por tener cat_origen="Lencería" en WooCommerce.
    (("bondage", "bdsm", "esposas", "antifaz", "amarre", "fusta", "latigo", "látigo",
      "kit ", "vendas", "mordaza"), "pareja-y-bondage"),
    # Lencería y disfraces (incluye suspensorios masculinos, pecheras y lencería erótica de hombre)
    (("suspensorio", "suspensor", "pechera", "body ", "baby doll", "babydoll", "conjunto ", "lencería", "lenceria",
      "disfra", "pantuflas", "pezonera", "ligero", "encaje"), "lenceria"),
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


# ── Capa de género/uso (para quién es el producto) ──
#
# La tabla productos NO tiene columna de género. Se infiere en runtime por reglas
# sobre nombre + descripción + categoría origen, igual que _categoria_normalizada.
# Esto es lo que permite enviar anillos para pene (hombre) y no vibradores de
# clítoris (mujer) cuando el cliente dice "para él / chimbo / pene".

# (palabras clave, género) — orden importa: se evalúa de arriba a abajo y la
# primera coincidencia gana. Van de lo más específico a lo más general.
_REGLAS_GENERO = [
    # HOMBRE: anillos/fundas para pene, masturbadores, próstata, bombas, lencería masculina
    (("anillo", "funda", "bomba pene", "bomba para", "bomba automatic", "bomba automática",
      "prostat", "próstata", "masturbador", "suspensorio", "suspensor", "pechera",
      "potenciador", "lovense diamo", "flexring", "candil", "frodo", "optimus", "diamo",
      "pene"), "hombre"),
    # PAREJA: juguetes de uso compartido (We-Vibe Chorus, doble estimulación, arnés con dildo)
    (("pareja", "we vibe", "we-vibe", "chorus", "doble estimulacion", "doble estimulación",
      "rabbit para pare", "arnes con dildo", "strap on", "strap-on"), "pareja"),
    # ANAL: plugs, bolas anales, dilatadores (puede ser para él o ella; se marca anal)
    (("plug", "bolas anal", "dilatador", "entrenamiento anal", "culo", "estimulacion anal",
      "estimulación anal"), "anal"),
    # MUJER: clítoris, punto G (no próstata), succionadores, rabbit, panty, body, baby doll
    (("clitoris", "clítoris", "clitorial", "punto g", "succionador", "suction", "air pulse",
      "rabbit", "panty vibr", "pezonera", "baby doll", "babydoll", "body ",
      "estimulacion clitor", "estimulación clitor"), "mujer"),
]


def _genero_normalizado(nombre: str, descripcion: str | None = "",
                        cat_origen: str | None = "") -> str:
    """Clasifica un producto en su género/uso: hombre|pareja|anal|mujer|unisex.

    Devuelve 'unisex' como fallback (lubricantes, bondage, cosmética, juegos,
    accesorios que aplican a cualquier persona).
    """
    haystack = _normalizar_texto(f"{nombre or ''} {descripcion or ''} {cat_origen or ''}")
    if not haystack.strip():
        return "unisex"
    for claves, genero in _REGLAS_GENERO:
        for clave in claves:
            if clave in haystack:
                return genero
    return "unisex"


# Mapeo de categoría funcional → categorías alternativas a probar cuando la
# intersección (categoría + género) da 0 resultados. Es el caso real de un
# cliente que pregunta por "vibradores" y luego aclara "para el pene": los
# productos de pene/hombre casi nunca son categoría funcional "vibradores", sino
# "anillos-y-fundas" o "masturbadores". Sin este mapeo, "vibradores" ∩ "hombre"
# = vacío y el bot respondía "Mira estas opciones…" sin enviar ninguna foto.
# Las categorías alternativas SE FILTRAN SIEMPRE por el género del cliente, así
# que no mezclan productos de mujer cuando el cliente pidió hombre.
_CATEGORIAS_ALTERNATIVAS_POR_GENERO = {
    "hombre": ["anillos-y-fundas", "masturbadores", "anal"],
    "anal": ["anal", "anillos-y-fundas"],
    "pareja": ["pareja-y-bondage", "vibradores", "anillos-y-fundas"],
    "mujer": ["vibradores", "succionadores", "dildos", "lenceria"],
}


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


# ── Pipeline determinístico: recuperación por género + clasificación de intención ──
#
# Estas funciones reemplazan la delegación total de la selección de productos al
# LLM. El sistema clasifica la intención del cliente y recupera los productos
# CORRECTOS de la DB (filtrados por categoría funcional + género), devolviendo
# candidatos confirmados que el LLM solo redacta.

# Mapa de intención del cliente (lo que busca) -> categoría funcional interna.
# Incluye sinónimos y jerga colombiana ("chimbo" = pene -> hombre / anillos).
_INTENCION_A_CATEGORIA_FUNCIONAL = {
    "vibradores": "vibradores",
    "vibrador": "vibradores",
    "succionador": "succionadores",
    "succionadores": "succionadores",
    "dildo": "dildos",
    "dildos": "dildos",
    "consolador": "dildos",
    "consoladores": "dildos",
    "plug": "anal",
    "anal": "anal",
    "masturbador": "masturbadores",
    "masturbadores": "masturbadores",
    "anillo": "anillos-y-fundas",
    "anillos": "anillos-y-fundas",
    "funda": "anillos-y-fundas",
    "fundas": "anillos-y-fundas",
    "bomba": "anillos-y-fundas",
    "suspensorio": "lenceria",
    "suspensorios": "lenceria",
    "suspensor": "lenceria",
    "suspensores": "lenceria",
    "lenceria": "lenceria",
    "lencería": "lenceria",
    "body": "lenceria",
    "bodys": "lenceria",
    "baby doll": "lenceria",
    "babydoll": "lenceria",
    "arnes": "anal",
    "arnés": "anal",
    "arneses": "anal",
    "conjunto": "lenceria",
    "conjuntos": "lenceria",
    "pechera": "lenceria",
    "bondage": "pareja-y-bondage",
    "lubricante": "lubricantes-y-cuidado",
    "lubricantes": "lubricantes-y-cuidado",
    "aceite": "lubricantes-y-cuidado",
}

# Sustantivos de producto que el cliente puede mencionar (para RAG/fallback).
_NOUN_KEYWORDS = [
    "suspensorio", "suspensor", "lenceria", "lencería", "body", "babydoll", "baby doll",
    "disfraz", "vibrador", "dildo", "succionador", "plug", "anal", "arnes", "arnés",
    "lubricante", "anillo", "funda", "masturbador", "bomba", "bondage", "chimbo", "pene",
]

# Detección de género en el MENSAJE DEL CLIENTE (no del producto).
# Incluye jerga colombiana. Orden: de específico a general; gana el primero.
_GENERO_KEYWORDS_CLIENTE = [
    (("chimbo", "pene", "para el", "para él", "para mi pene", "hombre", "masculino",
      "prostata", "próstata", "miembro", "verga", "gallo", "pito"), "hombre"),
    (("pareja", "en pareja", "los dos", "mi novia", "mi esposa", "mi novio", "mi esposo",
      "we vibe", "chorus"), "pareja"),
    (("anal", "el culo", "por atras", "por atrás", "cola", "recto"), "anal"),
    (("clitoris", "clítoris", "clitorial", "para ella", "punto g", "vagina", "vaginal",
      "mujer", "femenino", "mi novia", "clit"), "mujer"),
]

# Marcadores de que el cliente ya está especificando subtipo (no necesita calificar).
# Subtipos REALES (variantes dentro de una categoría), NO sustantivos de categoría.
# Importante: NO incluir aquí sustantivos de categoría (lubricante, dildo, anillo,
# succionador, funda, masturbador, suspensorio, conjunto, body, bondage, etc.) porque
# si no, "tienen lubricantes" se marcaría calificado=True y se saltaría la pregunta
# de calificación del turno 1. Solo subtipos que aclaran una variante concreta.
_SUBTIPO_KEYWORDS = (
    # Dildos
    "realista", "ventosa", "vidrio", "cristal", "doble",
    # Vibradores
    "rabbit", "punto g", "clitor", "clitori", "hitachi", "bala", "huevo vibr",
    # Anal
    "prostat", "próstata", "cola", "primera vez",
    # Lubricantes
    "base de agua", "silicona", "calor", "frío", "frio", "sabores", "sabor",
    "desensibiliz", "caliente",
    # Anillos/fundas
    # (Nota: "vibrador" NO se incluye aquí porque es sustantivo de categoría;
    # "anillo vibrador" se cubre porque "anillo" es categoría y el género/contexto
    # determina el subtipo en la regla híbrida.)
    # Lencería
    "arnes", "arnés", "liguero", "pechera", "encaje",
    # Generales de control
    "con app", "control remoto", "recargable", "inalambrico", "inalámbrico",
    "sencillo", "simple",
)

# Petición explícita de fotos por parte del cliente.
import re as _re_mod
_FOTO_REQUEST_RE = _re_mod.compile(
    r"\b(foto[s]?|imagen(es)?|fotografia[s]?|muestr(a|ame|amelo|amelas)|"
    r"mand(a|ame|ala|amelas)|envi(a|ame|ala|amelas)|ver la[s]? (foto|imagen)|"
    r"dame|las foto[s]?|puta[s]? foto[s]?|ver el producto|cada uno|todas las (foto|imagen))\b",
    _re_mod.IGNORECASE,
)


def _genero_desde_texto_cliente(texto: str) -> str | None:
    """Detecta el género/uso que el cliente expresa en su mensaje (None si no aclara).

    Usa word boundaries para evitar falsos positivos: "para el" NO debe coincidir
    dentro de "para ella" (substring). Así distinguimos "para él/hombre" de "para ella".
    """
    haystack = _normalizar_texto(texto)
    if not haystack.strip():
        return None
    for claves, genero in _GENERO_KEYWORDS_CLIENTE:
        for clave in claves:
            # Buscar la clave como palabra/frase completa con límites de palabra.
            patron = r"\b" + _re_mod.escape(clave) + r"\b"
            if _re_mod.search(patron, haystack):
                return genero
    return None


def _intencion_desde_texto(texto: str) -> tuple[str | None, str | None]:
    """Extrae (intencion, sustantivo) del mensaje del cliente.

    intencion = clave de _INTENCION_A_CATEGORIA_FUNCIONAL o None.
    sustantivo = el primer _NOUN_KEYWORDS hallado (para fallback).

    Matching robusto: además del substring exacto, usa matching por RAÍZ para
    cubrir plurales/variantes que no estén en el mapping (ej: 'suspensores'
    contiene la raíz 'suspensor'; 'consoladores' contiene 'consolador'). Esto
    evita regresiones cuando el cliente usa una forma no listada.
    """
    haystack = _normalizar_texto(texto)
    if not haystack.strip():
        return None, None
    # Tokenizar el mensaje en palabras (para matching por raíz contra palabras completas)
    palabras = set(_re_mod.findall(r"\b[a-z]{4,}\b", haystack))
    # Buscar la intención por sustantivo (longitud desc para preferir compuestos)
    intencion = None
    for clave, cat_func in sorted(_INTENCION_A_CATEGORIA_FUNCIONAL.items(),
                                  key=lambda kv: -len(kv[0])):
        # 1) Coincidencia directa de substring (caso general)
        if clave in haystack:
            intencion = clave
            break
        # 2) Matching por raíz: la clave (≥5 chars) es prefijo de una palabra del
        #    texto, o una palabra del texto es prefijo de la clave. Cubre plurales.
        if len(clave) >= 5:
            for pal in palabras:
                if pal.startswith(clave) or clave.startswith(pal):
                    intencion = clave
                    break
            if intencion:
                break
    # Sustantivo para fallback
    sustantivo = None
    for n in _NOUN_KEYWORDS:
        if n in haystack:
            sustantivo = n
            break
    return intencion, sustantivo


def clasificar_intencion_cliente(user_text: str,
                                 history: list[dict] | None = None) -> dict:
    """Clasifica la intención del cliente de forma determinística.

    Devuelve:
      {
        "intencion": "anillos" | "vibradores" | ... | None,  # sustantivo crudo
        "categoria_funcional": "anillos-y-fundas" | ... | None,
        "genero": "hombre" | "mujer" | "pareja" | "anal" | None,
        "calificado": bool,   # ¿ya sabemos subtipo/género suficientes para mostrar fotos?
        "pide_fotos": bool,
        "sustantivo": "anillo" | ... | None,
      }
    """
    if not user_text or not user_text.strip():
        return {
            "intencion": None, "categoria_funcional": None, "genero": None,
            "calificado": False, "pide_fotos": False, "sustantivo": None,
        }

    intencion, sustantivo = _intencion_desde_texto(user_text)
    genero = _genero_desde_texto_cliente(user_text)
    pide_fotos = bool(_FOTO_REQUEST_RE.search(user_text))

    # Si el mensaje es corto y no trae intención/género, mirar el historial reciente
    # (mismo truco ya usado por el RAG anterior) para frases como "negro", "sencillo".
    if not intencion and history:
        for h_msg in reversed(history[-6:]):
            c = h_msg.get("content", "")
            if h_msg.get("role") != "user":
                continue
            h_int, h_sus = _intencion_desde_texto(c)
            if h_int and not intencion:
                intencion = h_int
                if not sustantivo:
                    sustantivo = h_sus
            h_gen = _genero_desde_texto_cliente(c)
            if h_gen and not genero:
                genero = h_gen
            if intencion and genero:
                break

    categoria_funcional = _INTENCION_A_CATEGORIA_FUNCIONAL.get(intencion) if intencion else None

    # calificado: hay una intención clara Y (género o subtipo explícito o petición de fotos).
    tiene_subtipo = bool(sustantivo and any(
        s in _normalizar_texto(user_text) for s in _SUBTIPO_KEYWORDS
    ))
    calificado = bool(categoria_funcional and (genero or tiene_subtipo or pide_fotos))

    return {
        "intencion": intencion,
        "categoria_funcional": categoria_funcional,
        "genero": genero,
        "calificado": calificado,
        "pide_fotos": pide_fotos,
        "sustantivo": sustantivo,
    }


def _score_candidato(producto: dict, user_text: str) -> float:
    """Puntúa qué tan bien un producto se ajusta a la consulta del cliente.

    Premia coincidencia de tokens significativos del mensaje en nombre/descripción.
    """
    if not user_text:
        return 0.0
    tokens = _extract_search_tokens(user_text)
    if not tokens:
        return 0.0
    nombre = producto.get("nombre", "") or ""
    desc = producto.get("descripcion", "") or ""
    nombre_clean = _normalizar_texto(nombre)
    desc_clean = _normalizar_texto(desc)
    score = 0.0
    for t in tokens:
        if t in nombre_clean:
            score += 2.0  # coincidencia en nombre pesa más
        elif t in desc_clean:
            score += 0.5
    return score


async def get_productos_para_recomendar(
    categoria_funcional: str | None,
    genero: str | None,
    user_text: str = "",
    exclude_ids: list[int] | None = None,
    limit: int = 5,
) -> list[dict]:
    """Recupera los productos CORRECTOS para recomendar, filtrados por categoría
    funcional y género, con imagen y stock disponibles.

    Usa un FALLBACK PROGRESIVO para ser robusto: si el filtro más estricto no da
    resultados, relaja por etapas hasta encontrar productos. Esto evita el bug de
    "0 candidatos" que hacía que el bot respondiera sin fotos.

    - Intento A: categoría funcional + género + con imagen + activo.
    - Intento B: categoría funcional + género, sin exigir imagen (los sin foto se
      omiten al enviar, pero al menos hay candidatos del género correcto).
    - Intento C: categoría funcional + género + con imagen, sin exigir activo.
    - Intento D: categoría funcional + género, sin exigir imagen ni activo.
    - Intento E: búsqueda ILIKE por el sustantivo del user_text + género (con imagen).
      Captura productos que _categoria_normalizada etiqueta mal.
    Devuelve el primer intento con resultados.

    REGLA CRÍTICA: el género NUNCA se relaja. Es lo que distingue un suspensorio
    masculino de un conjunto de lencería femenino. Relajar el género (como hacía la
    versión anterior) metía productos de mujer cuando el cliente pedía hombre.
    """
    exclude_set = set(exclude_ids or [])

    async def _query(con_imagen: bool, con_activo: bool) -> list[dict]:
        where = ["(stock_status IS NULL OR stock_status <> 'outofstock')"]
        if con_imagen:
            where.append("imagen_url IS NOT NULL AND imagen_url != ''")
        if con_activo:
            where.append("activo = TRUE")
        sql = (
            "SELECT id, nombre, descripcion, categoria, precio, imagen_url, galeria_urls, permalink "
            "FROM productos"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY nombre"
        async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
            return [dict(r) for r in await conn.fetch(sql)]

    def _filtrar(rows: list[dict], exige_cat: bool, exige_gen: bool) -> list[dict]:
        out: list[dict] = []
        for r in rows:
            p = dict(r)
            if p["id"] in exclude_set:
                continue
            cat_func = _categoria_normalizada(p["nombre"], p["descripcion"], p["categoria"])
            gen = _genero_normalizado(p["nombre"], p["descripcion"], p["categoria"])
            if exige_cat and categoria_funcional and cat_func != categoria_funcional:
                continue
            if exige_gen and genero and gen != genero:
                continue
            p["_categoria_funcional"] = cat_func
            p["_genero"] = gen
            p["_score"] = _score_candidato(p, user_text)
            out.append(p)
        out.sort(key=lambda p: (-p["_score"], len(p["nombre"])))
        return out[:limit]

    # Intento A: categoría + género + con imagen + activo (más estricto)
    candidatos = _filtrar(await _query(con_imagen=True, con_activo=True),
                          exige_cat=True, exige_gen=True)
    if candidatos:
        return candidatos

    # Intento C: categoría + género + con imagen, sin exigir activo
    candidatos = _filtrar(await _query(con_imagen=True, con_activo=False),
                          exige_cat=True, exige_gen=True)
    if candidatos:
        log.info("get_productos_para_recomendar: intento C (sin activo) cat=%s género=%s → %d", categoria_funcional, genero, len(candidatos))
        return candidatos

    # Intento B: categoría + género, sin exigir imagen (candidatos correctos aunque falte foto)
    candidatos = _filtrar(await _query(con_imagen=False, con_activo=True),
                          exige_cat=True, exige_gen=True)
    if candidatos:
        log.info("get_productos_para_recomendar: intento B (sin imagen) cat=%s género=%s → %d", categoria_funcional, genero, len(candidatos))
        return candidatos

    # Intento D: categoría + género, sin exigir imagen ni activo
    candidatos = _filtrar(await _query(con_imagen=False, con_activo=False),
                          exige_cat=True, exige_gen=True)
    if candidatos:
        log.info("get_productos_para_recomendar: intento D (sin imagen ni activo) cat=%s género=%s → %d", categoria_funcional, genero, len(candidatos))
        return candidatos

    # Intento E-bis: RELAJAR LA CATEGORÍA por género. Cuando la intersección
    # (categoría + género) es vacía (ej: "vibradores" + "hombre" — los productos
    # de pene son anillos/fundas, no vibradores), probar las categorías
    # alternativas de ese género, siempre filtrando por género. Esto resuelve el
    # bug donde el cliente aclaraba "para el pene" y el bot no enviaba ninguna
    # foto porque no había vibradores de hombre.
    if categoria_funcional and genero:
        alt_cats = [c for c in _CATEGORIAS_ALTERNATIVAS_POR_GENERO.get(genero, [])
                    if c != categoria_funcional]
        for alt_cat in alt_cats:
            for con_img, con_act in [(True, True), (True, False), (False, True), (False, False)]:
                rows = await _query(con_imagen=con_img, con_activo=con_act)
                # Filtrar por la categoría alternativa + el género del cliente.
                res = []
                for r in rows:
                    p = dict(r)
                    if p["id"] in exclude_set:
                        continue
                    if _categoria_normalizada(p["nombre"], p["descripcion"], p["categoria"]) != alt_cat:
                        continue
                    if _genero_normalizado(p["nombre"], p["descripcion"], p["categoria"]) != genero:
                        continue
                    p["_categoria_funcional"] = alt_cat
                    p["_genero"] = genero
                    p["_score"] = _score_candidato(p, user_text)
                    res.append(p)
                if res:
                    res.sort(key=lambda p: (-p["_score"], len(p["nombre"])))
                    log.info("get_productos_para_recomendar: intento E-bis (relaja categoría %s→%s, género=%s) → %d",
                             categoria_funcional, alt_cat, genero, len(res))
                    return res[:limit]

    # Intento E: búsqueda ILIKE por sustantivo del user_text + género (con imagen).
    # Último recurso: captura productos que _categoria_normalizada etiqueta mal.
    # PROHIBIDO si hay una categoria_funcional explícita pedida: si el cliente pidió
    # "lubricantes", un ILIKE libre traería vibradores/dildos/etc. (mezcla de categorías).
    # Solo se usa cuando NO hay categoría clara (búsqueda abierta por género).
    if not categoria_funcional:
        tokens = user_text and _extract_search_tokens(user_text)
        if tokens:
            for tok in tokens[:3]:
                if len(tok) < 4:
                    continue
                async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
                    rows = await conn.fetch(
                        """
                        SELECT id, nombre, descripcion, categoria, precio, imagen_url, galeria_urls, permalink
                        FROM productos
                        WHERE (stock_status IS NULL OR stock_status <> 'outofstock')
                          AND imagen_url IS NOT NULL AND imagen_url != ''
                          AND (nombre ILIKE '%' || $1 || '%' OR descripcion ILIKE '%' || $1 || '%')
                          ORDER BY nombre
                          LIMIT 30
                        """,
                        tok,
                    )
                # Aquí sí exigimos género (no categoría funcional) para no mezclar.
                res = _filtrar([dict(r) for r in rows], exige_cat=False, exige_gen=True)
                if res:
                    log.info("get_productos_para_recomendar: intento E (ILIKE %r + género) → %d", tok, len(res))
                    return res

    if not candidatos:
        log.warning("get_productos_para_recomendar: 0 candidatos tras todos los intentos cat=%s género=%s", categoria_funcional, genero)
    return candidatos


async def buscar_producto_especifico(user_text: str, limit: int = 3) -> list[dict]:
    """Busca productos por nombre cuando el cliente pide algo específico (ej:
    "tienen el Lovense Diamo?"). Usa coincidencia de tokens con score >= 0.5.

    Para el pipeline: cuando no hay intención de categoría clara pero el texto
    menciona un producto concreto, recupéralo para que el LLM lo muestre.
    """
    if not user_text or len(user_text.strip()) < 3:
        return []
    tokens = _extract_search_tokens(user_text)
    if not tokens:
        return []
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        rows = await conn.fetch(
            """
            SELECT id, nombre, descripcion, categoria, precio, imagen_url, galeria_urls, permalink
            FROM productos
            WHERE activo = TRUE
              AND (stock_status IS NULL OR stock_status <> 'outofstock')
              AND imagen_url IS NOT NULL AND imagen_url != ''
            """
        )
    scored = []
    for r in rows:
        p = dict(r)
        score = _score_candidato(p, user_text)
        if score >= 1.0:  # al menos una coincidencia fuerte en el nombre
            p["_score"] = score
            scored.append(p)
    scored.sort(key=lambda p: (-p["_score"], len(p["nombre"])))
    return scored[:limit]
