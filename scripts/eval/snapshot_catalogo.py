"""Congela el catálogo REAL de producción en un JSON. SOLO LECTURA.

La evaluación de respuestas necesita el catálogo tal como lo ve el bot, no uno
inventado: los tests de la suite construyen sus productos a mano y por eso no
detectan que un subtipo se quedó sin cobertura o que un atributo no está
etiquetado en la tabla.

No se puede leer la tabla `productos` directamente —el Postgres de Coolify no
está expuesto y `DATABASE_URL` apunta al host `db` de la red interna—, así que
la fila se reconstruye uniendo las dos únicas fuentes de solo lectura que sí
alcanzan a producción:

  * el panel admin (`GET /admin/productos`), que renderiza las FACETAS TAL COMO
    ESTÁN GUARDADAS: tipo, zona, vibra, control, genero_uso y atributos. Es lo
    único que refleja la columna `atributos` real, incluidas las correcciones
    manuales que el sync ya no pisa.
  * la API de WooCommerce, de donde el sync copia nombre, descripción, precio,
    imágenes y categoría. Se aplican aquí las MISMAS transformaciones que
    `woocommerce.sync_products` (descripción corta o larga recortada a 300, la
    PRIMERA categoría, `activo` según estado y stock) para que la fila resultante
    sea indistinguible de la que hay en la tabla.

El nombre completo sale de WooCommerce porque el panel lo trunca a 58
caracteres; ese truncado es justamente la clave del join.

Uso:
    python3 scripts/eval/snapshot_catalogo.py [--salida scripts/eval/catalogo.json]

Sale con código 1 si el join no cubre todos los productos: un desfase entre las
dos fuentes invalidaría la evaluación entera, y es preferible enterarse aquí.
"""
from __future__ import annotations

import argparse
import base64
import html as H
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TRUNCADO_PANEL = 58


def cargar_env() -> dict[str, str]:
    """Lee `.env` a mano.

    No se usa `source` ni `dotenv`: `COOLIFY_TOKEN` contiene un `|` que el shell
    interpreta como pipe, y `python-dotenv` no está instalado en el `.venv`.
    """
    env: dict[str, str] = {}
    for linea in (_ROOT / ".env").read_text().splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        env[clave.strip()] = valor.strip().strip('"').strip("'")
    return env


# ── WooCommerce ──

def descargar_woocommerce(env: dict[str, str]) -> list[dict]:
    base = env["WOOCOMMERCE_URL"].rstrip("/")
    auth = base64.b64encode(
        f'{env["WOOCOMMERCE_CONSUMER_KEY"]}:{env["WOOCOMMERCE_CONSUMER_SECRET"]}'.encode()
    ).decode()
    productos: list[dict] = []
    pagina = 1
    while True:
        query = urllib.parse.urlencode({"status": "publish", "per_page": 50, "page": pagina})
        req = urllib.request.Request(f"{base}/wp-json/wc/v3/products?{query}",
                                     headers={"Authorization": "Basic " + auth})
        with urllib.request.urlopen(req, timeout=60) as resp:
            items = json.load(resp)
        if not items:
            break
        productos += items
        pagina += 1
        if pagina > 40:  # tope de seguridad; el catálogo ronda los 500
            break
        time.sleep(0.2)
    return productos


def _limpiar_html(texto: str | None) -> str:
    """Equivalente de `woocommerce._clean_html` sin depender del módulo.

    Importar `app.woocommerce` arrastraría `httpx`, que no está instalado.
    """
    if not texto:
        return ""
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = H.unescape(texto)
    return re.sub(r"\s+", " ", texto).strip()


def fila_desde_woocommerce(item: dict) -> dict:
    """Las mismas transformaciones que `woocommerce.sync_products`."""
    desc_short = _limpiar_html(item.get("short_description"))
    desc_full = _limpiar_html(item.get("description"))
    descripcion = (desc_short or desc_full[:300]) if desc_full else (desc_short or None)

    cats = item.get("categories") or []
    categoria = cats[0].get("name") if cats else None

    price_raw = item.get("price") or item.get("regular_price") or "0"
    try:
        precio = int(float(price_raw))
    except ValueError:
        precio = 0

    stock_status = item.get("stock_status") or "instock"
    imagenes = item.get("images") or []
    imagen_url = imagenes[0].get("src") if imagenes else None
    galeria = [img.get("src") for img in imagenes if img.get("src")]

    return {
        "woo_id": item.get("id"),
        "nombre": (item.get("name") or "").strip(),
        "descripcion": descripcion,
        "categoria": categoria,
        "precio": precio,
        "stock_status": stock_status,
        "imagen_url": imagen_url,
        "galeria_urls": json.dumps(galeria) if galeria else None,
        "permalink": item.get("permalink") or None,
        "activo": item.get("status") == "publish" and stock_status in ("instock", "onbackorder"),
    }


