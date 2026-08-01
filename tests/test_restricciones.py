"""La intención del cliente es un conjunto de restricciones que se ACUMULA.

Bug que lo motivó (2026-08-01):

    cliente: tienen vibradores        → categoria_funcional = vibradores
    bot:     ¿para ella, para él, anal/próstata, o en pareja?
    cliente: anal                     → categoria_funcional = anal   ← se perdió "vibradores"
    bot:     [enemas de limpieza y plugs sin vibración]

El bot preguntaba por un ATRIBUTO del vibrador y el clasificador lo interpretaba
como una categoría nueva, tirando "vibradores" a la basura. Había una sola
variable donde hacían falta varias restricciones.

Regla general que sustituye a los parches por palabra:
  - una palabra que aporta una faceta NUEVA (zona, atributo, control) REFINA
  - una palabra que aporta un TIPO distinto REEMPLAZA (cambio de tema)
"""
from __future__ import annotations

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

from app import facetas  # noqa: E402

leer = facetas.interpretar_mensaje
fusionar = facetas.fusionar_restricciones


# ── El caso del reporte ──

def test_anal_refina_vibradores_no_lo_reemplaza():
    r1 = fusionar({}, leer("tienen vibradores"))
    assert r1 == {"tipo": "vibrador"}
    r2 = fusionar(r1, leer("anal"))
    assert r2.get("tipo") == "vibrador", "no puede perderse el vibrador"
    assert r2.get("zona") == "anal"


def test_pene_refina_vibradores():
    r1 = fusionar({}, leer("tienen vibradores"))
    r2 = fusionar(r1, leer("pene"))
    assert r2.get("tipo") == "vibrador" and r2.get("zona") == "pene"


def test_con_app_refina_sin_perder_nada():
    r = fusionar({"tipo": "vibrador", "zona": "anal"}, leer("con app"))
    assert r == {"tipo": "vibrador", "zona": "anal", "control": "app"}


# ── Cambio de tema: un tipo distinto SÍ reemplaza ──

def test_un_tipo_distinto_reemplaza_todo():
    previas = {"tipo": "vibrador", "zona": "anal", "control": "app"}
    r = fusionar(previas, leer("ahora quiero lubricantes"))
    assert r.get("tipo") == "lubricante"
    assert "zona" not in r or r["zona"] != "anal", "no debe arrastrar la zona anterior"
    assert "control" not in r, "no debe arrastrar el control anterior"


def test_latigos_cambian_de_tema_desde_bombas():
    r = fusionar({"tipo": "bomba", "zona": "pene"}, leer("me gustaria ver si tinen latigos"))
    assert r.get("tipo") == "bondage"


def test_respuesta_afirmativa_no_cambia_nada():
    previas = {"tipo": "succionador", "zona": "clitoris"}
    for msg in ("si", "ok dale", "muéstrame", "claro"):
        assert fusionar(previas, leer(msg)) == previas, msg


# ── Atributos que implican un tipo ──

def test_sabores_implica_lubricante_si_el_tipo_no_lo_admite():
    """Venía de bombas: 'sabores' no es un atributo de una bomba de vacío."""
    r = fusionar({"tipo": "bomba", "zona": "pene"}, leer("sabores"))
    assert r.get("tipo") == "lubricante"
    assert "sabor" in r.get("atributos", [])


def test_sabores_solo_refina_si_ya_hablabamos_de_lubricantes():
    r = fusionar({"tipo": "lubricante"}, leer("sabores"))
    assert r.get("tipo") == "lubricante"
    assert "sabor" in r.get("atributos", [])


def test_anal_dentro_de_lubricantes_es_un_filtro():
    """Regresión: 'anal' tras hablar de lubricantes es 'lubricante anal'."""
    r = fusionar({"tipo": "lubricante"}, leer("anal"))
    assert r.get("tipo") == "lubricante", "no debe convertirse en juguete anal"
    assert r.get("zona") == "anal"


# ── Vocabulario ──

def test_reconoce_el_vocabulario_del_cliente():
    casos = [
        ("quiero un succionador", "tipo", "succionador"),
        ("tienen bombas para el pene", "tipo", "bomba"),
        ("busco un consolador", "tipo", "dildo"),
        ("tienen esposas", "tipo", "bondage"),
        ("un plug", "tipo", "plug"),
        ("algo para el clitoris", "zona", "clitoris"),
        ("con control remoto", "control", "remoto"),
        ("en pareja", "genero_uso", "pareja"),
    ]
    for texto, campo, esperado in casos:
        r = leer(texto)
        assert r.get(campo) == esperado, f"{texto!r} → {r}"


def test_no_coincide_dentro_de_otras_palabras():
    """Mismo blindaje que en la clasificación de productos."""
    assert leer("busco algo profundo").get("tipo") != "funda"
    assert leer("para doble penetracion").get("zona") != "pene"


def test_mensajes_sin_producto_no_aportan_restricciones():
    for msg in ("hola buenas tardes", "cuanto cuesta el envio", "ya hice el pago"):
        assert leer(msg) == {}, msg


# ── Integración con el clasificador que usa el pipeline ──

def test_el_clasificador_devuelve_restricciones_acumuladas():
    import asyncio
    from app import catalog, openai_client

    async def _sin_llm(_t):
        return None
    openai_client.clasificar_intencion_llm = _sin_llm

    async def _run():
        r1 = await catalog.clasificar_intencion_cliente("tienen vibradores", [], None, None)
        assert r1["restricciones"] == {"tipo": "vibrador"}, r1["restricciones"]
        r2 = await catalog.clasificar_intencion_cliente(
            "anal", [], r1["categoria_funcional"], r1["restricciones"])
        # El caso exacto del reporte: no puede perderse "vibrador".
        assert r2["restricciones"].get("tipo") == "vibrador", r2["restricciones"]
        assert r2["restricciones"].get("zona") == "anal", r2["restricciones"]
    asyncio.run(_run())


