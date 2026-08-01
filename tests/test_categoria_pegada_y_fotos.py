"""Regresiones de los bugs reportados el 2026-08-01 (conversación "Prueba 2").

Cuatro fallos distintos, con tres causas raíz:

  1. "bombas para el pene" solo mostraba el Hefesto, y "succionador" nunca mostraba
     el Nyla. Causa: `_categoria_normalizada` mezclaba nombre + descripción +
     categoría de origen en un solo texto, y una palabra de la DESCRIPCIÓN
     disparaba una regla anterior (más genérica) que se llevaba el producto.

  2. El cliente preguntaba por látigos, por lubricantes y por sabores, y seguía
     recibiendo bombas para el pene del primer turno. Causa: el motor de memoria
     solo aceptaba "cambio de tema" si el mensaje contenía uno de 15 sustantivos
     fijos, lista que omitía lubricante, látigo, plug, arnés, kit, masturbador…

  3. Al pedir látigos (que no hay), llegaban dos anillos vibradores al azar.
     Dos causas sumadas: el Intento E-bis relajaba la categoría ignorando el
     subtipo pedido, y `_handle_message` forzaba las fotos de los candidatos aunque
     la respuesta del LLM no ofreciera ningún producto (mensaje de escalado).

A diferencia del resto de la suite, estos tests EJECUTAN el código real: stubean
las dependencias de runtime que no están instaladas y, para las consultas, un
pool de DB falso con un catálogo mínimo.
"""
from __future__ import annotations

import ast
import asyncio
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Dependencias de runtime que no hacen falta para la lógica bajo prueba.
# setdefault: si están instaladas de verdad (Docker), se usan las reales.
for _m in ("asyncpg", "httpx", "openai", "qdrant_client", "redis", "redis.asyncio",
           "tiktoken", "PIL", "PIL.Image"):
    _mod = types.ModuleType(_m)
    _mod.__getattr__ = lambda _n: type("_Any", (), {"__init__": lambda *a, **k: None})  # type: ignore[attr-defined]
    sys.modules.setdefault(_m, _mod)

from app import catalog, db, openai_client  # noqa: E402


async def _sin_llm(_texto):
    """El respaldo LLM no debe influir en estos tests (y no hay red)."""
    return None


openai_client.clasificar_intencion_llm = _sin_llm


def _clasificar(user_text, history=None, estado=None):
    return asyncio.run(
        catalog.clasificar_intencion_cliente(user_text, history or [], estado))


# ── Bug 1: la descripción secuestraba la categoría ──

_BENDER = (
    "Bomba Automática para Pene Recargable Bender Optimus Pro",
    "Bomba automática para pene recargable Bender Optimus Pro CamToyz con sistema "
    "eléctrico de succión y válvula de liberación de aire para mayor seguridad.",
    "Bombas para el Pene",
)
_NYLA = (
    "Succionador Con Ondas Y Vibracion Nyla Fuscia",
    "Succionador de clítoris Nyla con tecnología de ondas de presión para "
    "estimulación precisa e intensa. Compatible con lubricantes a base de agua.",
    "Juguetes",
)
_HEFESTO = (
    "Bomba Para El Pene Hefesto",
    "Bomba para el pene Hefesto con sistema de vacío diseñada para mejorar la firmeza.",
    "Juguetes",
)


def test_bomba_bender_no_la_secuestra_la_palabra_succion_de_su_descripcion():
    # "sistema eléctrico de succión" hacía que cayera en succionadores.
    assert catalog._categoria_normalizada(*_BENDER) == "bombas-pene"


def test_succionador_nyla_no_lo_secuestra_la_palabra_lubricantes_de_su_descripcion():
    # "Compatible con lubricantes a base de agua" lo mandaba a lubricantes-y-cuidado.
    assert catalog._categoria_normalizada(*_NYLA) == "succionadores"


def test_las_dos_bombas_del_catalogo_quedan_en_la_misma_categoria():
    assert (catalog._categoria_normalizada(*_BENDER)
            == catalog._categoria_normalizada(*_HEFESTO) == "bombas-pene")


def test_la_descripcion_sigue_clasificando_cuando_el_nombre_no_dice_nada():
    # El nombre no da señal: hay que seguir mirando descripción y categoría origen.
    assert catalog._categoria_normalizada(
        "Kit Fiore", "Amarres, mordaza y látigo para bondage", "Fetish") == "pareja-y-bondage"
    assert catalog._categoria_normalizada("", "", "") == "juegos-y-accesorios"


