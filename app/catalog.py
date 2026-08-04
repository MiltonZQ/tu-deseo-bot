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
import re
from pathlib import Path
from dataclasses import dataclass, field
from functools import lru_cache
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
    "enciaste", "enviaste", "mandaste", "llegado", "llego", "hijueputa", "puta",
    # Conectores y saludos. Sin ellos, "que" contaba como token de búsqueda:
    # dos coincidencias de relleno en la descripción (0.5 + 0.5) alcanzaban el
    # umbral de producto específico sin tocar el nombre, y un "Hola, que dildos
    # tinen" devolvía 2 productos al azar en vez del catálogo de la categoría.
    "que", "qué", "cual", "cuales", "hay", "tiene", "hola", "buenas",
    "buenos", "dias", "tardes", "noches", "gracias", "favor", "porfa",
    "manejan", "maneja", "venden", "vende", "muestras", "mostrar", "quisiera",
    "podria", "puede", "algun", "alguna", "algunos", "algunas",
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
    matches = sum(1 for t in tokens if t in name_words)
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
    "anillos-vibradores",
    "fundas-pene",
    "bombas-pene",
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

CATEGORIAS_AMPLIAS = {
    "vibradores",
    "lenceria",
    "dildos",
    "anal",
    "lubricantes-y-cuidado",
    "pareja-y-bondage",
    "juegos-y-accesorios",
}

CATEGORIAS_PUNTUALES = {
    "succionadores",
    "bombas-pene",
    "fundas-pene",
    "masturbadores",
    "anillos-vibradores",
    "anillos-y-fundas",
}

# (palabras clave en nombre/descripción, categoría funcional) — orden importa:
# se evalúa de arriba a abajo y la primera coincidencia gana.
_REGLAS_CATEGORIA = [
    # Enemas, duchas anales e irrigadores (prioridad para no caer en marca optimus)
    (("enema", "ducha anal", "duchas anales", "irrigador", "pera anal"), "anal"),
    # Higiene/cuidado (sin ambigüedad con juguetes)
    (("limpiador", "limpia juguete", "toallitas"), "lubricantes-y-cuidado"),
    # Lubricantes y cosmética íntima (incluye geles y lubricantes anales)
    (("lubricant", "lubric", "estimulant", "retardant", "desensibiliz", "electrizante", "multiorgasmo", "gel anal", "lubricante anal", "sen anal", "spray", "vela ", "aceite ",
      "friction", "estrechant", "booster", "serum", "crema "), "lubricantes-y-cuidado"),

    # Succionadores de clítoris (antes que vibradores genéricos)
    (("succionador", "suction", "air pulse", "succión de clítoris", "succio", "pro 2", "satisfyer pro"), "succionadores"),
    # Bombas de vacío para el pene (separado de anillos)
    (("bomba para", "bomba pene", "bomba automatic", "bomba automática", "bomba de vacio", "bomba vacio", "hefesto"), "bombas-pene"),
    # Anillos vibradores y eróticos para él/pareja
    (("anillo vibrador", "anillos vibradores", "anillo vibrante", "anillos vibrantes",
      "flexring", "satisfyer bull", "diamo", "candil", "frodo", "optimus", "anillo peneano", "anillos peneanos"), "anillos-vibradores"),
    # Fundas para el pene (sin vibración o extensores)
    (("funda para el pene", "funda pene", "extensor pene", "engrosador"), "fundas-pene"),
    # Anillos genéricos
    (("anillo", "anillos"), "anillos-vibradores"),
    # Bondage / BDSM / pareja
    (("bondage", "bdsm", "esposas", "antifaz", "amarre", "fusta", "latigo", "látigo",
      "kit", "vendas", "mordaza", "sadomaso", "sado", "cepo", "spreade"), "pareja-y-bondage"),
    # Lencería y disfraces
    (("suspensorio", "suspensor", "pechera", "body ", "baby doll", "babydoll", "conjunto ", "lencería", "lenceria",
      "disfra", "pantuflas", "pezonera", "ligero", "encaje"), "lenceria"),
    # Anal: plugs, bolas anales, estimuladores de próstata, dilatadores, arneses
    (("plug", "anal", "prostat", "próstata", "bolas anal", "dilatador", "arnes",
      "arnés", "strap on", "strap-on", "cola ", "entrenamiento anal"), "anal"),
    # Masturbadores masculinos
    (("masturbador", "huevo masturb", "vagina "), "masturbadores"),
    # Dildos / consoladores
    (("dildo", "consolador", "realista", "ventosa"), "dildos"),
    # Funda secundaria
    (("funda", "fundas"), "fundas-pene"),
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


# Alias de typos comunes del cliente → forma correcta. Aplicado al user_text
# ANTES de clasificar, para que mensajes como "anl" (anal), "mjer" (mujer),
# "dldo" (dildo) se clasifiquen bien sin matching difuso genérico (que causaría
# falsos positivos en mensajes cortos). Conservador: solo sustantivos de producto.
# Se aplica por PALABRA completa (límites de palabra) para no romper palabras
# largas que contengan la secuencia (ej: no tocar "analógico").
_ALIASES_TYPO = {
    # anal
    "anl": "anal", "naal": "anal", "anla": "anal", "anall": "anal", "aanl": "anal",
    "anall": "anal", "anaal": "anal",
    # mujer
    "mjer": "mujer", "mjeres": "mujeres", "mujer": "mujer",
    # dildo
    "dldo": "dildo", "didlo": "dildo", "dildos": "dildo", "dilbo": "dildo",
    # vibrador
    "vibradro": "vibrador", "vibbrador": "vibrador", "vibradr": "vibrador",
    "vibador": "vibrador", "vibrador": "vibrador",
    # lubricante
    "lubrciante": "lubricante", "lubricatne": "lubricante", "lubricante": "lubricante",
    "lubrikante": "lubricante",
    # succionador
    "succiondor": "succionador", "succionadpr": "succionador",
    "succionador": "succionador", "succion": "succionador",
    # plug
    "plgu": "plug", "plugg": "plug",
    # consolador
    "consoladr": "consolador", "consolador": "consolador",
    # arnés
    "arne": "arnes", "arnse": "arnes", "arnes": "arnes", "harnez": "arnes",
    # masturbador
    "masturb": "masturbador", "masturbador": "masturbador",
    # Marcas (typos comunes → forma correcta)
    "lovence": "lovense", "lobense": "lovense", "lovns": "lovense", "lovene": "lovense",
    "levense": "lovense", "levensse": "lovense", "lovenese": "lovense", "lovense": "lovense",
    "lovense": "lovense",
    "satisfier": "satisfyer", "satifyer": "satisfyer", "satisfier": "satisfyer",
    "satisfyr": "satisfyer", "satisfayer": "satisfyer", "satisfyer": "satisfyer",
    "tengue": "tenga", "tenga": "tenga", "tenega": "tenga",
    "wevibe": "we vibe", "we-vibe": "we vibe", "we vibe": "we vibe",
    "wevay": "we vibe", "webe": "we vibe",
    "womanicer": "womanizer", "wumanizer": "womanizer", "womanizer": "womanizer",
    "lelo": "lelo", "lello": "lelo",
    "camtoys": "camtoyz", "cam toy": "camtoyz", "camtoy": "camtoyz", "camtoyz": "camtoyz",
    # disfraz
    "dizfraz": "disfraz", "disfras": "disfraz", "difraz": "disfraz",
}

_ALIASES_TYPO_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _ALIASES_TYPO if k) + r")\b",
    re.IGNORECASE,
)


def _corregir_typos(texto: str | None) -> str:
    """Reemplaza typos comunes del cliente por su forma correcta (por palabra).

    Conservador: solo alias explícitos de sustantivos de producto/género, con
    límites de palabra. No usa matching difuso genérico (evita falsos positivos
    en mensajes cortos tipo "si", "ok"). Se aplica al user_text antes de clasificar.
    """
    if not texto:
        return texto or ""
    def _reemp(m: re.Match) -> str:
        return _ALIASES_TYPO[m.group(0).lower()]
    return _ALIASES_TYPO_RE.sub(_reemp, texto)


# Claves que deben coincidir como PALABRA COMPLETA (admitiendo plural). El resto se
# comparan con límite de palabra solo a la IZQUIERDA, porque muchas son raíces
# deliberadas: "lubricant" debe pillar lubricante/lubricantes, "prostat" próstata,
# "succio" succión/succionador, "anal" anales.
#
# Comparar con `in` pelado (como se hacía) mete la clave DENTRO de otras palabras y
# clasifica mal. Casos reales del catálogo:
#   "funda"     dentro de "profunda"     → vibradores de mujer marcados como hombre
#   "culo"      dentro de "testículos"   → producto marcado como anal
#   "anal"      dentro de "canal"        → producto marcado como anal
#   "sado"      dentro de "pensado"      → producto marcado como bondage
#   "dado"      dentro de "cuidado"      → producto marcado como juego de mesa
#   "pene"      al inicio de "penetración" → producto marcado como hombre
# El último es prefijo (arranca palabra), por eso "pene" exige palabra completa.
_CLAVES_PALABRA_COMPLETA = frozenset({"pene"})


@lru_cache(maxsize=None)
def _patron_clave(clave: str) -> "re.Pattern[str]":
    patron = r"\b" + re.escape(clave)
    if clave in _CLAVES_PALABRA_COMPLETA:
        patron += r"(?:es|s)?\b"
    return re.compile(patron)


def _clave_en_texto(clave: str, haystack: str) -> bool:
    """True si la clave aparece en el texto empezando palabra (no dentro de otra)."""
    return bool(_patron_clave(clave).search(haystack))


def _aplicar_reglas_categoria(haystack: str) -> str | None:
    """Primera categoría funcional cuya palabra clave aparece en el texto."""
    for claves, cat_funcional in _REGLAS_CATEGORIA:
        for clave in claves:
            if _clave_en_texto(clave, haystack):
                return cat_funcional
    return None


