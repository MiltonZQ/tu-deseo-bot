"""Cliente de WooCommerce API + sincronización suave de catálogo y webhooks.

Maneja la importación de productos desde la tienda en línea (WordPress/WooCommerce),
preservando la estabilidad del servidor en producción (rate limit suave) y almacenando
precios, stock e imágenes para el envío automático de fotos por WhatsApp.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging

import httpx

from app import config, db, catalog

log = logging.getLogger("woocommerce")


def _get_auth_header() -> dict[str, str]:
    auth_str = f"{config.WOOCOMMERCE_CONSUMER_KEY}:{config.WOOCOMMERCE_CONSUMER_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    return {
        "Authorization": f"Basic {b64_auth}",
        "User-Agent": "TuDeseoBot/1.0",
    }


def verify_signature(payload_body: bytes, signature_header: str) -> bool:
    """Verifica la firma HMAC-SHA256 enviada por WooCommerce en el encabezado X-WC-Webhook-Signature."""
    secret = config.WOOCOMMERCE_WEBHOOK_SECRET
    if not secret or not signature_header:
        return True  # Si no hay secreto configurado, omitir validación estricta
    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), payload_body, hashlib.sha256).digest()
    ).decode("utf-8")
    return hmac.compare_digest(expected.strip(), signature_header.strip())


async def fetch_all_products(per_page: int = 50, delay_seconds: float = 0.3) -> list[dict]:
    """Descarga todos los productos publicados de WooCommerce paginando suavemente.

    `delay_seconds` evita saturar el CPU o la base de datos del WordPress en producción.
    """
    if not config.WOOCOMMERCE_URL or not config.WOOCOMMERCE_CONSUMER_KEY:
        log.warning("WooCommerce no está configurado (WOOCOMMERCE_URL o claves vacías)")
        return []

    base_url = config.WOOCOMMERCE_URL.rstrip("/")
    api_url = f"{base_url}/wp-json/wc/v3/products"
    headers = _get_auth_header()
    all_products = []
    page = 1

    import asyncio

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            params = {
                "status": "publish",
                "per_page": per_page,
                "page": page,
            }
            try:
                response = await client.get(api_url, headers=headers, params=params)
                if response.status_code != 200:
                    log.error("Error consultando WooCommerce API: %d - %s", response.status_code, response.text[:200])
                    break

                items = response.json()
                if not items:
                    break

                all_products.extend(items)
                log.info("WooCommerce sync: descargada página %d (%d productos)", page, len(items))

                total_pages = int(response.headers.get("X-WP-TotalPages", 1))
                if page >= total_pages:
                    break

                page += 1
                await asyncio.sleep(delay_seconds)  # Rate limiting suave respetuoso
            except Exception as e:
                log.exception("Excepción durante descarga de WooCommerce API en página %d: %s", page, e)
                break

    log.info("WooCommerce: descarga finalizada con %d productos totales", len(all_products))
    return all_products


def _clean_html(text: str | None) -> str:
    if not text:
        return ""
    import re
    clean = re.sub(r"<[^>]+>", "", text)
    return clean.strip()


async def _clasificar_y_guardar(producto_id: int, nombre: str,
                                descripcion: str | None, categoria: str | None,
                                permitir_llm: bool = True) -> None:
    """Calcula y guarda las facetas de un producto (ver app/facetas.py).

    Nunca rompe la sincronización: si la clasificación falla, el producto queda
    con las facetas que ya tuviera y el backfill lo recoge después.
    `set_facetas_producto` respeta lo corregido a mano desde el panel.
    """
    try:
        from app import facetas
        f, origen = await facetas.clasificar(
            nombre, descripcion, categoria, permitir_llm=permitir_llm)
        if f.tipo:
            await db.set_facetas_producto(producto_id, f, origen=origen)
    except Exception:
        log.warning("No se pudieron calcular las facetas de '%s'", nombre[:50])


async def _clasificar_pendientes_con_llm(limite: int = 60) -> int:
    """Resuelve con el LLM los productos que las reglas no supieron clasificar.

    Corre UNA vez al final del sync y está acotado: sobre el catálogo real son
    unos 6 de 246, pero el tope evita que un cambio masivo dispare el coste.
    """
    try:
        pendientes = await db.get_productos_sin_clasificar(limit=limite)
    except Exception:
        return 0
    for p in pendientes:
        await _clasificar_y_guardar(
            p["id"], p["nombre"], p.get("descripcion"), p.get("categoria"),
            permitir_llm=True)
    if pendientes:
        log.info("Facetas resueltas con LLM para %d productos", len(pendientes))
    return len(pendientes)


async def sync_catalog_from_woocommerce(full_replace: bool = True) -> dict:
    """Sincroniza la tabla `productos` con los datos de WooCommerce.

    Si `full_replace=True`, limpia la tabla actual para que únicamente se manejen
    los productos y precios extraídos del sitio web.
    """
    raw_products = await fetch_all_products()
    if not raw_products:
        return {"error": "No se pudieron obtener productos de WooCommerce"}

    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        if full_replace:
            log.info("Sincronización WooCommerce iniciada en modo actualización de productos...")

        insertados = 0
        actualizados = 0

        for item in raw_products:
            woo_id = item.get("id")
            nombre = (item.get("name") or "").strip()
            if not woo_id or not nombre:
                continue

            desc_short = _clean_html(item.get("short_description"))
            desc_full = _clean_html(item.get("description"))
            descripcion = desc_short or desc_full[:300] if desc_full else None

            cats = item.get("categories") or []
            categoria = cats[0].get("name") if cats else None

            # Precios
            price_raw = item.get("price") or item.get("regular_price") or "0"
            try:
                precio = int(float(price_raw))
            except ValueError:
                precio = 0

            try:
                reg_p = int(float(item.get("regular_price"))) if item.get("regular_price") else None
            except (ValueError, TypeError):
                reg_p = None

            try:
                sale_p = int(float(item.get("sale_price"))) if item.get("sale_price") else None
            except (ValueError, TypeError):
                sale_p = None

            sku_pos = (item.get("sku") or "").strip() or None
            stock_status = item.get("stock_status") or "instock"
            permalink = item.get("permalink") or None

            # Imágenes
            images = item.get("images") or []
            imagen_url = images[0].get("src") if images else None
            galeria_urls = json.dumps([img.get("src") for img in images if img.get("src")]) if images else None

            activo = (item.get("status") == "publish") and (
                stock_status == "instock" or stock_status == "onbackorder"
            )

            res = await conn.fetchrow(
                """
                INSERT INTO productos (
                    woo_id, nombre, descripcion, categoria, precio, precio_regular, precio_oferta,
                    sku_pos, stock_status, imagen_url, galeria_urls, permalink, activo, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, now())
                ON CONFLICT (woo_id) DO UPDATE SET
                    nombre = EXCLUDED.nombre,
                    descripcion = EXCLUDED.descripcion,
                    categoria = EXCLUDED.categoria,
                    precio = EXCLUDED.precio,
                    precio_regular = EXCLUDED.precio_regular,
                    precio_oferta = EXCLUDED.precio_oferta,
                    sku_pos = EXCLUDED.sku_pos,
                    stock_status = EXCLUDED.stock_status,
                    imagen_url = EXCLUDED.imagen_url,
                    galeria_urls = EXCLUDED.galeria_urls,
                    permalink = EXCLUDED.permalink,
                    activo = EXCLUDED.activo,
                    updated_at = now()
                RETURNING id
                """,
                woo_id, nombre, descripcion, categoria, precio, reg_p, sale_p,
                sku_pos, stock_status, imagen_url, galeria_urls, permalink, activo
            )
            if res:
                insertados += 1
                # Facetas con REGLAS dentro del bucle: es gratis e instantáneo, y
                # deja el producto consultable de inmediato. Los pocos que las
                # reglas no deciden se resuelven abajo con el LLM, fuera del bucle,
                # para no meter cientos de llamadas de red en la sincronización.
                await _clasificar_y_guardar(
                    res["id"], nombre, descripcion, categoria, permitir_llm=False)

    await _clasificar_pendientes_con_llm()

    # Actualizar prompt de conocimiento
    await catalog.export_knowledge_md()
    if config.QDRANT_ENABLED:
        try:
            from app import vector_store
            await vector_store.sync_qdrant_from_db()
        except Exception:
            log.exception("Error sincronizando Qdrant tras sync de WooCommerce")

    log.info("Sincronización WooCommerce finalizada: %d productos procesados", insertados)


    # Diagnóstico post-sync: cuántos productos hay con imagen/activo (para detectar
    # si los filtros del bot van a encontrar candidatos).
    try:
        async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
            total_db = await conn.fetchval("SELECT COUNT(*) FROM productos")
            con_imagen = await conn.fetchval("SELECT COUNT(*) FROM productos WHERE imagen_url IS NOT NULL AND imagen_url != ''")
            activos = await conn.fetchval("SELECT COUNT(*) FROM productos WHERE activo = TRUE")
            susp_con_img = await conn.fetchval("SELECT COUNT(*) FROM productos WHERE nombre ILIKE '%suspensor%' AND imagen_url IS NOT NULL AND imagen_url != ''")
        log.info("Diagnóstico DB: %d productos | con imagen: %d | activos: %d | suspensorios con imagen: %d",
                 total_db, con_imagen, activos, susp_con_img)
    except Exception:
        log.exception("Error en diagnóstico post-sync")

    return {"total": len(raw_products), "sincronizados": insertados}


async def process_webhook_payload(payload: dict, event_topic: str) -> bool:
    """Procesa eventos de webhook desde WooCommerce (product.created, product.updated, product.deleted)."""
    if not payload:
        return False

    woo_id = payload.get("id")
    if not woo_id:
        return False

    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        if "deleted" in event_topic.lower():
            await conn.execute("DELETE FROM productos WHERE woo_id = $1", woo_id)
            log.info("Webhook WooCommerce: Producto woo_id=%d eliminado de DB", woo_id)
        else:
            nombre = (payload.get("name") or "").strip()
            if not nombre:
                return False

            desc_short = _clean_html(payload.get("short_description"))
            desc_full = _clean_html(payload.get("description"))
            descripcion = desc_short or desc_full[:300] if desc_full else None

            cats = payload.get("categories") or []
            categoria = cats[0].get("name") if cats else None

            price_raw = payload.get("price") or payload.get("regular_price") or "0"
            try:
                precio = int(float(price_raw))
            except ValueError:
                precio = 0

            try:
                reg_p = int(float(payload.get("regular_price"))) if payload.get("regular_price") else None
            except (ValueError, TypeError):
                reg_p = None

            try:
                sale_p = int(float(payload.get("sale_price"))) if payload.get("sale_price") else None
            except (ValueError, TypeError):
                sale_p = None

            sku_pos = (payload.get("sku") or "").strip() or None
            stock_status = payload.get("stock_status") or "instock"
            permalink = payload.get("permalink") or None

            images = payload.get("images") or []
            imagen_url = images[0].get("src") if images else None
            galeria_urls = json.dumps([img.get("src") for img in images if img.get("src")]) if images else None

            activo = (payload.get("status") == "publish") and (stock_status == "instock")

            fila = await conn.fetchrow(
                """
                INSERT INTO productos (
                    woo_id, nombre, descripcion, categoria, precio, precio_regular, precio_oferta,
                    sku_pos, stock_status, imagen_url, galeria_urls, permalink, activo, updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, now())
                ON CONFLICT (woo_id) DO UPDATE SET
                    nombre = EXCLUDED.nombre,
                    descripcion = EXCLUDED.descripcion,
                    categoria = EXCLUDED.categoria,
                    precio = EXCLUDED.precio,
                    precio_regular = EXCLUDED.precio_regular,
                    precio_oferta = EXCLUDED.precio_oferta,
                    sku_pos = EXCLUDED.sku_pos,
                    stock_status = EXCLUDED.stock_status,
                    imagen_url = EXCLUDED.imagen_url,
                    galeria_urls = EXCLUDED.galeria_urls,
                    permalink = EXCLUDED.permalink,
                    activo = EXCLUDED.activo,
                    updated_at = now()
                RETURNING id
                """,
                woo_id, nombre, descripcion, categoria, precio, reg_p, sale_p,
                sku_pos, stock_status, imagen_url, galeria_urls, permalink, activo
            )
            log.info("Webhook WooCommerce: Producto woo_id=%d ('%s') actualizado en DB", woo_id, nombre)

        # Un solo producto: aquí sí vale la pena consultar al LLM si hace falta.
        if fila:
            await _clasificar_y_guardar(fila["id"], nombre, descripcion, categoria)

    # Actualizar catalogo.md en segundo plano
    await catalog.export_knowledge_md()
    return True