# ── Coherencia entre lo que el bot OFRECE y lo que sabe filtrar ──
# El bug del reporte empezó aquí: el bot ofrecía "anal/próstata" como opción para
# vibradores, pero el buscador no sabía cumplir esa combinación. Una opción que
# se ofrece y no se entiende es una promesa que el sistema no puede sostener.

RESPUESTAS_A_LAS_PREGUNTAS = [
    # (lo que el bot ofrece, lo que responde el cliente, faceta que debe salir)
    ("vibradores", "para ella", "genero_uso"),
    ("vibradores", "clitoris", "zona"),
    ("vibradores", "punto g", "zona"),
    ("vibradores", "pene", "zona"),
    ("vibradores", "anillos", "tipo"),
    ("vibradores", "anal", "zona"),
    ("vibradores", "prostata", "zona"),
    ("vibradores", "en pareja", "genero_uso"),
    ("dildos", "realista", "atributos"),
    ("dildos", "con ventosa", "atributos"),
    ("dildos", "vidrio", "atributos"),
    ("dildos", "doble", "atributos"),
    ("lubricantes", "base de agua", "atributos"),
    ("lubricantes", "silicona", "atributos"),
    ("lubricantes", "desensibilizante", "atributos"),
    ("lubricantes", "sabores", "atributos"),
    ("anal", "primera vez", "atributos"),
    ("anal", "prostata", "zona"),
    ("anal", "con control remoto", "control"),
    ("lenceria", "body", "tipo"),
    ("lenceria", "suspensorio", "tipo"),
    ("succionadores", "con app", "control"),
    ("bondage", "esposas", "tipo"),
    ("bondage", "antifaz", "tipo"),
    ("bondage", "fustas", "tipo"),
]


def test_toda_opcion_ofrecida_se_entiende():
    fallos = []
    for menu, respuesta, campo in RESPUESTAS_A_LAS_PREGUNTAS:
        r = leer(respuesta)
        if campo not in r:
            fallos.append(f"menú {menu}: el cliente responde {respuesta!r} y no se "
                          f"reconoce {campo} (se obtuvo {r})")
    assert not fallos, "\n  " + "\n  ".join(fallos)


# ── Vocabulario único ──
# El vocabulario del cliente quedó duplicado: catalog.py (clasificador viejo) y
# facetas.py (el que manda en la búsqueda). Divergieron, y el nuevo era el más
# pobre. Reportado: "Manejan suspensores de hombre" devolvía conjuntos de mujer
# porque ni "suspensores" ni "de hombre" estaban en el vocabulario nuevo.

FRASES_REALES = [
    # (mensaje del cliente, campo, valor esperado)
    ("Manejan suspensores de hombre", "tipo", "lenceria"),
    ("Manejan suspensores de hombre", "genero_uso", "hombre"),
    ("Quiero suspensores de hombre", "genero_uso", "hombre"),
    ("quiero un suspensor", "tipo", "lenceria"),
    ("tienen suspensorios", "tipo", "lenceria"),
    ("algo de hombre", "genero_uso", "hombre"),
    ("algo para hombre", "genero_uso", "hombre"),
    ("es para un hombre", "genero_uso", "hombre"),
    ("lenceria de mujer", "genero_uso", "mujer"),
    ("algo femenino", "genero_uso", "mujer"),
    ("para dama", "genero_uso", "mujer"),
    ("para caballero", "genero_uso", "hombre"),
    ("juguetes masculinos", "genero_uso", "hombre"),
]


def test_las_frases_reales_de_los_clientes_se_entienden():
    fallos = []
    for texto, campo, esperado in FRASES_REALES:
        r = leer(texto)
        if r.get(campo) != esperado:
            fallos.append(f"{texto!r} → {campo}={r.get(campo)!r} (esperado {esperado!r})")
    assert not fallos, "\n  " + "\n  ".join(fallos)


def test_suspensores_de_hombre_no_devuelve_lenceria_de_mujer():
    """El caso exacto del reporte, sobre el estado que había."""
    previas = {"tipo": "lenceria"}
    r = fusionar(previas, leer("Manejan suspensores de hombre"))
    assert r.get("tipo") == "lenceria"
    assert r.get("genero_uso") == "hombre", r


# Marcas: no se mapean a facetas a propósito. Satisfyer fabrica succionadores de
# clítoris y también masturbadores masculinos, así que "satisfyer" no implica un
# género. Los nombres de marca los resuelve la búsqueda por nombre
# (`catalog.buscar_producto_especifico`), no la clasificación por facetas.
_MARCAS_NO_SON_FACETAS = {"satisfyer", "lovense diamo", "we vibe", "chorus"}


def test_el_vocabulario_del_clasificador_viejo_esta_cubierto():
    """Ninguna palabra que el clasificador viejo entendía puede perderse.

    Los dos vocabularios convivieron y divergieron: el viejo conocía
    "suspensores" y "de hombre", el nuevo no, y como el nuevo manda en la
    búsqueda, un cliente que pedía suspensorios masculinos recibía conjuntos de
    mujer. Este test falla si vuelven a separarse.
    """
    from app import catalog
    faltan = []
    for claves, genero in catalog._GENERO_KEYWORDS_CLIENTE:
        for clave in claves:
            if len(clave) < 4 or clave in _MARCAS_NO_SON_FACETAS:
                continue
            r = leer(f"quiero algo {clave}")
            if not r:
                faltan.append(f"{clave!r} (→ {genero})")
    assert not faltan, ("el vocabulario nuevo no entiende palabras que el viejo sí:\n  "
                        + "\n  ".join(faltan[:25]))