def _categoria_normalizada(nombre: str, descripcion: str | None = "",
                           cat_origen: str | None = "") -> str:
    """Clasifica un producto en una categoría funcional mediante reglas de matching.

    EL NOMBRE MANDA. Las reglas se evalúan primero contra el nombre solo, y únicamente
    si el nombre no da señal se amplía a descripción + categoría de origen. Mezclar los
    tres textos desde el principio hacía que una palabra suelta de la DESCRIPCIÓN
    disparara una regla anterior (más genérica) y se llevara el producto a otra
    categoría. Casos reales:
      - "Bomba Automática ... Bender Optimus Pro" → su descripción dice "sistema
        eléctrico de succión", y la regla de succionadores va antes que la de bombas:
        quedaba como succionador y NUNCA aparecía al pedir "bombas para el pene".
      - "Succionador Con Ondas Y Vibracion Nyla Fuscia" → su descripción dice
        "Compatible con lubricantes a base de agua", y la regla de lubricantes va antes
        que la de succionadores: quedaba como lubricante y era el único succionador que
        no se mostraba.
    Sobre el catálogo real esto corrige 32 de 246 productos (13%).
    """
    if not f"{nombre or ''}{descripcion or ''}{cat_origen or ''}".strip():
        return "juegos-y-accesorios"
    cat = _aplicar_reglas_categoria(_normalizar_texto(nombre))
    if cat:
        return cat
    haystack = _normalizar_texto(f"{nombre or ''} {descripcion or ''} {cat_origen or ''}")
    cat = _aplicar_reglas_categoria(haystack)
    if cat:
        return cat
    cat_o = _normalizar_texto(cat_origen)
    if "bondage" in cat_o:
        return "pareja-y-bondage"
    if "lencer" in cat_o:
        return "lenceria"
    if "cosmetica" in cat_o or "cosmética" in (cat_origen or "").lower():
        return "lubricantes-y-cuidado"
    return "juegos-y-accesorios"


# ── Capa de género/uso (para quién es el producto) ──
# Orden prioritario: Succionadores/Mujer van PRIMERO para evitar que productos femeninos caigan en hombre.
_REGLAS_GENERO = [
    # MUJER: succionadores, clítoris, punto G, rabbit, baby doll, pezonera
    (("clitoris", "clítoris", "clitorial", "punto g", "succionador", "suction", "air pulse",
      "rabbit", "panty vibr", "pezonera", "baby doll", "babydoll", "estimulacion clitor", "pro 2", "satisfyer pro"), "mujer"),
    # ANAL: enemas, duchas anales, plugs, bolas anales, dilatadores (prioridad antes que hombre para no descalificar enemas con marca "optimus")
    (("enema", "ducha anal", "duchas anales", "irrigador", "pera anal", "plug", "bolas anal", "dilatador", "entrenamiento anal", "culo", "estimulacion anal",
      "estimulación anal"), "anal"),
    # HOMBRE: anillos/fundas para pene, masturbadores, próstata, bombas, lencería masculina
    (("anillo", "funda", "bomba pene", "bomba para", "bomba automatic", "bomba automática",
      "prostat", "próstata", "masturbador", "suspensorio", "suspensor", "pechera",
      "potenciador", "lovense diamo", "flexring", "candil", "frodo", "optimus", "diamo",
      "pene"), "hombre"),
    # PAREJA: juguetes de uso compartido
    (("pareja", "we vibe", "we-vibe", "chorus", "doble estimulacion", "doble estimulación",
      "rabbit para pare", "arnes con dildo", "strap on", "strap-on"), "pareja"),
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
            if _clave_en_texto(clave, haystack):
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
    "hombre": ["anillos-vibradores", "anillos-y-fundas", "fundas-pene", "bombas-pene", "masturbadores", "anal"],
    "anal": ["anal", "anillos-y-fundas"],
    "pareja": ["pareja-y-bondage", "vibradores", "anillos-vibradores", "anillos-y-fundas"],
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
    "anillos vibradores": "anillos-vibradores",
    "anillo vibrador": "anillos-vibradores",
    "anillos de pene": "anillos-vibradores",
    "anillo de pene": "anillos-vibradores",
    "anillos peneanos": "anillos-vibradores",
    "anillo peneano": "anillos-vibradores",
    "anillos eroticos": "anillos-vibradores",
    "anillo erotico": "anillos-vibradores",
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
    "anillo": "anillos-vibradores",
    "anillos": "anillos-vibradores",
    "funda": "fundas-pene",
    "fundas": "fundas-pene",
    "funda para el pene": "fundas-pene",
    "fundas para el pene": "fundas-pene",
    "funda para pene": "fundas-pene",
    "fundas para pene": "fundas-pene",
    "bomba": "bombas-pene",
    "bombas": "bombas-pene",
    "bomba pene": "bombas-pene",
    "bombas pene": "bombas-pene",
    "bomba para el pene": "bombas-pene",
    "bombas para el pene": "bombas-pene",
    "bomba para pene": "bombas-pene",
    "bombas para pene": "bombas-pene",
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
    "arnes": "pareja-y-bondage",
    "arnés": "pareja-y-bondage",
    "arneses": "pareja-y-bondage",
    "conjunto": "lenceria",
    "conjuntos": "lenceria",
    "pechera": "lenceria",
    "bondage": "pareja-y-bondage",
    "bdsm": "pareja-y-bondage",
    "kit": "pareja-y-bondage",
    "kits": "pareja-y-bondage",
    "amarre": "pareja-y-bondage",
    "amarres": "pareja-y-bondage",
    "esposas": "pareja-y-bondage",
    "esposa": "pareja-y-bondage",
    "fustas": "pareja-y-bondage",
    "fusta": "pareja-y-bondage",
    "latigo": "pareja-y-bondage",
    "látigo": "pareja-y-bondage",
    "antifaz": "pareja-y-bondage",
    "antifaces": "pareja-y-bondage",
    "vendas": "pareja-y-bondage",
    "mordaza": "pareja-y-bondage",
    "sadomasoquismo": "pareja-y-bondage",
    "sado": "pareja-y-bondage",
    "lubricante": "lubricantes-y-cuidado",
    "lubricantes": "lubricantes-y-cuidado",
    "aceite": "lubricantes-y-cuidado",
}

# Sustantivos de producto que el cliente puede mencionar (para RAG/fallback).
_NOUN_KEYWORDS = [
    "anillos vibradores", "anillo vibrador", "suspensorio", "suspensor", "lenceria", "lencería",
    "body", "babydoll", "baby doll", "disfraz", "vibrador", "dildo", "succionador", "plug", "anal",
    "arnes", "arnés", "lubricante", "anillo", "funda", "masturbador", "bomba", "bondage", "chimbo", "pene",
    "kit", "kits", "amarre", "amarres", "esposas", "esposa", "fustas", "fusta", "antifaz", "antifaces",
    "vendas", "mordaza", "latigo", "látigo", "bdsm", "sado", "sadomasoquismo",
]

# Detección de género en el MENSAJE DEL CLIENTE (no del producto).
# Incluye jerga colombiana. Orden: de específico a general; gana el primero.
_GENERO_KEYWORDS_CLIENTE = [
    (("chimbo", "pene", "para el", "para él", "para mi pene", "hombre", "masculino",
      "prostata", "próstata", "miembro", "verga", "gallo", "pito",
      "anillos vibradores", "anillo vibrador", "anillos de pene", "anillo de pene",
      "masturbador", "masturbadores", "huevo masturb",
      # Lencería masculina: _REGLAS_GENERO ya marca estos PRODUCTOS como hombre,
      # esto alinea la lectura del mensaje del cliente. Sin ellos, "suspensorio"
      # dejaba género=None y la categoría lencería podía traer prendas de mujer.
      "suspensorio", "suspensorios", "suspensor", "suspensores", "pechera"), "hombre"),
    (("pareja", "en pareja", "los dos", "mi novia", "mi esposa", "mi novio", "mi esposo",
      "we vibe", "chorus"), "pareja"),
    (("anal", "el culo", "por atras", "por atrás", "cola", "recto"), "anal"),
    (("clitoris", "clítoris", "clitorial", "para ella", "punto g", "vagina", "vaginal",
      "mujer", "femenino", "mi novia", "clit", "succionador", "succionadores", "succion", "satisfyer"), "mujer"),
]

# Marcadores de que el cliente ya está especificando subtipo (no necesita calificar).
# Subtipos REALES (variantes dentro de una categoría), NO sustantivos de categoría.
# Importante: NO incluir aquí sustantivos de categoría (lubricante, dildo, anillo,
# succionador, funda, masturbador, suspensorio, conjunto, body, bondage, etc.) porque
# si no, "tienen lubricantes" se marcaría calificado=True y se saltaría la pregunta
# de calificación del turno 1. Solo subtipos que aclaran una variante concreta.
_SUBTIPO_KEYWORDS = (
    # Dildos
    "realista", "ventosa", "vidrio", "cristal", "doble", "textura piel", "textura", "piel",
    # Vibradores
    "rabbit", "punto g", "clitor", "clitori", "hitachi", "bala", "huevo vibr",
    # Anal
    "prostat", "próstata", "cola", "primera vez", "vibracion", "vibración",
    "ducha", "enema",
    # Lubricantes
    "base de agua", "silicona", "calor", "frío", "frio", "sabores", "sabor",
    "desensibiliz", "caliente",
    # Lencería
    "funda", "fundas", "funda para el pene",
    "arnes", "arnés", "liguero", "pechera", "encaje", "body", "bodies", "bodys",
    # Disfraces por tipo (van ANTES de "disfraz": la clave más específica debe
    # ganar cuando el mensaje trae ambas, ej. "disfraz colegiala").
    "colegiala", "coneja", "conejita", "diabla", "enfermera", "mucama",
    "playboy", "policia", "sailor moon",
    "disfraz",
    "suspensorio", "conjunto",
    # Bondage / BDSM (pareja-y-bondage)
    "esposas", "esposa", "antifaz", "antifaces", "fustas", "fusta",
    "latigo", "látigo", "amarre", "amarres", "mordaza", "vendas",
    # Succionadores
    "doble estimulacion", "doble estimulación",
    # Generales de control
    "con app", "control por app", "control app", "app control", "control remoto", "recargable", "inalambrico", "inalámbrico",
    "sencillo", "simple",
)