# ── Bug 2: la categoría del primer turno se quedaba pegada ──

def test_latigos_no_heredan_la_categoria_bombas_del_turno_anterior():
    r = _clasificar("me gustaria ver si tinen latigos", estado="bombas-pene")
    assert r["categoria_funcional"] == "pareja-y-bondage"
    assert r["subtipo_detectado"] == "latigo"


def test_lubricantes_no_heredan_la_categoria_bombas_del_turno_anterior():
    r = _clasificar("puedo ver que lubricantes manejan", estado="bombas-pene")
    assert r["categoria_funcional"] == "lubricantes-y-cuidado"


def test_sabores_deriva_lubricantes_aunque_la_memoria_diga_bombas():
    r = _clasificar("sabores", estado="bombas-pene")
    assert r["categoria_funcional"] == "lubricantes-y-cuidado"
    assert r["subtipo_detectado"] == "sabores"


def test_anal_sigue_siendo_un_filtro_dentro_de_lubricantes():
    # Regresión del fix anterior: "anal" tras hablar de lubricantes NO es cambio
    # de tema, es "lubricante anal".
    r = _clasificar("anal", estado="lubricantes-y-cuidado")
    assert r["categoria_funcional"] == "lubricantes-y-cuidado"
    assert r["subtipo_detectado"] == "desensibiliz"


def test_respuesta_afirmativa_no_cambia_de_tema():
    for msg in ("si", "ok dale", "los rojos", "mas diseños"):
        r = _clasificar(msg, estado="succionadores")
        assert r["categoria_funcional"] == "succionadores", msg


def test_conversacion_reportada_completa():
    """Los 5 turnos exactos de la conversación del reporte."""
    esperado = [
        ("tienen bombas para el pene", "bombas-pene"),
        ("me gustaria ver si tinen latigos", "pareja-y-bondage"),
        ("puedo ver que lubricantes manejan", "lubricantes-y-cuidado"),
        ("sabores", "lubricantes-y-cuidado"),
    ]
    estado = None
    history: list[dict] = []
    for msg, cat in esperado:
        r = _clasificar(msg, history, estado)
        assert r["categoria_funcional"] == cat, f"{msg!r} → {r['categoria_funcional']}"
        history += [{"role": "user", "content": msg}, {"role": "assistant", "content": "..."}]
        estado = r["categoria_funcional"] or estado


# ── Bug 3a: E-bis relajaba la categoría ignorando el subtipo pedido ──

class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, *_a, **_k):
        return list(self._rows)

    async def fetchrow(self, *_a, **_k):
        return self._rows[0] if self._rows else None


class _FakePool:
    def __init__(self, rows):
        self._rows = rows

    def acquire(self):
        rows = self._rows

        class _Ctx:
            async def __aenter__(self):
                return _FakeConn(rows)

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


_CATALOGO_FAKE = [
    {"id": 1, "nombre": "Bomba Para El Pene Hefesto", "descripcion": "Bomba de vacío",
     "categoria": "Juguetes", "precio": 180000, "imagen_url": "http://x/1.jpg",
     "galeria_urls": None, "permalink": None},
    {"id": 2, "nombre": "Anillo Vibrador Candil para el Pene", "descripcion": "Anillo vibrador",
     "categoria": "Juguetes", "precio": 25000, "imagen_url": "http://x/2.jpg",
     "galeria_urls": None, "permalink": None},
    {"id": 3, "nombre": "Anillos para Pene Donut Stay Hard Kit x3", "descripcion": "Anillos",
     "categoria": "Juguetes", "precio": 14900, "imagen_url": "http://x/3.jpg",
     "galeria_urls": None, "permalink": None},
]


def _recomendar(**kwargs):
    original, db._pool = getattr(db, "_pool", None), _FakePool(_CATALOGO_FAKE)
    qdrant, catalog.config.QDRANT_ENABLED = catalog.config.QDRANT_ENABLED, False
    try:
        return asyncio.run(catalog.get_productos_para_recomendar(**kwargs))
    finally:
        db._pool = original
        catalog.config.QDRANT_ENABLED = qdrant