# ── Panel admin ──

def descargar_facetas(env: dict[str, str]) -> list[dict]:
    """Las facetas guardadas, leídas del panel. Solo los ofrecibles."""
    cookies = http.cookiejar.CookieJar()
    navegador = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookies))
    dominio = env["WEBHOOK_DOMAIN"].rstrip("/")
    navegador.open(
        f"{dominio}/admin/login",
        data=urllib.parse.urlencode({"username": env["ADMIN_USER"],
                                     "password": env["ADMIN_PASSWORD"]}).encode(),
        timeout=60)
    doc = navegador.open(f"{dominio}/admin/productos", timeout=180).read().decode()

    filas = []
    for bloque in doc.split("<tr")[1:]:
        marca = re.search(r'class="attr-(\d+)"', bloque)
        nombre = re.search(r"<td><strong>(.*?)</strong>", bloque)
        if not marca or not nombre:
            continue
        pid = int(marca.group(1))

        def seleccionado(campo: str) -> str | None:
            bloque_select = re.search(
                rf'<select name="{campo}" id="{campo}-\d+">(.*?)</select>', bloque, re.S)
            if not bloque_select:
                return None
            elegido = re.search(r'<option value="([^"]*)" selected>', bloque_select.group(1))
            return H.unescape(elegido.group(1)) if elegido and elegido.group(1) else None

        atributos = [H.unescape(valor) for valor, marcado
                     in re.findall(r'class="attr-\d+" value="([^"]+)"( checked)?', bloque)
                     if marcado]
        filas.append({
            "id": pid,
            "nombre_truncado": H.unescape(nombre.group(1)),
            "tipo": seleccionado("tipo"),
            "zona": seleccionado("zona"),
            "control": seleccionado("control"),
            "genero_uso": seleccionado("genero_uso"),
            "vibra": bool(re.search(rf'id="vibra-{pid}" checked', bloque)),
            "atributos": sorted(atributos),
        })
    return filas


# ── Unión ──

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--salida", default=str(Path(__file__).parent / "catalogo.json"))
    args = parser.parse_args()

    env = cargar_env()
    print("Descargando WooCommerce…")
    woo = [fila_desde_woocommerce(i) for i in descargar_woocommerce(env)]
    print(f"  {len(woo)} productos publicados")

    print("Leyendo facetas del panel admin…")
    facetas = descargar_facetas(env)
    print(f"  {len(facetas)} productos ofrecibles")

    # El panel solo lista los ofrecibles (con stock y con foto); el snapshot
    # tiene el mismo alcance, que es el conjunto que el bot puede recomendar.
    por_truncado: dict[str, dict] = {}
    for fila in woo:
        if fila["stock_status"] == "outofstock" or not fila["imagen_url"]:
            continue
        por_truncado.setdefault(fila["nombre"][:_TRUNCADO_PANEL], fila)

    catalogo = []
    sin_pareja = []
    for faceta in facetas:
        pareja = por_truncado.get(faceta["nombre_truncado"])
        if not pareja:
            sin_pareja.append(faceta["nombre_truncado"])
            continue
        fila = dict(pareja)
        fila["id"] = faceta["id"]
        for campo in ("tipo", "zona", "control", "genero_uso", "vibra", "atributos"):
            fila[campo] = faceta[campo]
        catalogo.append(fila)

    if sin_pareja:
        print(f"\n{len(sin_pareja)} productos del panel no casaron con WooCommerce:",
              file=sys.stderr)
        for nombre in sin_pareja[:10]:
            print(f"  - {nombre!r}", file=sys.stderr)
        print("El snapshot quedaría incompleto y la evaluación mediría un catálogo\n"
              "que no existe. Revisa si el sync está al día antes de seguir.",
              file=sys.stderr)
        return 1

    destino = Path(args.salida)
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(catalogo, ensure_ascii=False, indent=1))
    con_desc = sum(1 for f in catalogo if (f.get("descripcion") or "").strip())
    print(f"\nEscrito: {destino}")
    print(f"  {len(catalogo)} productos ofrecibles, {con_desc} con descripción")
    return 0


if __name__ == "__main__":
    sys.exit(main())