# Mapeo subtipo → categoría funcional. Cuando el cliente responde SOLO un subtipo
# (ej: "sabores", "doble", "ventosa") sin sustantivo de categoría, el sistema antes
# heredaba la categoría del turno anterior (dildos) y mostraba productos espurios.
# Con este mapeo, el sistema sabe que "sabores" → lubricantes, "doble" → dildos,
# "rabbit" → vibradores, y deriva la categoría correcta (cambio de tema legítimo).
# Subtipos NO ambiguos (seguros de mapear siempre):
_SUBTIPO_A_CATEGORIA = {
    "sabores": "lubricantes-y-cuidado", "sabor": "lubricantes-y-cuidado",
    "base de agua": "lubricantes-y-cuidado", "silicona": "lubricantes-y-cuidado",
    "desensibiliz": "lubricantes-y-cuidado",
    "realista": "dildos", "ventosa": "dildos", "vidrio": "dildos",
    "cristal": "dildos", "doble": "dildos", "textura piel": "dildos", "textura": "dildos", "piel": "dildos",
    "rabbit": "vibradores", "punto g": "vibradores", "hitachi": "vibradores",
    "bala": "vibradores", "huevo vibr": "vibradores",
    "prostat": "anal", "próstata": "anal",
    "ducha": "anal", "enema": "anal",
    "liguero": "lenceria", "pechera": "lenceria", "encaje": "lenceria",
    "body": "lenceria", "bodies": "lenceria", "bodys": "lenceria",
    "disfraz": "lenceria", "suspensorio": "lenceria",
    "conjunto": "lenceria",
    "colegiala": "lenceria", "coneja": "lenceria", "conejita": "lenceria",
    "diabla": "lenceria", "enfermera": "lenceria", "mucama": "lenceria",
    "playboy": "lenceria", "policia": "lenceria", "sailor moon": "lenceria",
    "funda": "fundas-pene", "fundas": "fundas-pene", "funda para el pene": "fundas-pene",
    "arnes": "pareja-y-bondage", "arnés": "pareja-y-bondage",
    "esposas": "pareja-y-bondage", "esposa": "pareja-y-bondage",
    "antifaz": "pareja-y-bondage", "antifaces": "pareja-y-bondage",
    "fustas": "pareja-y-bondage", "fusta": "pareja-y-bondage",
    "latigo": "pareja-y-bondage", "látigo": "pareja-y-bondage",
    "amarre": "pareja-y-bondage", "amarres": "pareja-y-bondage",
    "mordaza": "pareja-y-bondage", "vendas": "pareja-y-bondage",
}

_SUBTIPOS_SOFT = {
    "primera vez", "primera", "principiante", "suave", "sutil",
    "sencillo", "simple", "facil", "fácil", "basico", "básico",
    "discreto", "pequeño", "pequeña", "pequeno", "inicio", "iniciando",
    "para empezar", "empezar",
}


def _es_subtipo_soft(subtipo: str | None) -> bool:
    if not subtipo:
        return False
    s = _normalizar_texto(subtipo)
    for soft in _SUBTIPOS_SOFT:
        soft_n = _normalizar_texto(soft)
        if not soft_n:
            continue
        if soft_n in s or s in soft_n:
            return True
    return False

# Sinónimos y equivalencias por subtipo para filtrado estricto en recomendaciones
_SUBTIPO_SINONIMOS: dict[str, list[str]] = {
    "ducha": ["ducha", "duchas", "enema", "enemas", "irrigador", "pera anal", "canula", "cánula", "limpieza", "higiene", "lavado"],
    "enema": ["ducha", "duchas", "enema", "enemas", "irrigador", "pera anal", "canula", "cánula", "limpieza", "higiene", "lavado"],
    "limpieza": ["ducha", "duchas", "enema", "enemas", "irrigador", "pera anal", "canula", "cánula", "limpieza", "higiene", "lavado"],
    "prostat": ["prostat", "próstata", "prostatico", "prostático"],
    "próstata": ["prostat", "próstata", "prostatico", "prostático"],
    "plug": ["plug", "plugs", "dilatador"],
    "bola": ["bola", "bolas", "ben wa"],
    "bolas": ["bola", "bolas", "ben wa"],
    "realista": ["realista", "realistas", "peneano", "peneana", "textura piel", "textura", "piel"],
    "textura piel": ["realista", "realistas", "textura piel", "textura", "piel", "peneano"],
    "textura": ["realista", "textura piel", "textura", "piel"],
    "piel": ["realista", "textura piel", "piel"],
    "ventosa": ["ventosa", "ventosas"],
    "vidrio": ["vidrio", "cristal"],
    "cristal": ["vidrio", "cristal"],
    "doble": ["doble", "doble estimulacion", "doble penetracion"],
    "rabbit": ["rabbit", "orejas"],
    "punto g": ["punto g", "puntog", "curvado"],
    "hitachi": ["hitachi", "varita", "wand"],
    "bala": ["bala", "bullet"],
    "huevo vibr": ["huevo", "huevo vibrador"],
    "sabores": ["sabor", "sabores", "comestible"],
    "sabor": ["sabor", "sabores", "comestible"],
    "base de agua": ["agua", "base de agua"],
    "silicona": ["silicona", "base de silicona"],
    "con app": ["con app", "control por app", "control app", "app control", "app", "con aplicacion", "control remoto app"],
    "control por app": ["con app", "control por app", "control app", "app control", "app", "con aplicacion"],
    "control app": ["con app", "control por app", "control app", "app control", "app"],
    "control remoto": ["control remoto", "control por app", "con app", "app control"],
    "desensibiliz": ["anal", "desensibilizante", "desensibiliz", "anestesico", "anestésico", "relajante anal", "lubricante anal"],
    "colegiala": ["colegiala"],
    "coneja": ["coneja", "conejita"],
    "conejita": ["coneja", "conejita"],
    "diabla": ["diabla"],
    "enfermera": ["enfermera"],
    "mucama": ["mucama"],
    "playboy": ["playboy"],
    "policia": ["policia"],
    "sailor moon": ["sailor moon"],
}


def _filtrar_por_subtipo(filas: list[dict], subtipo: str | None) -> list[dict]:
    """Deja solo los productos cuyo nombre/descripción nombran el subtipo pedido.

    Misma semántica que el filtro del camino legacy (`_filtrar`), pero aplicable
    al camino por restricciones, que no lo tenía: pedir "disfraz de colegiala"
    devolvía las 20 lencerías y el orden por concordancia no bastaba para
    separarlas ("Mucama Lerot" empata con "Colegiala Lerot" en los tokens
    "disfraz" y "lerot").

    Se compara sobre texto NORMALIZADO en ambos lados —no con ILIKE en SQL—
    porque el catálogo tiene "Disfraz Policía Lerot" y el cliente escribe
    "policia": sin quitar tildes en los dos lados no hay coincidencia.
    """
    if not subtipo:
        return filas
    sinonimos = [_normalizar_texto(s)
                 for s in _SUBTIPO_SINONIMOS.get(subtipo, [subtipo])]
    sinonimos = [s for s in sinonimos if s]
    if not sinonimos:
        return filas
    out = []
    for p in filas:
        nd = _normalizar_texto(f"{p.get('nombre', '')} {p.get('descripcion', '')}")
        if any(s in nd for s in sinonimos):
            out.append(p)
    return out


# Subtipos AMBIGUOS: "calor"/"frío" pueden ser lubricantes (sensaciones) O
# juguetes con calentamiento. "cola" puede ser plug anal o lencería. "clitor"/
# "clitori" suelen ser vibradores pero también succionadores. Estos NO se mapean
# a una categoría fija; se dejan al contexto (si el estado previo era lubricantes,
# "calor" sigue siendo lubricantes; si era vibradores, sigue vibradores).

# ── Vocabulario conocido del catálogo (para detectar marcas/modelos SIN lista
# blanca) ──
# Cualquier palabra del mensaje del cliente que NO esté aquí (sustantivos de
# categoría, subtipos, sinónimos, género, atributos "soft") es candidata a ser
# un nombre de marca/modelo (ej: "King Cock", "Icicles", "Tenera", "AYAMI") que
# el cliente mencionó explícitamente. Se usa para buscar por NOMBRE sin
# depender de mantener una lista fija de marcas (que siempre queda incompleta).
# Palabras genéricas/de relleno que el cliente usa sin nombrar nada concreto
# (ej: "tienen algo para X", "ese producto"). No son marca/modelo aunque no
# estén en el vocabulario de categorías — se excluyen para no disparar
# búsquedas por nombre innecesarias.
_PALABRAS_GENERICAS_IGNORAR = frozenset({
    "algo", "eso", "esto", "esa", "ese", "cosa", "cosas", "producto",
    "productos", "articulo", "articulos", "artículo", "artículos", "marca",
    "modelo", "tipo", "clase", "opcion", "opciones", "opción", "cual", "cuales",
    "cuál", "cuáles", "algun", "alguna", "algún", "alguno", "algunos", "algunas",
    "probar", "prueba", "pruebas", "recomiendas", "recomienda", "recomendar",
    "necesito", "busco", "buscando", "gustaria", "gustaría", "bien", "mejor",
    "gusta", "gustan", "vale", "verlo", "verla",
})


def _construir_vocabulario_conocido() -> frozenset[str]:
    frases: list[str] = []
    frases.extend(_INTENCION_A_CATEGORIA_FUNCIONAL.keys())
    frases.extend(_NOUN_KEYWORDS)
    frases.extend(_SUBTIPO_KEYWORDS)
    frases.extend(_SUBTIPO_A_CATEGORIA.keys())
    frases.extend(_SUBTIPOS_SOFT)
    for claves, _genero in _GENERO_KEYWORDS_CLIENTE:
        frases.extend(claves)
    for sinonimos in _SUBTIPO_SINONIMOS.values():
        frases.extend(sinonimos)
    palabras: set[str] = set()
    for frase in frases:
        for palabra in _normalizar_texto(frase).split():
            if palabra:
                palabras.add(palabra)
    return frozenset(palabras)


_VOCABULARIO_CONOCIDO = _construir_vocabulario_conocido()


def _tokens_no_reconocidos(user_text: str) -> list[str]:
    """Tokens del mensaje del cliente que NO son vocabulario conocido del
    catálogo (categoría/subtipo/género/atributo) ni relleno genérico.
    Palabras de 4+ letras que sobreviven este filtro suelen ser nombres de
    marca/modelo."""
    tokens = _extract_search_tokens(user_text)
    return [t for t in tokens
            if len(t) >= 4 and t not in _VOCABULARIO_CONOCIDO and t not in _PALABRAS_GENERICAS_IGNORAR]


# Marcas/modelos concretos del catálogo. Si el cliente nombra uno, se busca por
# NOMBRE y se muestra directo (sin pregunta de calificación). Esta lista curada
# NO pretende ser exhaustiva: _tokens_no_reconocidos() cubre las marcas que no
# estén aquí, y corregir_typos_contra_catalogo() las cubre incluso con typos.
_MARCAS_CONOCIDAS = (
    "lovense", "satisfyer", "tenga", "we vibe", "wevibe", "chorus",
    "womanizer", "lelo", "fun factory", "pipedream", "california dreaming",
    "camtoyz", "lerot", "optimus", "calexotics",
    "lush", "hush", "diamo", "max", "nova", "ambu", "oski", "ferri",
    "gush", "pro 2", "pro 3",
    # Marcas de cosmética/lubricantes/estimulantes del catálogo
    "sen ", "sen intimo", "elixir", "blix", "hot production", "hot ",
    "joy division", "joydivision", "system jo", "swiss navy", "pjur",
    "doc johnson", "troc", "basix", "icicles", "spanish fly",
    # Términos de productos que están en nombres del catálogo
    "multiorgasmo", "orgasmo", "estimulante", "retardante", "afrodisiaco",
)