def test_subtipo_concreto_sin_stock_no_devuelve_productos_de_otra_categoria():
    # Pedir látigos no puede terminar en anillos vibradores.
    res = _recomendar(categoria_funcional="pareja-y-bondage", genero="hombre",
                      user_text="me gustaria ver si tinen latigos", subtipo="latigo")
    assert res == [], [p["nombre"] for p in res]


def test_sin_subtipo_e_bis_sigue_relajando_la_categoria_por_genero():
    # Regresión del fix anterior: "vibradores" + "hombre" no existe como
    # intersección, y E-bis debe traer anillos/fundas de hombre.
    res = _recomendar(categoria_funcional="vibradores", genero="hombre",
                      user_text="vibradores para el pene")
    assert res, "E-bis debe seguir funcionando cuando no hay subtipo concreto"
    assert all("anillo" in p["nombre"].lower() for p in res)


# ── Bug 3b: se forzaban fotos sobre respuestas que no ofrecían productos ──

def test_las_fotos_solo_salen_con_el_texto_que_las_describe():
    """Antes se adjuntaban fotos a respuestas que no ofrecían productos.

    El cliente preguntaba por látigos, recibía el mensaje de escalado y detrás
    dos anillos vibradores al azar. Se resolvía con una bandera que detectaba si
    el LLM había prometido productos; ahora la garantía es estructural: en los
    turnos de producto el TEXTO lo arma el sistema desde los mismos candidatos
    que las fotos, y solo entonces se envían fotos.
    """
    src = (_ROOT / "app" / "main.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_message":
            fn_src = ast.get_source_segment(src, node)
            i = fn_src.index("texto_lo_arma_el_sistema = ")
            assert "info.get(\"debe_mostrar\") and candidatos" in fn_src[i:i + 160], fn_src[i:i + 160]
            # "\n    if" para no encontrar antes el "elif" que redacta el texto.
            j = fn_src.index("\n    if texto_lo_arma_el_sistema:")
            bloque = fn_src[j:j + 700]
            assert "final_productos = candidatos[:5]" in bloque, bloque[:300]
            assert "_resolver_candidatos_del_llm" in bloque, (
                "en los turnos que redacta el LLM hay que seguir validando sus IDs")
            return
    raise AssertionError("No se encontró _handle_message")


def test_un_fallo_de_envio_no_cancela_las_fotos_restantes():
    """El try/except debe estar DENTRO del bucle de envío."""
    src = (_ROOT / "app" / "main.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_enviar_fotos_productos":
            for child in node.body:
                # Ningún try puede envolver el for: el for debe estar en el cuerpo.
                assert not isinstance(child, ast.Try), (
                    "el try no puede envolver el bucle entero: un fallo cancelaría "
                    "todas las fotos siguientes")
            fors = [n for n in ast.walk(node) if isinstance(n, ast.For)]
            assert fors and any(
                isinstance(c, ast.Try) for f in fors for c in ast.walk(f)), (
                "cada envío debe tener su propio try/except")
            return
    raise AssertionError("No se encontró _enviar_fotos_productos")


# ── Numeración continua entre rondas ──
# Al pedir "ver más", la segunda tanda numeraba otra vez desde 1️⃣, así que había
# dos productos distintos con el mismo número. Como al final se le pide al cliente
# "indícame los números", eso hacía ambiguo el pedido.

def _main():
    """app.main con fastapi/starlette stubeados (ver tests/stubs.py)."""
    from tests.stubs import importar_main
    return importar_main()


def test_numeracion_continua_en_la_segunda_ronda():
    m = _main()
    prods = [{"id": i, "nombre": f"Producto {i}", "precio": 10000 * i} for i in (1, 2, 3)]
    txt = m._texto_desde_candidatos(prods, {"intencion": "succionadores"}, offset=5)
    assert "6️⃣" in txt and "7️⃣" in txt and "8️⃣" in txt, txt
    assert "1️⃣" not in txt.replace("1️⃣0️⃣", ""), f"no debe reiniciar en 1: {txt}"


def test_numeracion_primera_ronda_arranca_en_uno():
    m = _main()
    prods = [{"id": 1, "nombre": "Producto", "precio": 1000}]
    assert "1️⃣" in m._texto_desde_candidatos(prods, {"intencion": "dildos"})


def test_keycaps_mas_alla_de_diez():
    m = _main()
    assert m._numero_lista(1) == "1️⃣"
    assert m._numero_lista(9) == "9️⃣"
    assert m._numero_lista(10) == "🔟"
    assert m._numero_lista(12) == "1️⃣2️⃣"
    # El detector de selección numérica busca el carácter de keycap: debe seguir ahí.
    assert "️⃣" in m._numero_lista(15)


# ── Bug 4: las claves hacían match DENTRO de otras palabras ──
# Reportado el 2026-08-01: el cliente pedía vibradores "para el pene" y recibía
# "Majestic Vibrador tipo Hitachi Ursula" y "Vibrador Doble Tifany". Ninguno es de
# hombre: sus descripciones dicen "estimulación profunda" / "sensaciones profundas",
# y la clave "funda" (de fundas-pene → género hombre) hacía match dentro de
# "proFUNDA". La comparación era `clave in haystack`, sin límites de palabra.

def test_profunda_no_es_una_funda_para_el_pene():
    for palabra in ("estimulacion profunda", "sensaciones profundas", "vibra profundamente"):
        assert catalog._categoria_normalizada("Vibrador Ursula", palabra, "Vibradores") != "fundas-pene", palabra
        assert catalog._genero_normalizado("Vibrador Ursula", palabra, "Vibradores") != "hombre", palabra


def test_penetracion_no_convierte_el_producto_en_masculino():
    # "pene" arranca palabra en "penetración", por eso exige palabra completa.
    assert catalog._genero_normalizado(
        "Arnes Strap-On", "Para doble penetracion en pareja", "Arneses") != "hombre"
    # pero "pene" como palabra real sí debe seguir marcando hombre
    assert catalog._genero_normalizado("Funda para el Pene Cobra", "", "") == "hombre"


def test_otras_colisiones_reales_del_catalogo():
    casos = [
        ("Dildo con Testiculos", "musculos y testiculos realistas", "anal"),   # 'culo'
        ("Vibrador", "recorre el canal del placer", "anal"),                    # 'anal'
        ("Antifaz", "un diseño pensado y rosado", "pareja-y-bondage"),          # 'sado'
        ("Lubricante", "elaborado con cuidado", "juegos-y-accesorios"),         # 'dado'
        ("Gel", "revela tus sentidos", "lubricantes-y-cuidado"),                # 'vela '
    ]
    for nombre, desc, categoria_incorrecta in casos:
        cat = catalog._categoria_normalizada(nombre, desc, "")
        gen = catalog._genero_normalizado(nombre, desc, "")
        assert not (cat == categoria_incorrecta and categoria_incorrecta == "anal" and "canal" in desc), desc
        assert gen != "anal" or "anal" in nombre.lower(), f"{desc} → genero {gen}"


def test_las_raices_deliberadas_siguen_funcionando():
    """El fix no puede romper los prefijos que sí son intencionales."""
    assert catalog._categoria_normalizada("Gel", "un lubricante intimo", "") == "lubricantes-y-cuidado"
    assert catalog._categoria_normalizada("Estimulador", "para la prostata", "") == "anal"
    assert catalog._categoria_normalizada("Satisfyer", "succionador de clitoris", "") == "succionadores"
    assert catalog._categoria_normalizada("Set", "bolas anales de silicona", "") == "anal"
    assert catalog._categoria_normalizada("Juguete", "vibradores recargables", "") == "vibradores"
    assert catalog._genero_normalizado("Kit", "estimulacion de la prostata", "") == "hombre"


def test_los_dos_vibradores_del_reporte_ya_no_son_de_hombre():
    ursula = ("Majestic Vibrador tipo Hitachi Ursula",
              "Descubre un mundo de satisfaccion con nuestro vibrador Ursula, en un "
              "llamativo tono rosa, obra maestra de la estimulacion profunda.", "Vibradores")
    tifany = ("Vibrador Doble Tifany Camtoyz",
              "Sistema de doble estimulacion pulsante. Sus modos de vibracion replican "
              "sensaciones profundas que estimulan multiples zonas.", "Vibradores")
    for prod in (ursula, tifany):
        assert catalog._categoria_normalizada(*prod) == "vibradores", prod[0]
        assert catalog._genero_normalizado(*prod) != "hombre", prod[0]
