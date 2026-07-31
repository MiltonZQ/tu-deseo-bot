"""Cliente de Vector Store con Qdrant HTTP REST API y OpenAI Embeddings.

Permite búsqueda semántica de productos por necesidades, características o beneficios
(ej: "algo para venir sin manos", "control por app", "resistente al agua").
"""
import logging
from typing import Optional, List, Dict, Any

import httpx
from app import config

log = logging.getLogger("vector_store")


_qdrant_available = False


def _get_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if config.QDRANT_API_KEY:
        headers["api-key"] = config.QDRANT_API_KEY
    return headers


def _qdrant_urls() -> list[str]:
    urls = []
    if config.QDRANT_URL:
        urls.append(config.QDRANT_URL.rstrip('/'))
    urls.extend([
        "http://pjuo95m2fo92350i673n4cyo:6333",
        "http://qdrant-pjuo95m2fo92350i673n4cyo:6333",
        "http://qdrant:6333",
    ])
    seen = []
    for u in urls:
        if u not in seen:
            seen.append(u)
    return seen


async def init_vector_store() -> None:
    global _qdrant_available
    if not config.QDRANT_ENABLED:
        log.info("Qdrant desactivado o sin QDRANT_URL")
        _qdrant_available = False
        return
    for base in _qdrant_urls():
        url = f"{base}/collections/{config.QDRANT_COLLECTION_NAME}"
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.get(url, headers=_get_headers())
                if resp.status_code == 200:
                    log.info("Coleccion Qdrant '%s' ya existe en %s", config.QDRANT_COLLECTION_NAME, base)
                    _qdrant_available = True
                    return
                log.info("Creando coleccion Qdrant '%s' en %s", config.QDRANT_COLLECTION_NAME, base)
                payload = {"vectors": {"size": 1536, "distance": "Cosine"}}
                create_resp = await client.put(url, headers=_get_headers(), json=payload)
                if create_resp.status_code in (200, 201):
                    log.info("Coleccion Qdrant '%s' creada en %s", config.QDRANT_COLLECTION_NAME, base)
                    _qdrant_available = True
                    return
                log.warning("Fallo crear coleccion %s en %s: %s", config.QDRANT_COLLECTION_NAME, base, create_resp.text[:200])
        except Exception as exc:
            log.warning("Excepcion Qdrant init %s: %s", base, exc)
    log.warning("No se pudo inicializar Qdrant en ninguna URL %s - fallback a SQL", _qdrant_urls())
    _qdrant_available = False


async def generate_embeddings(texts: List[str]) -> List[List[float]]:
    """Genera embeddings vectoriales usando text-embedding-3-small de OpenAI."""
    if not texts:
        return []
    base_url = config.OPENAI_BASE_URL.rstrip('/') if config.OPENAI_BASE_URL else "https://api.openai.com/v1"
    url = f"{base_url}/embeddings"
    headers = {
        "Authorization": f"Bearer {config.OPENAI_EMBEDDING_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": config.OPENAI_EMBEDDING_MODEL,
        "input": [t[:2000] for t in texts],
    }
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code not in (200, 201):
            log.warning("Error generando embeddings: HTTP %s - %s", resp.status_code, resp.text)
            return []
        data = resp.json()
        embeddings = [item["embedding"] for item in data.get("data", [])]
        return embeddings
    except Exception as exc:
        log.warning("Excepción llamando a la API de Embeddings de OpenAI: %s", exc)
        return []


def _construir_texto_producto(p: Dict[str, Any]) -> str:
    nombre = (p.get("nombre") or "").strip()
    desc = (p.get("descripcion") or "").strip()
    cat = (p.get("categoria_funcional") or "").strip()
    genero = (p.get("genero") or "unisex").strip()
    subtipo = (p.get("subtipo") or "").strip()
    partes = [
        f"{nombre}",
        f"Categoria funcional {cat}",
        f"Uso {genero}",
    ]
    if "app" in nombre.lower() or "app" in desc.lower():
        partes.append("control por app aplicacion movil bluetooth")
    if subtipo:
        partes.append(f"{subtipo}")
    if desc:
        partes.append(f"{desc[:400]}")
    return " ".join(partes)


