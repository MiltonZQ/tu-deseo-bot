"""Cuánta cobertura real tiene cada subtipo en el catálogo. SOLO LECTURA.

Antes de hacer estricto el filtro por subtipo hay que saber cuáles se quedarían
en cero, porque en modo estricto un cero no muestra menos productos: pausa el
bot y abre un ticket. El CSV de `prompts/knowledge/` NO sirve para medir esto:
es un archivo de prompts congelado, sin descripciones y sin productos que el bot
sí muestra en producción (ventosa, playboy, sailor moon), y da 27 subtipos en
cero que en el catálogo real sí existen.

Dos fuentes válidas:

  --db    la tabla `productos` (por defecto). Solo funciona donde el host de
          DATABASE_URL resuelva: dentro del contenedor en Coolify, o con túnel.
  --woo   la API de WooCommerce, de donde se sincroniza esa tabla. Da los
          mismos nombres y descripciones —que es lo que mira el filtro— y las
          facetas se recalculan con `facetas.clasificar_por_reglas`, la misma
          función del sync. Es la que se puede correr desde fuera del servidor.

Uso:
    .venv/bin/python scripts/auditar_subtipos.py [--db|--woo]

Sale con código 1 si algún subtipo se queda sin cobertura y sin declarar en
`_SUBTIPOS_SIN_COBERTURA`: en modo estricto eso pausaría el bot.
"""
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from app import catalog, db, facetas  # noqa: E402

SQL = """
SELECT nombre, descripcion, tipo, zona, atributos
FROM productos
WHERE (stock_status IS NULL OR stock_status <> 'outofstock')
  AND imagen_url IS NOT NULL AND imagen_url != ''
"""


def _cumple_facetas(producto: dict, restricciones: dict) -> bool:
    if not restricciones:
        return False
    if restricciones.get("tipo") and producto.get("tipo") != restricciones["tipo"]:
        return False
    if restricciones.get("zona") and producto.get("zona") != restricciones["zona"]:
        return False
    atributos = producto.get("atributos") or []
    return all(a in atributos for a in (restricciones.get("atributos") or []))


async def _filas_desde_db() -> list[dict]:
    try:
        await db.init_pool()
    except Exception as e:
        # `DATABASE_URL` apunta al host `db`, el nombre del servicio dentro de
        # la red de Docker/Coolify: desde un portátil no resuelve.
        print(f"No se pudo conectar a la DB ({type(e).__name__}: {e}).\n"
              "Corre esto donde el host de DATABASE_URL resuelva —dentro del\n"
              "contenedor en Coolify, o con túnel—, o usa --woo.",
              file=sys.stderr)
        raise SystemExit(1)
    async with db._pool.acquire() as conn:  # type: ignore[attr-defined]
        return [dict(r) for r in await conn.fetch(SQL)]


async def _filas_desde_woocommerce() -> list[dict]:
    """Mismo catálogo, visto desde la fuente que alimenta la tabla.

    Se replica aquí el criterio de "ofrecible" del SQL (con stock y con imagen)
    y se recalculan las facetas con la misma función que usa el sync, para que
    las dos fuentes sean comparables.
    """
    from app import woocommerce

    filas = []
    for p in await woocommerce.fetch_all_products():
        if (p.get("stock_status") or "") == "outofstock":
            continue
        imagenes = p.get("images") or []
        if not imagenes or not (imagenes[0].get("src") or ""):
            continue
        nombre = p.get("name") or ""
        desc = woocommerce._clean_html(
            p.get("short_description") or p.get("description") or "")
        cats = ", ".join(c.get("name", "") for c in (p.get("categories") or []))
        f = facetas.clasificar_por_reglas(nombre, desc, cats)
        filas.append({"nombre": nombre, "descripcion": desc, "tipo": f.tipo,
                      "zona": f.zona, "atributos": list(f.atributos)})
    return filas