# ── Corrección de typos contra los nombres REALES del catálogo ──
# _corregir_typos() cubre typos frecuentes con alias fijos ("anl"→"anal"), pero
# no puede cubrir los nombres de producto/marca (son cientos y cambian con el
# catálogo). Sin esto, un typo como "multiorgarmo" no matchea nada: el
# clasificador no reconoce categoría y el LLM se queda sin candidatos, que es
# justo el escenario donde puede inventarse una lista de productos.
_PALABRAS_CATALOGO_CACHE: frozenset[str] = frozenset()
_PALABRAS_CATALOGO_TS: float = 0.0
_PALABRAS_CATALOGO_TTL = 600  # segundos


async def _palabras_de_nombres_catalogo() -> frozenset[str]:
    """Palabras que aparecen en los NOMBRES de productos activos (cache con TTL).

    Ante cualquier fallo devuelve el cache previo (posiblemente vacío): la
    corrección de typos es una mejora opcional, nunca debe romper el flujo.
    """
    global _PALABRAS_CATALOGO_CACHE, _PALABRAS_CATALOGO_TS
    import time
    ahora = time.monotonic()
    if _PALABRAS_CATALOGO_CACHE and (ahora - _PALABRAS_CATALOGO_TS) < _PALABRAS_CATALOGO_TTL:
        return _PALABRAS_CATALOGO_CACHE
    try:
        async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
            rows = await conn.fetch(
                "SELECT nombre FROM productos WHERE activo = TRUE AND nombre IS NOT NULL"
            )
        palabras: set[str] = set()
        for r in rows:
            for palabra in re.findall(r"\b[a-z]{4,}\b", _normalizar_texto(r["nombre"])):
                palabras.add(palabra)
        _PALABRAS_CATALOGO_CACHE = frozenset(palabras)
        _PALABRAS_CATALOGO_TS = ahora
        log.info("Cache de palabras del catálogo refrescado: %d palabras", len(palabras))
    except Exception as exc:
        log.warning("No se pudo refrescar el cache de palabras del catálogo: %s", exc)
    return _PALABRAS_CATALOGO_CACHE


def _misma_familia(a: str, b: str) -> bool:
    """True si dos palabras son variantes de la misma (uno es prefijo del otro),
    ej: 'multiorgasmo' y 'multiorgasmos'."""
    return a.startswith(b) or b.startswith(a)


async def corregir_typos_contra_catalogo(user_text: str) -> str:
    """Corrige typos de nombres de producto/marca comparando con el catálogo real.

    Solo toca tokens que NO son vocabulario conocido (categoría/subtipo/género)
    y que tienen al menos 5 letras, y solo si hay UNA coincidencia cercana de
    alta confianza. Si no hay match claro, devuelve el texto sin cambios: es
    preferible no encontrar nada a buscar un producto equivocado.
    """
    tokens = [t for t in _tokens_no_reconocidos(user_text) if len(t) >= 5]
    if not tokens:
        return user_text
    vocabulario = await _palabras_de_nombres_catalogo()
    if not vocabulario:
        return user_text
    candidatas = sorted(vocabulario | {m.strip() for m in _MARCAS_CONOCIDAS if len(m.strip()) >= 5})
    import difflib
    texto_corregido = user_text
    for token in tokens:
        if token in vocabulario:
            continue  # ya es una palabra real del catálogo, no es typo
        cercanas = difflib.get_close_matches(token, candidatas, n=2, cutoff=0.8)
        if not cercanas:
            continue
        # No elegir al azar entre dos palabras DISTINTAS igual de parecidas (sería
        # adivinar de qué producto habla el cliente). Variantes de la misma palabra
        # ("multiorgasmo"/"multiorgasmos") no cuentan como ambigüedad: una es
        # prefijo de la otra y ambas llevan al mismo producto.
        if len(cercanas) > 1 and not _misma_familia(cercanas[0], cercanas[1]):
            r0 = difflib.SequenceMatcher(None, token, cercanas[0]).ratio()
            r1 = difflib.SequenceMatcher(None, token, cercanas[1]).ratio()
            if (r0 - r1) < 0.05:
                continue
        texto_corregido = re.sub(rf"\b{re.escape(token)}\b", cercanas[0],
                                 texto_corregido, flags=re.IGNORECASE)
        log.info("Typo corregido contra el catálogo: %r → %r", token, cercanas[0])
    return texto_corregido


# Petición explícita de fotos por parte del cliente.
import re as _re_mod
_FOTO_REQUEST_RE = _re_mod.compile(
    r"\b(foto[s]?|imagen(es)?|fotografia[s]?|muestr(a|ame|amelo|amelas)|"
    r"mand(a|ame|ala|amelas)|envi(a|ame|ala|amelas)|ver la[s]? (foto|imagen)|"
    r"ver (mas|más)|(mas|más) opciones|otra[s]? opciones|dejame ver|déjame ver|"
    r"dame|las foto[s]?|puta[s]? foto[s]?|ver el producto|cada uno|todas las (foto|imagen))\b",
    _re_mod.IGNORECASE,
)


# OJO: se comparan contra texto ya pasado por _normalizar_texto (ASCII sin
# acentos), así que los patrones se normalizan también. Escritos con "ñ"/tildes
# nunca matcheaban: "mas diseños" → "mas disenos" y la comparación fallaba.
_VER_MAS_PATRONES = tuple(_normalizar_texto(p) for p in (
    "ver mas", "ver más", "mas diseños", "más diseños", "mas modelos", "más modelos",
    "otros diseños", "otros modelos", "mas opciones", "más opciones",
    "quiero ver mas", "ver mas diseños", "otras opciones",
))


def _es_ver_mas(user_text: str) -> bool:
    """El cliente pide más opciones de lo que ya está viendo (no describe un
    producto nuevo)."""
    t = _normalizar_texto(user_text)
    return any(p in t for p in _VER_MAS_PATRONES)


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


# Palabras que indican que el mensaje es una DIRECCIÓN de entrega, no una
# consulta de producto. Igual que pedidos.py::_DIRECCION_RE pero pensada para
# detectar (no extraer) — ver clasificar_intencion_cliente().
_DIRECCION_MARCADORES_RE = _re_mod.compile(
    r"\b(calle|cll|cl|carrera|cra|kr|kra|diagonal|diag|transversal|transv|"
    r"avenida|av|manzana|mz|conjunto\s+residencial|torre|apto|apartamento|"
    r"interior|int\.|barrio|casa\s+\d|piso\s+\d)\b",
    _re_mod.IGNORECASE,
)
_DIRECCION_NUMERO_RE = _re_mod.compile(r"#?\s*\d{1,3}\s*[a-z]?\s*-\s*\d{1,3}|#\s*\d{1,4}")


def _parece_direccion_envio(texto: str) -> bool:
    """True si el texto tiene forma de dirección de entrega colombiana.

    Se usa para que clasificar_intencion_cliente() NO reclasifique categoría
    cuando el cliente está dando su dirección (bug: "Conjunto Residencial..."
    disparaba la categoría "lencería" porque "conjunto" es también una clave
    de producto, y eso reseteaba el estado de la conversación a mitad del
    cierre de venta).
    """
    if not texto:
        return False
    haystack = _normalizar_texto(texto)
    tiene_marcador = bool(_DIRECCION_MARCADORES_RE.search(haystack))
    tiene_numero_de_direccion = bool(_DIRECCION_NUMERO_RE.search(texto))
    if not (tiene_marcador or tiene_numero_de_direccion):
        return False
    # Si además nombra un producto real, no es SOLO una dirección (ej. "quiero
    # un conjunto de lencería" tiene "conjunto" pero también intención clara
    # de producto) — en ese caso sí debe clasificarse normalmente.
    tiene_producto = any(
        _re_mod.search(rf"\b{_re_mod.escape(n)}\b", haystack) for n in _NOUN_KEYWORDS
    )
    return not tiene_producto


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
        # 1) Coincidencia de FRASE COMPLETA (límites de palabra a ambos lados).
        # Antes era substring suelto (`clave in haystack`): una clave corta podía
        # aparecer como fragmento de una palabra más larga sin relación (ej.
        # "kit" dentro de una palabra que lo contenga) y disparar una categoría
        # que el cliente no pidió. Mismo criterio ya usado en
        # _genero_desde_texto_cliente (línea 1171) para el mismo problema.
        if _re_mod.search(rf"\b{_re_mod.escape(clave)}\b", haystack):
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


