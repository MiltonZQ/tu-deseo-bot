"""Recuperación de productos por restricciones, con relajación explícita.

Antes, cuando la intersección de la búsqueda quedaba vacía, los fallbacks
RELLENABAN con lo que hubiera cerca sin decirlo: por eso "vibrador anal"
devolvía enemas de limpieza. Ahora, si hay que soltar una restricción, se
reporta cuál, para que el mensaje al cliente lo diga.
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

for _m in ("asyncpg", "httpx", "openai", "qdrant_client", "redis", "redis.asyncio",
           "tiktoken", "PIL", "PIL.Image"):
    _mod = types.ModuleType(_m)
    _mod.__getattr__ = lambda _n: type("_Any", (), {"__init__": lambda *a, **k: None})  # type: ignore[attr-defined]
    sys.modules.setdefault(_m, _mod)

from app import catalog, db  # noqa: E402


# Catálogo mínimo con el caso del bug: enema, plug simple, plug vibrador.
CATALOGO = [
    dict(id=1, nombre="Enema para limpieza Anal Lito", tipo="enema", zona="anal",
         vibra=False, control="ninguno", genero_uso="unisex", atributos=[]),
    dict(id=2, nombre="Plug Anal Basix", tipo="plug", zona="anal",
         vibra=False, control="ninguno", genero_uso="unisex", atributos=["silicona"]),
    dict(id=3, nombre="Plug Lovense Hush 2", tipo="plug", zona="anal",
         vibra=True, control="app", genero_uso="unisex", atributos=[]),
    dict(id=4, nombre="Lovense Masajeador Prostático Edge 2", tipo="vibrador",
         zona="anal", vibra=True, control="app", genero_uso="hombre", atributos=[]),
    dict(id=5, nombre="Majestic Vibrador Hitachi Ursula", tipo="vibrador",
         zona="clitoris", vibra=True, control="manual", genero_uso="mujer", atributos=[]),
    dict(id=6, nombre="Anillo Vibrador Candil", tipo="anillo", zona="pene",
         vibra=True, control="manual", genero_uso="hombre", atributos=[]),
]
for _p in CATALOGO:
    _p.update(descripcion="", categoria="", precio=100000,
              imagen_url=f"http://x/{_p['id']}.jpg", galeria_urls=None, permalink=None)


class _Conn:
    """Aplica en Python el WHERE que el SQL aplicaría, leyendo los parámetros."""

    def __init__(self, filas):
        self.filas = filas
        self.ultimo_sql = ""

    async def fetch(self, sql, *params):
        self.ultimo_sql = " ".join(sql.split())
        excluidos = set()
        if "NOT (id = ANY(" in self.ultimo_sql:
            for p in params:
                if isinstance(p, list) and p and isinstance(p[0], int):
                    excluidos = set(p)
        out = []
        for f in self.filas:
            if f["id"] in excluidos:
                continue
            if "tipo = $" in self.ultimo_sql and f["tipo"] not in params:
                continue
            if "zona = $" in self.ultimo_sql and f["zona"] not in params:
                continue
            if "vibra = TRUE" in self.ultimo_sql and not f["vibra"]:
                continue
            if "control = $" in self.ultimo_sql and f["control"] not in params:
                continue
            if "genero_uso = $" in self.ultimo_sql and f["genero_uso"] not in params:
                continue
            out.append(dict(f))
        return out


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_e):
                return False
        return _Ctx()


def _buscar(restricciones, **kw):
    conn = _Conn(CATALOGO)
    orig, db._pool = getattr(db, "_pool", None), _Pool(conn)
    try:
        return asyncio.run(catalog.buscar_por_restricciones(restricciones, **kw))
    finally:
        db._pool = orig


# ── El caso del reporte ──

def test_vibrador_anal_no_devuelve_enemas():
    res = _buscar({"tipo": "vibrador", "zona": "anal"})
    nombres = [p["nombre"] for p in res.productos]
    assert res.productos, "debe encontrar el Edge 2"
    assert all(p["tipo"] != "enema" for p in res.productos), nombres
    assert all(p["zona"] == "anal" for p in res.productos), nombres
    assert "Enema para limpieza Anal Lito" not in nombres


def test_algo_que_vibre_en_anal_incluye_plugs_vibradores():
    res = _buscar({"zona": "anal", "vibra": True})
    nombres = [p["nombre"] for p in res.productos]
    assert "Plug Lovense Hush 2" in nombres
    assert "Plug Anal Basix" not in nombres, "ese no vibra"
    assert "Enema para limpieza Anal Lito" not in nombres


# ── Relajación explícita ──

def test_cuando_relaja_lo_reporta():
    """No hay succionadores anales: hay que soltar algo y decir cuál."""
    res = _buscar({"tipo": "succionador", "zona": "anal"})
    assert res.relajado, "debe informar qué restricción se soltó"


def test_no_relaja_si_hay_resultados_exactos():
    res = _buscar({"tipo": "vibrador", "zona": "anal"})
    assert res.relajado is None


def test_el_tipo_y_la_zona_no_se_relajan_en_silencio():
    """Si no hay nada del tipo pedido, se informa; nunca se cuela otro tipo."""
    res = _buscar({"tipo": "bomba", "zona": "pene"})
    if res.productos:
        assert res.relajado, "si devuelve algo de otro tipo, tiene que decirlo"


def test_el_control_se_relaja_antes_que_la_vibracion():
    """Pedir 'vibrador anal con app' cuando el control es lo menos importante."""
    res = _buscar({"tipo": "vibrador", "zona": "anal", "control": "remoto"})
    assert res.productos, "debe encontrar el Edge 2 soltando el control"
    assert res.relajado == "control", res.relajado
    assert all(p["zona"] == "anal" for p in res.productos)


def test_excluye_los_ya_mostrados():
    res = _buscar({"zona": "anal"}, exclude_ids=[1, 2, 3])
    assert all(p["id"] not in (1, 2, 3) for p in res.productos)


# ── Puerta de validación antes de enviar ──
# Última red: aunque fallen la clasificación, la búsqueda o el LLM, no puede
# salir un producto que contradiga lo que el cliente pidió.

def _main():
    import importlib
    if "fastapi" not in sys.modules:
        from tests.test_categoria_pegada_y_fotos import _main as cargar
        return cargar()
    return importlib.import_module("app.main")


def test_la_puerta_descarta_lo_que_no_cumple():
    m = _main()
    productos = [
        dict(id=1, nombre="Enema para limpieza Anal Lito", tipo="enema", zona="anal"),
        dict(id=4, nombre="Masajeador Prostático Edge 2", tipo="vibrador", zona="anal"),
    ]
    ok = m._validar_envio(productos, {"tipo": "vibrador", "zona": "anal"}, None)
    assert [p["id"] for p in ok] == [4], [p["nombre"] for p in ok]


def test_la_puerta_respeta_lo_que_se_relajo_a_proposito():
    """Si ya decidimos ceder en 'tipo' y avisar, no se descarta por eso."""
    m = _main()
    productos = [dict(id=3, nombre="Plug Lovense Hush 2", tipo="plug", zona="anal")]
    ok = m._validar_envio(productos, {"tipo": "vibrador", "zona": "anal"}, "tipo")
    assert len(ok) == 1


def test_la_puerta_no_estorba_cuando_no_hay_restricciones():
    m = _main()
    productos = [dict(id=9, nombre="Algo", tipo="dildo", zona="vaginal")]
    assert m._validar_envio(productos, {}, None) == productos


def test_el_texto_avisa_cuando_se_relajo():
    m = _main()
    prods = [{"id": 3, "nombre": "Plug Lovense Hush 2", "precio": 250000}]
    txt = m._texto_desde_candidatos(
        prods, {"intencion": "vibradores", "relajado": "tipo",
                "restricciones": {"tipo": "vibrador", "zona": "anal"}})
    assert "No tengo exactamente" in txt, txt


# ── Panel de productos ──

def test_el_panel_expone_la_vista_y_el_guardado():
    import ast as _ast
    src = (_ROOT / "app" / "admin.py").read_text()
    tree = _ast.parse(src)
    rutas = set()
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.AsyncFunctionDef, _ast.FunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec, _ast.Call) and dec.args:
                    a = dec.args[0]
                    if isinstance(a, _ast.Constant):
                        rutas.add(a.value)
    assert "/productos" in rutas, rutas
    assert "/productos/{producto_id}/facetas" in rutas, rutas
    assert '("productos", "Productos", "productos")' in src, "falta el enlace en el menú"


def test_una_edicion_manual_se_marca_como_revisada():
    """Sin esto, el siguiente sync pisaría la corrección del operador."""
    src = (_ROOT / "app" / "admin.py").read_text()
    i = src.index("async def guardar_facetas")
    bloque = src[i:i + 1400]
    assert 'origen="manual"' in bloque, bloque[:400]


def test_el_panel_y_el_bot_usan_el_mismo_criterio_de_ofrecible():
    """Si divergen, el panel muestra números que el bot no cumple.

    Pasó: el panel listaba los 483 productos del catálogo sin distinguir stock,
    así que parecía que el bot ofrecía productos agotados. La búsqueda sí los
    excluía, pero la vista mentía — el mismo tipo de desajuste entre lo que se
    dice y lo que se hace que causó los bugs de recomendación.
    """
    fuente_db = (_ROOT / "app" / "db.py").read_text()
    fuente_cat = (_ROOT / "app" / "catalog.py").read_text()
    assert "SQL_OFRECIBLE" in fuente_db, "el panel debe tener el criterio en un solo sitio"
    i = fuente_db.index("SQL_OFRECIBLE = ")
    criterio = fuente_db[i:i + 260]
    for pieza in ("stock_status", "outofstock", "imagen_url"):
        assert pieza in criterio, f"falta {pieza} en el criterio del panel"
    # El bot filtra por lo mismo en su consulta por restricciones.
    j = fuente_cat.index("async def _consultar_restricciones")
    bloque = fuente_cat[j:j + 900]
    for pieza in ("stock_status", "outofstock", "imagen_url"):
        assert pieza in bloque, f"la búsqueda del bot no filtra por {pieza}"


def test_el_panel_lista_solo_ofrecibles_por_defecto():
    fuente = (_ROOT / "app" / "admin.py").read_text()
    i = fuente.index("async def productos_page")
    bloque = fuente[i:i + 1500]
    assert "solo_ofrecibles = not todos" in bloque, (
        "por defecto el panel debe mostrar solo lo que el bot puede ofrecer")


# ── "Ver más" no puede traer productos que no cumplen ──
# Reportado: había 3 vibradores anales, se mostraron los 3, el bot igual ofreció
# "¿deseas ver más diseños?" y al pedirlos soltó la zona y mandó vibradores
# genéricos (Pigly, Pandora, Rocco) que no son anales.

def test_ver_mas_no_relaja_y_agota_limpio():
    """Si ya se mostró todo lo que cumple, no se rellena con otra cosa."""
    ya_vistos = [4]  # el único vibrador anal del catálogo de prueba
    res = _buscar({"tipo": "vibrador", "zona": "anal"},
                  exclude_ids=ya_vistos, permitir_relajar=False)
    assert res.productos == [], [p["nombre"] for p in res.productos]
    assert res.relajado is None, "no debe reportar relajación: simplemente se agotó"


def test_la_busqueda_normal_sigue_relajando():
    """La relajación sigue viva para una búsqueda nueva (no es un 'ver más')."""
    res = _buscar({"tipo": "succionador", "zona": "anal"}, permitir_relajar=True)
    assert res.relajado, "una consulta nueva sí puede ceder, avisando"


def test_contar_por_restricciones():
    """Saber cuántos hay es lo que decide si se ofrece 'ver más'."""
    conn = _Conn(CATALOGO)
    orig, db._pool = getattr(db, "_pool", None), _Pool(conn)
    try:
        n = asyncio.run(catalog.contar_por_restricciones({"tipo": "vibrador", "zona": "anal"}))
    finally:
        db._pool = orig
    assert n == 1, n


def test_con_pocas_opciones_no_se_ofrece_ver_mas():
    """Con 3 resultados en total, el CTA pregunta cuál quiere, no ofrece más."""
    m = _main()
    prods = [{"id": i, "nombre": f"Producto {i}", "precio": 50000} for i in (1, 2, 3)]
    txt = m._texto_desde_candidatos(prods, {"intencion": "vibradores", "hay_mas": False})
    assert "ver más diseños" not in txt.lower(), txt
    assert "cuál" in txt.lower(), txt


def test_con_mas_opciones_si_se_ofrece_ver_mas():
    m = _main()
    prods = [{"id": i, "nombre": f"Producto {i}", "precio": 50000} for i in range(1, 6)]
    txt = m._texto_desde_candidatos(prods, {"intencion": "vibradores", "hay_mas": True})
    assert "ver más diseños" in txt.lower(), txt


def test_el_aviso_de_relajacion_es_legible():
    """Salía 'No tengo exactamente anal para anal en este momento'."""
    m = _main()
    prods = [{"id": 1, "nombre": "Vibrador Pigly", "precio": 90900}]
    txt = m._texto_desde_candidatos(prods, {
        "intencion": "anal", "relajado": "zona", "hay_mas": False,
        "restricciones": {"tipo": "vibrador", "zona": "anal"}})
    assert "anal para anal" not in txt, txt
    assert "vibradores anales" in txt, txt
    # Un solo encabezado, no dos saludos encadenados.
    assert "¡Buena elección!" not in txt and "¡Perfecto!" not in txt, txt