async def upsert_batch_products(products: List[Dict[str, Any]]) -> int:
    if not config.QDRANT_ENABLED or not products:
        return 0
    if not _qdrant_available:
        log.info("Qdrant no disponible, skip upsert %d productos", len(products))
        return 0

    texts = [_construir_texto_producto(p) for p in products]
    embeddings = await generate_embeddings(texts)
    if not embeddings or len(embeddings) != len(products):
        log.warning("No se pudieron obtener embeddings suficientes (%d/%d)", len(embeddings), len(products))
        return 0

    points = []
    for p, emb in zip(products, embeddings):
        pid = int(p["id"])
        payload = {
            "id": pid,
            "nombre": p.get("nombre"),
            "precio": p.get("precio", 0),
            "imagen_url": p.get("imagen_url"),
            "categoria_funcional": p.get("categoria_funcional"),
            "genero": p.get("genero"),
            "stock": p.get("stock", 0),
            "subtipo": p.get("subtipo"),
            "woo_id": p.get("woo_id"),
            "descripcion": (p.get("descripcion") or "")[:300],
        }
        points.append({
            "id": pid,
            "vector": emb,
            "payload": payload,
        })

    for base in _qdrant_urls():
        url = f"{base}/collections/{config.QDRANT_COLLECTION_NAME}/points?wait=true"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.put(url, headers=_get_headers(), json={"points": points})
            if resp.status_code in (200, 201):
                log.info("Indexados %d puntos Qdrant en %s", len(points), base)
                return len(points)
            log.warning("Fallo upsert Qdrant %s: %s %s", base, resp.status_code, resp.text[:200])
        except Exception as exc:
            log.warning("Excepcion upsert Qdrant %s: %s", base, exc)
    return 0


async def search_semantic_products(
    query: str,
    categoria_funcional: Optional[str] = None,
    genero: Optional[str] = None,
    exclude_ids: Optional[List[int]] = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    if not config.QDRANT_ENABLED or not query.strip():
        return []
    if not _qdrant_available:
        return []
    embeddings = await generate_embeddings([query])
    if not embeddings:
        return []
    vector = embeddings[0]
    threshold = 0.50
    try:
        env_thr = float((getattr(config, "QDRANT_SCORE_THRESHOLD", "") or "0.50"))
        threshold = env_thr
    except ValueError:
        pass
    if len(query.strip().split()) <= 2:
        threshold = max(threshold, 0.55)

    filter_conditions = []
    if categoria_funcional:
        filter_conditions.append({"key": "categoria_funcional", "match": {"value": categoria_funcional}})
    if genero:
        filter_conditions.append({"key": "genero", "match": {"value": genero}})

    qdrant_filter = {}
    if filter_conditions:
        qdrant_filter["must"] = filter_conditions
    if exclude_ids:
        qdrant_filter["must_not"] = [{"has_id": exclude_ids}]

    search_payload = {"vector": vector, "limit": limit * 2, "with_payload": True}
    if qdrant_filter:
        search_payload["filter"] = qdrant_filter

    for base in _qdrant_urls():
        url = f"{base}/collections/{config.QDRANT_COLLECTION_NAME}/points/search"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, headers=_get_headers(), json=search_payload)
            if resp.status_code != 200:
                log.warning("Error Qdrant search %s: %s %s", base, resp.status_code, resp.text[:200])
                continue
            results = resp.json().get("result", [])
            candidatos = []
            for res in results:
                p = res.get("payload") or {}
                score = res.get("score", 0.0)
                if score < threshold:
                    continue
                if not p.get("imagen_url"):
                    continue
                p["_score"] = score
                candidatos.append(p)
                if len(candidatos) >= limit:
                    break
            log.info("Qdrant '%s' -> %d candidatos thr %.2f cat=%s gen=%s via %s", query[:40], len(candidatos), threshold, categoria_funcional, genero, base)
            return candidatos
        except Exception as exc:
            log.warning("Excepcion Qdrant search %s: %s", base, exc)
    return []


async def sync_qdrant_from_db() -> int:
    if not config.QDRANT_ENABLED:
        return 0
    try:
        from app import db, catalog
        async with db._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, woo_id, nombre, descripcion, categoria, precio, imagen_url, stock_status
                FROM productos
                WHERE activo = TRUE
                  AND imagen_url IS NOT NULL AND imagen_url != ''
                  AND (stock_status IS NULL OR stock_status != 'outofstock')
                """
            )
        if not rows:
            return 0
        products = []
        for r in rows:
            p = dict(r)
            cat_func = catalog._categoria_normalizada(p["nombre"], p.get("descripcion"), p.get("categoria"))
            genero = catalog._genero_normalizado(p["nombre"], p.get("descripcion"), p.get("categoria"))
            p["categoria_funcional"] = cat_func
            p["genero"] = genero
            p["stock"] = 1
            products.append(p)
        indexed_count = 0
        batch_size = 30
        for i in range(0, len(products), batch_size):
            batch = products[i:i+batch_size]
            indexed_count += await upsert_batch_products(batch)
        log.info("Indexados %d productos en Qdrant desde DB (filtrados con imagen)", indexed_count)
        return indexed_count
    except Exception as exc:
        log.warning("Error al sincronizar Qdrant desde DB: %s", exc)
        return 0