async def clasificar_intencion_cliente(user_text: str,
                                 history: list[dict] | None = None,
                                 categoria_estado: str | None = None,
                                 restricciones_previas: dict | None = None) -> dict:
    """Clasifica la intención del cliente de forma determinística.

    categoria_estado: categoría persistida del turno anterior
    (conversation_state.categoria_funcional). Es la fuente confiable de "qué
    estaba viendo el cliente" y el motor de memoria la prefiere sobre adivinarla
    leyendo el texto del bot.

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

    if _parece_direccion_envio(user_text):
        # El cliente está dando su dirección de entrega, no pidiendo un
        # producto nuevo: no reclasificar categoría (evita que "conjunto",
        # "kit" u otra clave de producto que aparezca dentro de la dirección
        # dispare un cambio de tema falso y resetee productos_mostrados).
        return {
            "intencion": None, "categoria_funcional": categoria_estado,
            "genero": None, "calificado": bool(categoria_estado),
            "pide_fotos": False, "sustantivo": None,
        }

    # Corregir typos comunes del cliente (anl→anal, mjer→mujer, dldo→dildo...)
    # antes de clasificar. Conservador: solo alias explícitos por palabra completa.
    user_text = _corregir_typos(user_text)

    intencion, sustantivo = _intencion_desde_texto(user_text)
    # Intención que trae el MENSAJE ACTUAL por sí mismo (antes de heredar nada del
    # historial). Es lo que distingue "el cliente nombró otra categoría" de "el
    # cliente respondió algo corto y seguimos en la misma".
    intencion_propia = intencion
    genero = _genero_desde_texto_cliente(user_text)
    pide_fotos = bool(_FOTO_REQUEST_RE.search(user_text))

    # Si el mensaje es corto y no trae intención/género, mirar el historial reciente
    # (mismo truco ya usado por el RAG anterior) para frases como "negro", "sencillo".
    # IMPORTANTE: si el mensaje actual trae un SUBTIPO explícito (doble, ventosa...),
    # NO heredamos género del historial. El subtipo define la variante, no el género;
    # heredar género espurio causaba "doble" → "para hombre" incorrecto.
    norm_user_temprano = _normalizar_texto(user_text)
    tiene_subtipo_temprano = any(
        s in norm_user_temprano for s in _SUBTIPO_KEYWORDS
    )
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
            # Solo heredar género si el mensaje actual NO trae subtipo propio.
            if not tiene_subtipo_temprano:
                h_gen = _genero_desde_texto_cliente(c)
                if h_gen and not genero:
                    genero = h_gen
            if intencion and (genero or tiene_subtipo_temprano):
                break

    categoria_funcional = _INTENCION_A_CATEGORIA_FUNCIONAL.get(intencion) if intencion else None
    # ¿La categoría salió del MENSAJE ACTUAL (y no de heredar el historial)? Es la
    # señal de cambio de tema que usa el motor de memoria más abajo.
    cat_desde_mensaje = bool(intencion_propia and categoria_funcional)

    # subtipo_detectado: CUÁL subtipo reconoció (ej: "doble", "ventosa", "realista"),
    # para usarlo como filtro de ranking (que el producto mostrado coincida con el
    # subtipo pedido). Antes solo se sabía SI había subtipo (bool), no cuál.
    # NOTA: la detección de subtipo NO depende de tener sustantivo: el cliente puede
    # responder solo "sabores" (subtipo de lubricantes) sin sustantivo de categoría.
    # Se calcula aquí (antes del respaldo LLM) para que ese respaldo sepa si ya hay subtipo.
    norm_user = _normalizar_texto(user_text)
    subtipo_detectado = None
    for s in _SUBTIPO_KEYWORDS:
        if s in norm_user:
            subtipo_detectado = s
            break
    tiene_subtipo = subtipo_detectado is not None

    # RESPALDO LLM: si el clasificador determinístico (listas de palabras) no
    # reconoció ninguna categoría, pero el mensaje parece buscar un producto
    # (no es saludo/pago/queja), hacer una llamada BARATA al LLM que clasifica
    # en una de las 11 categorías cerradas. Así cubrimos jerga/variantes no
    # listadas (kits de sadomasoquismo, cinturón de castidad, etc.) sin mantener
    # listas infinitas. El LLM solo puede elegir una de las 11 o "ninguna".
    if not categoria_funcional and not subtipo_detectado and user_text and len(user_text.strip()) >= 4:
        try:
            from app import openai_client
            res_llm = await openai_client.clasificar_intencion_llm(user_text)
            if res_llm and res_llm.get("categoria") and res_llm["categoria"] != "ninguna":
                categoria_funcional = res_llm["categoria"]
                cat_desde_mensaje = True
                if not intencion:
                    intencion = res_llm["categoria"]
                if not genero and res_llm.get("genero"):
                    genero = res_llm["genero"]
        except Exception:
            log.warning("Respaldo LLM de clasificación falló — se ignora")

    # DERIVACIÓN subtipo → categoría: si el cliente respondió SOLO un subtipo (ej:
    # "sabores", "doble") sin sustantivo de categoría, derivar la categoría del
    # subtipo. Así "sabores" → lubricantes (no hereda "dildos" del turno anterior)
    # y se reconoce como cambio de tema legítimo (reset del estado).
    if subtipo_detectado and not categoria_funcional:
        cat_derivada = _SUBTIPO_A_CATEGORIA.get(subtipo_detectado)
        if cat_derivada:
            categoria_funcional = cat_derivada
            cat_desde_mensaje = True
            if not intencion:
                intencion = subtipo_detectado

    # ── MOTOR DE MEMORIA Y CONTEXTO (Memory-First Resolution) ──
    # Si en la memoria conversacional reciente la categoría activa era una categoría concreta
    # (ej: lubricantes-y-cuidado) y el mensaje actual NO trae un sustantivo explícito de cambio de tema,
    # la respuesta corta del usuario (ej: "anal", "agua", "sabores") se interpreta como filtro dentro
    # de la categoría activa en memoria, sin cambiar de tema a juguetes anales o categorías diferentes.
    #
    # La categoría persistida del turno anterior es el dato CONFIABLE y manda sobre
    # todo lo demás. La heurística de abajo (adivinar leyendo el texto del bot) es
    # solo un respaldo para cuando no hay estado, ej. el primer turno.
    cat_activa_memoria = categoria_estado
    if not cat_activa_memoria and history:
        for h_msg in reversed(history[-6:]):
            if h_msg.get("role") == "assistant":
                c_h = (h_msg.get("content") or "").lower()
                # ORDEN: de específico a genérico, igual que _REGLAS_CATEGORIA. Los
                # succionadores van antes que los vibradores porque casi todos se
                # describen como "succionador de clítoris", y la rama de vibradores
                # (que también busca "clítoris") se los llevaba: por eso un "si"
                # tras ver succionadores terminaba trayendo vibradores Lovense.
                if any(w in c_h for w in ("succionador", "succionadores", "air pulse", "satisfyer pro")):
                    cat_activa_memoria = "succionadores"
                    break
                elif any(w in c_h for w in ("lubricante", "base de agua", "silicona", "sabores", "sensaciones")):
                    cat_activa_memoria = "lubricantes-y-cuidado"
                    break
                elif any(w in c_h for w in ("dildo", "ventosa", "realista")):
                    cat_activa_memoria = "dildos"
                    break
                elif any(w in c_h for w in ("lencería", "lenceria", "body", "suspensorio")):
                    cat_activa_memoria = "lenceria"
                    break
                elif any(w in c_h for w in ("vibrador", "clítoris", "clitoris")):
                    cat_activa_memoria = "vibradores"
                    break

    # ── ¿CAMBIO DE TEMA? ──
    # Antes esto era una lista fija de 15 sustantivos ("dildo", "vibrador",
    # "bomba"...) y CUALQUIER mensaje que no la contuviera dejaba pegada la
    # categoría del turno anterior. La lista omitía lubricante, látigo, plug,
    # arnés, kit, masturbador, esposas, disfraz… así que una conversación que
    # empezaba en "bombas para el pene" seguía respondiendo bombas cuando el
    # cliente ya preguntaba por látigos, por lubricantes y por sabores.
    #
    # Criterio correcto y sin listas paralelas: si la categoría salió del MENSAJE
    # ACTUAL (sustantivo propio, respaldo LLM o subtipo derivado) y es distinta de
    # la de memoria, el cliente cambió de tema y manda su mensaje.
    #
    # EXCEPCIÓN: dentro de lubricantes, palabras como "anal" son un FILTRO de la
    # categoría activa ("lubricante anal"), no una categoría nueva. Es el motivo
    # original de este bloque y se conserva.
    es_filtro_de_lubricantes = (
        cat_activa_memoria == "lubricantes-y-cuidado"
        and any(q in norm_user for q in ("anal", "agua", "silicona", "sabor", "desensibiliz",
                                         "otro", "otros", "tipo", "tipos", "cual", "cuál", "opcion", "opción"))
    )
    cambio_de_tema = bool(
        cat_desde_mensaje
        and categoria_funcional != cat_activa_memoria
        and not es_filtro_de_lubricantes
    )
    if cambio_de_tema and cat_activa_memoria:
        log.info("Cambio de tema en el mensaje: memoria=%s → mensaje=%s",
                 cat_activa_memoria, categoria_funcional)

    # La faceta que aporta ESTE mensaje. Se mira aquí y no al final porque
    # decide si el cliente cambió de tema: "multiorgasmo" no está en el
    # vocabulario de intenciones legacy, así que sin esto `cambio_de_tema`
    # quedaba en False y se heredaba la categoría del turno anterior — el
    # cliente pedía multiorgasmo y el bot respondía "opciones de dildos".
    from app import facetas as _facetas
    _tipo_del_mensaje = _facetas.interpretar_mensaje(user_text).get("tipo")
    _cat_del_tipo = _facetas._TIPO_A_CATEGORIA_LEGACY.get(_tipo_del_mensaje or "")
    if (cat_activa_memoria and _cat_del_tipo and _cat_del_tipo != cat_activa_memoria
            and not es_filtro_de_lubricantes):
        log.info("Cambio de tema por faceta propia: memoria=%s → tipo=%s (%s)",
                 cat_activa_memoria, _tipo_del_mensaje, _cat_del_tipo)
        cambio_de_tema = True
        categoria_funcional = categoria_funcional or _cat_del_tipo
        intencion = intencion or _cat_del_tipo

    if cat_activa_memoria and not cambio_de_tema:
        categoria_funcional = cat_activa_memoria
        intencion = cat_activa_memoria
        if "anal" in norm_user or "desensibiliz" in norm_user:
            subtipo_detectado = "desensibiliz"
        elif "agua" in norm_user:
            subtipo_detectado = "base de agua"
        elif "silicona" in norm_user:
            subtipo_detectado = "silicona"
        elif "sabor" in norm_user or "sabores" in norm_user:
            subtipo_detectado = "sabores"
        tiene_subtipo = subtipo_detectado is not None

    # HERENCIA DE SUBTIPO DESDE LA MEMORIA DE CONVERSACIÓN.
    # Mismo criterio que la herencia de intención/género de arriba, pero acotada a
    # los "ver más": ahí el cliente pide más de lo mismo y el mensaje ("Mas
    # diseños") no lleva subtipo propio, así que sin esto se perdía el filtro y el
    # sistema rellenaba con cualquier producto de la categoría (pedía suspensorios
    # y recibía bodies de mujer). Fuera de ese caso NO se hereda: si el cliente
    # nombra otra cosa, mandar lo que él acaba de pedir, no lo del turno anterior.
    if not subtipo_detectado and categoria_funcional and history and _es_ver_mas(user_text):
        for h_msg in reversed(history[-8:]):
            if h_msg.get("role") != "user":
                continue
            norm_h = _normalizar_texto(_corregir_typos(h_msg.get("content") or ""))
            for s in _SUBTIPO_KEYWORDS:
                if s not in norm_h:
                    continue
                # Heredar solo si el subtipo pertenece a la categoría activa, para
                # no arrastrarlo tras un cambio de tema ("suspensorio" no aplica si
                # ahora pide dildos). Los subtipos ambiguos (calor, cola, clitor) no
                # están en el mapeo y dependen justamente del contexto: se heredan.
                cat_del_subtipo = _SUBTIPO_A_CATEGORIA.get(s)
                if cat_del_subtipo is None or cat_del_subtipo == categoria_funcional:
                    subtipo_detectado = s
                    log.info("Subtipo %r heredado de la conversación (cat=%s)",
                             s, categoria_funcional)
                break
            if subtipo_detectado:
                break
        tiene_subtipo = subtipo_detectado is not None

    calificado = bool(categoria_funcional and (genero or tiene_subtipo or pide_fotos))
    if categoria_funcional in CATEGORIAS_PUNTUALES or (cat_activa_memoria and not cambio_de_tema):
        calificado = True


    # ES_ESPECÍFICO: el cliente pide un producto concreto por marca/modelo (Lovense
    # Lush, Satisfyer Pro 2, Tenga Egg, We-Vibe Chorus...). En ese caso hay que
    # buscarlo por nombre y mostrarlo DIRECTO, sin pasar por la calificación de
    # categoría (que genera la pregunta genérica innecesaria). El user_text ya viene
    # corregido de typos (_corregir_typos), así que "lovence"→"lovense" matchea.
    es_especifico = any(marca in norm_user for marca in _MARCAS_CONOCIDAS)
    if es_especifico:
        # Producto específico → mostrar directo (no preguntar calificación).
        calificado = True

    # ── RESTRICCIONES ACUMULADAS (ver app/facetas.py) ──
    # Es el modelo nuevo: la intención del cliente como conjunto de facetas que se
    # refina entre turnos, en vez de una única categoría que se sobrescribe. Se
    # calcula en paralelo a la clasificación de arriba mientras dura la
    # transición; la recuperación de productos ya usa esto.
    from app import facetas as _facetas
    restricciones = _facetas.fusionar_restricciones(
        restricciones_previas, _facetas.interpretar_mensaje(user_text))

    # Si el mensaje actual es un filtro explícito de lubricantes (ej. "anal", "agua",
    # "silicona", "sabores" tras preguntar por lubricantes), forzar tipo="lubricante"
    # en las restricciones para no arrastrar tipos de productos anteriores (ej: bondage).
    if es_filtro_de_lubricantes and restricciones.get("tipo") != "lubricante":
        restricciones["tipo"] = "lubricante"

    return {
        "intencion": intencion,
        "categoria_funcional": categoria_funcional,
        "genero": genero,
        "calificado": calificado,
        "pide_fotos": pide_fotos,
        "sustantivo": sustantivo,
        "subtipo_detectado": subtipo_detectado,
        "es_especifico": es_especifico,
        "restricciones": restricciones,
    }


def _mismo_termino(a: str, b: str) -> bool:
    """True si son la misma palabra salvo el plural.

    `_misma_familia` (prefijo puro, arriba) es demasiado laxo para puntuar:
    haría casar 'anal' con 'analgésico' y 'pro' con 'próstata'. Aquí solo se
    admite el sufijo de plural castellano, que es lo que se perdía: el cliente
    escribe 'succionadores' y el producto se llama 'Succionador ...', o escribe
    'multiorgasmo' y los productos se llaman 'Multiorgasmos ...'.
    """
    if a == b:
        return True
    corta, larga = (a, b) if len(a) <= len(b) else (b, a)
    if len(corta) < 4:
        return False
    return larga in (corta + "s", corta + "es")


def _score_nombre_discriminante(producto: dict, user_text: str) -> float:
    """Cuánto casa el NOMBRE con las palabras que identifican un producto concreto.

    Solo cuentan los tokens que NO son vocabulario de catálogo: marcas y
    modelos. La palabra de la categoría no vale como prueba de que el cliente
    pide un producto específico — "dildos" está en el nombre de los 22 dildos,
    así que serviría para "encontrar" cualquiera de ellos. Con esto, "dildos
    Tenera" se resuelve por "tenera" y "Hola, que dildos tinen" no se resuelve
    por nada, que es lo correcto: ahí el cliente habla de la categoría.
    """
    discriminantes = _tokens_no_reconocidos(user_text)
    if not discriminantes:
        return 0.0
    nombre_toks = set(re.findall(r"\b[a-z]{2,}\b", _normalizar_texto(
        producto.get("nombre", "") or "")))
    return sum(2.0 for t in discriminantes
               if any(_mismo_termino(t, n) for n in nombre_toks))


def _score_candidato(producto: dict, user_text: str) -> float:
    if not user_text:
        return 0.0
    tokens = _extract_search_tokens(user_text)
    if not tokens:
        return 0.0
    nombre = producto.get("nombre", "") or ""
    desc = producto.get("descripcion", "") or ""
    import re
    nombre_toks = set(re.findall(r"\b[a-z]{2,}\b", _normalizar_texto(nombre)))
    desc_toks = set(re.findall(r"\b[a-z]{2,}\b", _normalizar_texto(desc)))
    score = 0.0
    for t in tokens:
        if any(_mismo_termino(t, n) for n in nombre_toks):
            score += 2.0
        elif any(_mismo_termino(t, d) for d in desc_toks):
            score += 0.5
    return score


async def get_productos_para_recomendar(
    categoria_funcional: str | None,
    genero: str | None,
    user_text: str = "",
    exclude_ids: list[int] | None = None,
    limit: int = 5,
    subtipo: str | None = None,
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

    # Query semántica: normalmente el texto del cliente, que describe lo que busca.
    # Pero un "ver más" ("Mas diseños") no describe ningún producto — su embedding
    # contra el catálogo es ruido puro. En ese caso se usa el contexto real de la
    # búsqueda; si no hay contexto, se salta Qdrant y resuelve el SQL, que es
    # determinístico y ya respeta exclude_ids.
    query_semantica = (user_text or "").strip()
    if query_semantica and _es_ver_mas(query_semantica):
        contexto = [p for p in (subtipo, categoria_funcional, genero) if p]
        query_semantica = " ".join(contexto).replace("-", " ") if contexto else ""

    qdrant_candidatos_cache: list[dict] = []
    if config.QDRANT_ENABLED and query_semantica:
        try:
            from app import vector_store
            qdrant_candidatos_cache = await vector_store.search_semantic_products(
                query=query_semantica,
                categoria_funcional=categoria_funcional,
                genero=genero,
                exclude_ids=exclude_ids,
                limit=limit,
            )
            log.info("Qdrant pre-busqueda %d candidatos", len(qdrant_candidatos_cache))
        except Exception as exc:
            log.warning("Qdrant pre-busqueda fallo %s", exc)
            qdrant_candidatos_cache = []

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

    def _pasa_filtro_negativo(p: dict) -> bool:
        norm_text = _normalizar_texto(f"{p.get('nombre', '')} {p.get('descripcion', '')}")
        usr_text = _normalizar_texto(user_text)
        # Si busca anillos vibradores
        if categoria_funcional in ("anillos-vibradores", "anillos-y-fundas") or "anillo" in usr_text:
            if "bomba" in norm_text and "anillo" not in norm_text:
                return False
            if any(w in norm_text for w in ("succionador", "air pulse", "pro 2", "satisfyer pro")):
                return False
        # Si busca enema / ducha / limpieza anal
        if any(w in usr_text for w in ("enema", "ducha", "limpieza anal", "irrigador")):
            if any(w in norm_text for w in ("bolas anales", "bola anal", "plug anal", "arnes", "dilatador")) and not any(w in norm_text for w in ("enema", "ducha", "limpieza")):
                return False
        # Si el género del cliente es hombre (para él / pene)
        if genero == "hombre":
            if any(w in norm_text for w in ("succionador", "air pulse", "para ella", "estimulacion clitor", "baby doll")):
                return False
        # Si busca bombas de pene
        if categoria_funcional == "bombas-pene":
            if any(w in norm_text for w in ("succionador", "baby doll")):
                return False
        return True


    def _filtrar(rows: list[dict], exige_cat: bool, exige_gen: bool) -> list[dict]:
        out: list[dict] = []
        out_subtipo: list[dict] = []
        es_soft = _es_subtipo_soft(subtipo) if subtipo else False
        for r in rows:
            p = dict(r)
            if p["id"] in exclude_set:
                continue
            if not _pasa_filtro_negativo(p):
                continue
            cat_func = _categoria_normalizada(p["nombre"], p["descripcion"], p["categoria"])
            gen = _genero_normalizado(p["nombre"], p["descripcion"], p["categoria"])
            if exige_cat and categoria_funcional and cat_func != categoria_funcional:
                continue
            if exige_gen and genero and gen != genero:
                continue
            p["_categoria_funcional"] = cat_func
            p["_genero"] = gen
            score = _score_candidato(p, user_text)
            cumple_subtipo = False
            if subtipo:
                nd = _normalizar_texto(f"{p.get('nombre','')} {p.get('descripcion','')}")
                sinonimos = _SUBTIPO_SINONIMOS.get(subtipo, [subtipo])
                if any(s in nd for s in sinonimos):
                    cumple_subtipo = True
                    if subtipo in ("sabor", "sabores") and "sin sabor" in nd and not any(f in nd for f in ("fresa", "chocolate", "uva", "cereza", "manzana", "menta", "frutilla", "sandia", "chicle", "sabor a")):
                        cumple_subtipo = False
                if cumple_subtipo:
                    score += 10.0
            p["_score"] = score
            if cumple_subtipo:
                out_subtipo.append(p)
            out.append(p)
        if subtipo:
            if out_subtipo:
                out_subtipo.sort(key=lambda p: (-p["_score"], len(p["nombre"])))
                return out_subtipo[:limit]
            if es_soft:
                out.sort(key=lambda p: (-p["_score"], p.get("precio", 0), len(p["nombre"])))
                return out[:limit]
            return []
        return out[:limit]

    # Intento Q: Qdrant semantico filtrado por reglas negocio
    filtrados_q: list[dict] = []
    if qdrant_candidatos_cache:
        for p in qdrant_candidatos_cache:
            if p.get("id") in exclude_set:
                continue
            if not p.get("imagen_url"):
                continue
            if not _pasa_filtro_negativo(p):
                continue
            # validar categoria/genero si se exige
            if categoria_funcional:
                cat_q = _categoria_normalizada(p.get("nombre",""), p.get("descripcion",""), p.get("categoria_funcional") or p.get("categoria") or "")
                # si qdrant ya filtro por categoria, respetar; si no, exigir
                if cat_q != categoria_funcional and categoria_funcional not in (p.get("categoria_funcional") or ""):
                    # permitir si no hay categoria exacta pero es cercana? por ahora exigir
                    if cat_q != categoria_funcional:
                        # para puntuales como succionadores, exigir exacto
                        if categoria_funcional in ("succionadores","bombas-pene","fundas-pene","masturbadores","anillos-vibradores"):
                            continue
            if genero:
                gen_q = p.get("genero") or _genero_normalizado(p.get("nombre",""), p.get("descripcion"), "")
                if gen_q != genero and gen_q != "unisex":
                    if genero != "unisex":
                        continue
            p["_categoria_funcional"] = categoria_funcional or p.get("categoria_funcional")
            p["_genero"] = genero or p.get("genero")
            if "_score" not in p:
                p["_score"] = _score_candidato(p, user_text) + float(p.get("_score",0) or 0)
            filtrados_q.append(p)
        if filtrados_q:
            filtrados_q.sort(key=lambda x: (-x.get("_score",0), len(x.get("nombre",""))))
            if len(filtrados_q) >= limit:
                log.info("Qdrant filtrado %d/%d candidatos validos", len(filtrados_q), len(qdrant_candidatos_cache))
                return filtrados_q[:limit]
            # Qdrant encontró candidatos pero MENOS que el límite pedido (ej: solo 1
            # de 5 succionadores por el umbral de score semántico). En vez de
            # devolver un puñado incompleto, se completan los huecos con el SQL
            # determinístico de abajo (excluyendo los ya elegidos por Qdrant).
            log.info("Qdrant filtrado %d/%d candidatos validos (< limit=%d) — se completa con SQL",
                     len(filtrados_q), len(qdrant_candidatos_cache), limit)
            exclude_set = exclude_set | {p["id"] for p in filtrados_q}

    def _combinar_con_qdrant(candidatos_sql: list[dict]) -> list[dict]:
        if not filtrados_q:
            return candidatos_sql
        return (filtrados_q + candidatos_sql)[:limit]

    # Intento A: categoría + género + con imagen + activo (más estricto)
    candidatos = _filtrar(await _query(con_imagen=True, con_activo=True),
                          exige_cat=True, exige_gen=True)
    if candidatos:
        return _combinar_con_qdrant(candidatos)

    # Intento C: categoría + género + con imagen, sin exigir activo
    candidatos = _filtrar(await _query(con_imagen=True, con_activo=False),
                          exige_cat=True, exige_gen=True)
    if candidatos:
        log.info("get_productos_para_recomendar: intento C (sin activo) cat=%s género=%s → %d", categoria_funcional, genero, len(candidatos))
        return _combinar_con_qdrant(candidatos)

    # Intento B: categoría + género, sin exigir imagen (candidatos correctos aunque falte foto)
    candidatos = _filtrar(await _query(con_imagen=False, con_activo=True),
                          exige_cat=True, exige_gen=True)
    if candidatos:
        log.info("get_productos_para_recomendar: intento B (sin imagen) cat=%s género=%s → %d", categoria_funcional, genero, len(candidatos))
        return _combinar_con_qdrant(candidatos)

    # Intento D: categoría + género, sin exigir imagen ni activo
    candidatos = _filtrar(await _query(con_imagen=False, con_activo=False),
                          exige_cat=True, exige_gen=True)
    if candidatos:
        log.info("get_productos_para_recomendar: intento D (sin imagen ni activo) cat=%s género=%s → %d", categoria_funcional, genero, len(candidatos))
        return _combinar_con_qdrant(candidatos)

    # Ningún intento SQL encontró candidatos NUEVOS que completar — si Qdrant al
    # menos había encontrado algo, mejor devolver ese puñado que nada.
    if filtrados_q:
        return filtrados_q[:limit]

    # Intento E-bis: RELAJAR LA CATEGORÍA por género. Cuando la intersección
    # (categoría + género) es vacía (ej: "vibradores" + "hombre" — los productos
    # de pene son anillos/fundas, no vibradores), probar las categorías
    # alternativas de ese género, siempre filtrando por género. Esto resuelve el
    # bug donde el cliente aclaraba "para el pene" y el bot no enviaba ninguna
    # foto porque no había vibradores de hombre.
    # BONUS DE COMBINACIÓN: si el cliente pidió una combinación tipo "vibrador
    # anal" (categoría original vibradores + género anal), los productos de la
    # categoría alternativa que TAMBIÉN cumplan la categoría original (un plug
    # anal que vibra) suben al tope. Así no se diluyen los pocos productos que
    # combinan ambas intenciones entre muchos que solo cumplen el género.
    # NO se relaja la categoría cuando el cliente pidió un subtipo CONCRETO (látigos,
    # sabores, duchas anales...): ahí "no hay" es la respuesta correcta y el turno debe
    # escalar, no rellenar con lo que sea del género. Este bloque ignoraba `subtipo` por
    # completo, y era el que mandaba anillos vibradores al azar a un cliente que había
    # preguntado por látigos y luego por lubricantes de sabores.
    subtipo_hard = bool(subtipo) and not _es_subtipo_soft(subtipo)
    if categoria_funcional and genero and not subtipo_hard:
        cat_original_tokens = {
            "vibradores": ("vibr", "vibrador", "vibrator"),
            "dildos": ("dildo", "consolador"),
            "masturbadores": ("masturb",),
            "succionadores": ("succion", "suction"),
            "lubricantes-y-cuidado": ("lubric",),
        }.get(categoria_funcional, ())
        alt_cats = [c for c in _CATEGORIAS_ALTERNATIVAS_POR_GENERO.get(genero, [])
                    if c != categoria_funcional]
        acumulado = []
        for alt_cat in alt_cats:
            for con_img, con_act in [(True, True), (True, False), (False, True), (False, False)]:
                rows = await _query(con_imagen=con_img, con_activo=con_act)
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
                    if cat_original_tokens:
                        nombre_desc = _normalizar_texto(f"{p.get('nombre','')} {p.get('descripcion','')}")
                        if any(tok in nombre_desc for tok in cat_original_tokens):
                            p["_score"] += 10.0
                    res.append(p)
                if res:
                    acumulado.extend(res)
            if len(acumulado) >= limit:
                break
        if acumulado:
            acumulado.sort(key=lambda p: (-p["_score"], len(p["nombre"])))
            log.info("E-bis relaja %s→%s gen=%s -> %d (acumulado)", categoria_funcional, alt_cats, genero, len(acumulado))
            return acumulado[:limit]

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

    if not candidatos and qdrant_candidatos_cache:
        # Ultimo recurso: Qdrant sin filtro estricto pero con imagen
        validos = [p for p in qdrant_candidatos_cache if p.get("imagen_url") and p.get("id") not in exclude_set]
        if validos:
            log.info("Fallback Qdrant sin filtro estricto -> %d", len(validos))
            return validos[:limit]

    if not candidatos:
        log.warning("get_productos_para_recomendar: 0 candidatos tras todos los intentos cat=%s género=%s", categoria_funcional, genero)
    return candidatos


# ══════════════════════════════════════════════════════════════════════════
# Recuperación por restricciones (ver app/facetas.py)
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Resultado:
    """Productos encontrados y, si hubo que ceder, QUÉ restricción se soltó.

    `relajado` es la pieza que faltaba: antes, cuando la intersección quedaba
    vacía, los fallbacks rellenaban con lo que hubiera cerca sin decirlo, y por
    eso "vibrador anal" devolvía enemas de limpieza. Ahora quien redacta el
    mensaje sabe que hay que avisar ("no tengo vibradores anales, pero estos
    plugs anales sí vibran").
    """
    productos: list[dict] = field(default_factory=list)
    relajado: str | None = None
    restricciones: dict = field(default_factory=dict)


# Orden en que se ceden las restricciones cuando no hay resultados exactos, de lo
# más accesorio a lo más esencial.
#
# LA ZONA DEL CUERPO NO ESTÁ EN LA LISTA: no se cede nunca. Ceder en la FORMA del
# juguete mantiene la petición (un "vibrador para el pene" que no existe como tal
# se responde con anillos vibradores, que sí); ceder en la ZONA la convierte en
# otra cosa. Reportado en producción: al pedir "vibradores" → "pene" se soltaba la
# zona y llegaban vibradores de mujer.
#
# Medido sobre las 42 combinaciones que los menús del bot pueden producir, con
# las facetas reales del catálogo: con la zona en la escalera hay 1 respuesta que
# contradice lo pedido; sin ella, ninguna.
#
_ESCALERA_RELAJACION = ("control", "genero_uso", "tipo", "vibra")


async def _fetch_restricciones(sql: str, *params) -> list[dict]:
    """Único punto que toca la DB en esta ruta. Aparte para poder probar qué
    productos entran en la página sin levantar Postgres."""
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        return [dict(r) for r in await conn.fetch(sql, *params)]


async def _consultar_restricciones(restricciones: dict, exclude_ids: list[int] | None,
                                   limit: int, user_text: str = "",
                                   subtipo: str | None = None) -> list[dict]:
    """Una consulta SQL con las restricciones dadas. Filtra en la DB, no en Python.

    Con `user_text` ordena por CONCORDANCIA en Python: el SQL no sabe qué
    escribió el cliente, y el viejo ORDER BY LENGTH(nombre) dejaba los
    "Succionador ..." fuera del LIMIT. `subtipo` filtra además por
    nombre/descripción (ver `_filtrar_por_subtipo`).
    """
    where = ["(stock_status IS NULL OR stock_status <> 'outofstock')",
             "imagen_url IS NOT NULL AND imagen_url != ''"]
    params: list = []
    idx = 1
    for campo in ("tipo", "zona", "control", "genero_uso"):
        valor = restricciones.get(campo)
        if valor:
            where.append(f"{campo} = ${idx}")
            params.append(valor)
            idx += 1
    if restricciones.get("vibra"):
        where.append("vibra = TRUE")
    atributos = restricciones.get("atributos") or []
    if atributos:
        where.append(f"atributos @> ${idx}::text[]")
        params.append(list(atributos))
        idx += 1
    if exclude_ids:
        where.append(f"NOT (id = ANY(${idx}::bigint[]))")
        params.append(list(exclude_ids))
        idx += 1
    # Con texto del cliente hay que traer el conjunto completo para poder
    # ordenarlo: cortar en SQL por longitud de nombre es justamente el fallo.
    # 200 cubre de sobra la categoría más grande del catálogo (76 vibradores) y
    # es menos de lo que ya pide `contar_por_restricciones` (500).
    n_filas = limit if not user_text else 200
    sql = (
        "SELECT id, nombre, descripcion, categoria, precio, imagen_url, galeria_urls, "
        "permalink, tipo, zona, vibra, control, genero_uso, atributos "
        "FROM productos WHERE " + " AND ".join(where) +
        f" ORDER BY (activo IS TRUE) DESC, LENGTH(nombre) ASC LIMIT {int(n_filas)}"
    )
    filas = await _fetch_restricciones(sql, *params)
    if subtipo:
        filtradas = _filtrar_por_subtipo(filas, subtipo)
        # Degradación deliberada: hay subtipos cuyo vocabulario no está en los
        # nombres del catálogo ("con app", "primera vez"). Filtrar a ciegas ahí
        # convertiría un listado válido en un escalado a humano. Solo se cede
        # cuando NO hay exclusiones: en un "ver más" el vacío ES la respuesta
        # ("ya viste todas las colegialas"), no una excusa para traer Mucamas.
        if filtradas or exclude_ids:
            filas = filtradas
        else:
            log.info("Subtipo %r sin coincidencias en %s — se muestra la categoría",
                     subtipo, restricciones)
    if not user_text:
        return filas[:limit]
    for p in filas:
        p["_score"] = _score_candidato(p, user_text)
    # Desempate por nombre corto: entre productos igual de relevantes conserva
    # el orden anterior, así que la paginación es estable.
    filas.sort(key=lambda p: (-p["_score"], len(p.get("nombre") or "")))
    return filas[:limit]


async def contar_por_restricciones(restricciones: dict,
                                   subtipo: str | None = None) -> int:
    """Cuántos productos ofrecibles cumplen EXACTAMENTE estas restricciones.

    Es lo que decide si tiene sentido ofrecer "ver más": con 3 opciones en total
    no se pregunta "¿deseas ver más diseños?", se pregunta cuál quiere.
    """
    restricciones = {k: v for k, v in (restricciones or {}).items()
                     if k != "_implicitos" and v}
    if not restricciones:
        return 0
    try:
        filas = await _consultar_restricciones(restricciones, None, 500, subtipo=subtipo)
        return len(filas)
    except Exception:
        return 0


async def facetas_disponibles(restricciones: dict) -> dict:
    """Qué facetas tienen producto OFRECIBLE dentro de estas restricciones.

    Devuelve {"atributos": {"sabor": 6, …}, "zonas": {…}, "generos": {…}}.

    Es lo que permite preguntarle al cliente solo por ramas que existen. Ofrecer
    "de silicona" cuando no queda ninguno es peor que no preguntar: el cliente la
    elige, la consulta exacta da 0 filas, y `_ESCALERA_RELAJACION` suelta el
    atributo devolviéndole justo lo que no pidió.

    Cuenta sobre las mismas filas que devolvería la búsqueda (agregando en
    Python, no en SQL): son pocos cientos de productos y así no hay dos WHERE
    que puedan divergir.
    """
    vacio = {"atributos": {}, "zonas": {}, "generos": {}}
    restricciones = {k: v for k, v in (restricciones or {}).items()
                     if k != "_implicitos" and v}
    if not restricciones:
        return vacio
    try:
        filas = await _consultar_restricciones(restricciones, None, 500)
    except Exception:
        log.warning("No se pudieron contar las facetas disponibles", exc_info=True)
        return vacio

    atributos: dict[str, int] = {}
    zonas: dict[str, int] = {}
    generos: dict[str, int] = {}
    for fila in filas:
        for attr in fila.get("atributos") or []:
            atributos[attr] = atributos.get(attr, 0) + 1
        if zona := fila.get("zona"):
            zonas[zona] = zonas.get(zona, 0) + 1
        if genero := fila.get("genero_uso"):
            generos[genero] = generos.get(genero, 0) + 1
    return {"atributos": atributos, "zonas": zonas, "generos": generos}


async def buscar_por_restricciones(restricciones: dict,
                                   exclude_ids: list[int] | None = None,
                                   limit: int = 5,
                                   permitir_relajar: bool = True,
                                   user_text: str = "",
                                   subtipo: str | None = None) -> Resultado:
    """Busca productos que cumplan las restricciones, cediendo de forma explícita.

    1. Intenta la combinación exacta.
    2. Si no hay nada, suelta UNA restricción por vez siguiendo
       `_ESCALERA_RELAJACION` y devuelve cuál soltó.
    3. Si tras soltarlas todas sigue sin haber nada, devuelve vacío. Nunca
       rellena con productos de otro tipo en silencio: eso es lo que hacía que
       un cliente pidiera un vibrador anal y recibiera un enema.
    """
    restricciones = {k: v for k, v in (restricciones or {}).items()
                     if k != "_implicitos" and v}
    if not restricciones:
        return Resultado(restricciones=restricciones)

    exactos = await _consultar_restricciones(restricciones, exclude_ids, limit, user_text,
                                             subtipo=subtipo)
    if exactos:
        return Resultado(productos=exactos, restricciones=restricciones)

    # Sin relajación (caso "ver más"): si ya se mostró todo lo que cumple, la
    # respuesta correcta es "no queda más", no rellenar con otra cosa. Pasó en
    # producción: había 3 vibradores anales, se mostraron los 3, y al pedir "ver
    # más diseños" se soltaba la zona y llegaban vibradores genéricos.
    if not permitir_relajar:
        return Resultado(restricciones=restricciones)

    for campo in _ESCALERA_RELAJACION:
        if campo not in restricciones:
            continue
        aflojadas = {k: v for k, v in restricciones.items() if k != campo}
        if not aflojadas.get("tipo") and not aflojadas.get("zona"):
            continue  # soltar eso dejaría la búsqueda sin ancla
        encontrados = await _consultar_restricciones(aflojadas, exclude_ids, limit, user_text,
                                                     subtipo=subtipo)
        if encontrados:
            log.info("Restricción relajada: %s (quedan %s) → %d productos",
                     campo, aflojadas, len(encontrados))
            return Resultado(productos=encontrados, relajado=campo,
                             restricciones=aflojadas)

    # Último recurso. Si el cliente nombró una ZONA, es lo único que se respeta:
    # nunca se cae a "solo el tipo", porque eso devolvería productos de otra parte
    # del cuerpo, que es exactamente el fallo que se corrigió.
    #
    # Y si nombró un ATRIBUTO no hay último recurso que valga: "otros dildos" no
    # es una respuesta parcial a "un dildo doble", es otro producto. Quien llame
    # decide qué hacer con el vacío; rellenarlo aquí es lo que ponía cinco fotos
    # de realistas delante de quien pidió dobles.
    ancla = "zona" if restricciones.get("zona") else "tipo"
    if restricciones.get(ancla) and not restricciones.get("atributos"):
        solo = {ancla: restricciones[ancla]}
        encontrados = await _consultar_restricciones(solo, exclude_ids, limit, user_text,
                                                     subtipo=subtipo)
        if encontrados:
            log.info("Solo se pudo respetar %s=%s → %d productos",
                     ancla, restricciones[ancla], len(encontrados))
            return Resultado(productos=encontrados,
                             relajado="zona" if ancla == "tipo" else "tipo",
                             restricciones=solo)

    # Nada cumple ni cediendo lo cedible. Se responde con honestidad, sin fotos.
    log.warning("Sin productos para %s (ni relajando)", restricciones)
    return Resultado(relajado="sin_resultado", restricciones=restricciones)


async def contar_productos(categoria_funcional: str | None, genero: str | None) -> int:
    try:
        if not categoria_funcional and not genero:
            return 0
        where = ["(stock_status IS NULL OR stock_status <> 'outofstock')", "imagen_url IS NOT NULL AND imagen_url != ''", "activo = TRUE"]
        sql = "SELECT id, nombre, descripcion, categoria FROM productos WHERE " + " AND ".join(where)
        async with db._pool.acquire() as conn:
            rows = await conn.fetch(sql)
        count = 0
        for r in rows:
            cat = _categoria_normalizada(r["nombre"], r["descripcion"], r["categoria"])
            gen = _genero_normalizado(r["nombre"], r["descripcion"], r["categoria"])
            if categoria_funcional and cat != categoria_funcional:
                continue
            if genero and gen != genero:
                continue
            count += 1
        return count
    except Exception:
        return 0


async def _fetch_especificos(sql: str, *params) -> list[dict]:
    """Único punto que toca la DB en la búsqueda por nombre. Aparte para poder
    probar el filtrado sin levantar Postgres."""
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        return [dict(r) for r in await conn.fetch(sql, *params)]


async def buscar_producto_especifico(user_text: str, limit: int = 3,
                                     exclude_ids: list[int] | None = None,
                                     tipo: str | None = None) -> list[dict]:
    """Productos cuyo NOMBRE coincide con lo que escribió el cliente.

    Para el pipeline: cuando el texto menciona un producto concreto ("tienen el
    Lovense Diamo?"), recuperarlo directo en vez de listar su categoría.

    Exige al menos una coincidencia en el NOMBRE. Antes bastaba llegar a un
    score de 1.0, y como una coincidencia en la descripción vale 0.5, dos
    palabras de relleno ("que" + "dildos") lo alcanzaban: un "Hola, que dildos
    tinen" devolvía 2 productos al azar y, por la vía de `es_especifico`, esos 2
    sustituían al listado de los 22 dildos de la categoría.

    Con `tipo`, la búsqueda se restringe a esa faceta: "dildos Tenera" busca
    Tenera entre los dildos y no puede traer nada de otra categoría.

    exclude_ids: IDs a excluir (productos ya mostrados, para no repetir fotos al
    pedir "ver más"). Retrocompatible: si es None, no excluye nada.
    """
    if not user_text or len(user_text.strip()) < 3:
        return []
    tokens = _extract_search_tokens(user_text)
    if not tokens:
        return []
    exclude_set = set(exclude_ids or [])
    where = ["activo = TRUE",
             "(stock_status IS NULL OR stock_status <> 'outofstock')",
             "imagen_url IS NOT NULL AND imagen_url != ''"]
    params: list = []
    if tipo:
        where.append(f"tipo = ${len(params) + 1}")
        params.append(tipo)
    sql = ("SELECT id, nombre, descripcion, categoria, precio, imagen_url, "
           "galeria_urls, permalink FROM productos WHERE " + " AND ".join(where))
    rows = await _fetch_especificos(sql, *params)

    scored = []
    for p in rows:
        if p["id"] in exclude_set:
            continue
        # Tiene que casar por marca/modelo, no por la palabra de la categoría:
        # "dildos" está en el nombre de los 22 dildos y "encontraría" cualquiera.
        if _score_nombre_discriminante(p, user_text) < 2.0:
            continue
        p["_score"] = _score_candidato(p, user_text)
        scored.append(p)
    scored.sort(key=lambda p: (-p["_score"], len(p["nombre"])))
    return scored[:limit]