async def main() -> None:
    usar_woo = "--woo" in sys.argv
    fuente = "WooCommerce (`fetch_all_products`)" if usar_woo else "tabla `productos`"
    filas = await (_filas_desde_woocommerce() if usar_woo else _filas_desde_db())
    print(f"Fuente: {fuente}")
    con_desc = sum(1 for f in filas if (f.get("descripcion") or "").strip())
    pct = 100 * con_desc // max(len(filas), 1)
    print(f"Productos ofrecibles: {len(filas)}")
    print(f"Con descripción no vacía: {con_desc} ({pct}%)\n")

    lineas = ["| subtipo | por nombre | por faceta | soft | veredicto |",
              "|---|---|---|---|---|"]
    sin_cobertura = []
    for s in catalog._SUBTIPO_KEYWORDS:
        por_nombre = len(catalog._filtrar_por_subtipo(filas, s))
        # Qué entiende el sistema de facetas si el cliente escribe solo esto.
        interpretado = facetas.interpretar_mensaje(s) or {}
        por_faceta = sum(1 for f in filas if _cumple_facetas(f, interpretado))
        soft = catalog._es_subtipo_soft(s)
        # "FACETA CUBRE" solo vale cuando el filtro por nombre NO llega a
        # correr, y eso pasa únicamente con `atributos`: es la condición de
        # `_subtipo_ya_en_restricciones`. Con tipo o zona el filtro sí corre, y
        # ahí un 0 por nombre escala igual aunque la faceta tenga productos —
        # que es justo el falso negativo que hay que cazar aquí.
        cubre_faceta = bool(interpretado.get("atributos")) and por_faceta > 0
        if por_nombre or soft:
            veredicto = "ok"
        elif cubre_faceta:
            veredicto = "FACETA CUBRE"
        else:
            veredicto = "SIN COBERTURA"
            sin_cobertura.append(s)
        lineas.append(
            f"| `{s}` | {por_nombre} | {por_faceta} | {soft} | {veredicto} |")

    informe = "\n".join(lineas)
    print(informe)
    print(f"\nSIN COBERTURA (escalarían en modo estricto): {len(sin_cobertura)}")
    for s in sin_cobertura:
        print(f"  - {s!r}")

    destino = _ROOT / "docs" / "auditoria-subtipos.md"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(
        "# Auditoría de cobertura por subtipo\n\n"
        f"Generado por `scripts/auditar_subtipos.py`. Fuente: {fuente}.\n\n"
        "`por nombre` es lo que ve `_filtrar_por_subtipo`, que es quien decide "
        "en modo estricto.\n`por faceta` solo evita ese filtro cuando la faceta "
        "es un `atributo` (ver `_subtipo_ya_en_restricciones`).\n\n"
        f"Productos ofrecibles: {len(filas)} — con descripción no vacía: "
        f"{con_desc} ({pct}%)\n\n"
        + informe
        + "\n\n## Sin cobertura\n\n"
        + ("\n".join(f"- `{s}`" for s in sin_cobertura) or "_ninguno_")
        + "\n")
    print(f"\nEscrito: {destino}")

    # Gate del modo estricto. Un subtipo sin cobertura que nadie declaró no
    # muestra menos productos: pausa el bot y abre un ticket. Que falle aquí
    # —antes de desplegar— es justo el punto. La comprobación no puede vivir en
    # la suite porque necesita el catálogo, y `tests/run.py` corre sin DB.
    no_declarados = sorted(set(sin_cobertura) - set(catalog._SUBTIPOS_SIN_COBERTURA))
    if no_declarados:
        print("\n⚠️  Subtipos sin cobertura y sin declarar: "
              f"{no_declarados}\n"
              "Añade sinónimos en `_SUBTIPO_SINONIMOS` si el producto existe "
              "con otro nombre,\no decláralos en `_SUBTIPOS_SIN_COBERTURA` si "
              "de verdad no los vendemos.", file=sys.stderr)
        raise SystemExit(1)
    print("Cobertura OK: todo subtipo sin producto está declarado.")


if __name__ == "__main__":
    asyncio.run(main())
