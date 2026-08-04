"""El filtro por subtipo no debe pisar lo que las facetas ya resolvieron.

"dildo con ventosa" llega como restricciones={"tipo":"dildo",
"atributos":["ventosa"]}: el SQL ya devolvió el conjunto correcto. Volver a
exigir la palabra "ventosa" en el NOMBRE deja fuera productos bien etiquetados
("Dildo Realista Baru 21 cm" lo está) y, con el filtro estricto, eso no muestra
menos productos: pausa el bot y abre un ticket por una venta que sí teníamos.
"""
import asyncio
import sys
from pathlib import Path
from types import ModuleType

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

for _mod in ("asyncpg", "httpx", "openai", "qdrant_client", "redis", "redis.asyncio",
             "tiktoken", "PIL", "PIL.Image"):
    _m = ModuleType(_mod)
    _m.__getattr__ = lambda _n: type("_Any", (), {"__init__": lambda *a, **k: None})  # type: ignore[attr-defined]
    sys.modules.setdefault(_mod, _m)

from app import catalog  # noqa: E402


class parchar:
    """Sustituto del `monkeypatch` de pytest (aquí no hay pytest, ver tests/run.py)."""

    def __init__(self, obj, **kwargs):
        self._obj, self._nuevos = obj, kwargs
        self._previos: dict = {}

    def __enter__(self):
        for k, v in self._nuevos.items():
            self._previos[k] = getattr(self._obj, k)
            setattr(self._obj, k, v)
        return self._obj

    def __exit__(self, *exc):
        for k, v in self._previos.items():
            setattr(self._obj, k, v)
        return False


def _prod(pid, nombre, tipo="dildo", atributos=(), zona=None):
    return {"id": pid, "nombre": nombre, "descripcion": "", "categoria": tipo,
            "precio": 90000, "imagen_url": f"http://x/{pid}.jpg", "galeria_urls": None,
            "permalink": None, "tipo": tipo, "zona": zona, "vibra": False,
            "control": None, "genero_uso": None, "atributos": list(atributos)}


# Los cuatro están etiquetados con el atributo "ventosa"; solo dos lo llevan
# en el nombre. Es el caso real del transcript.
CON_VENTOSA = [
    _prod(1, "Consolador Realista con Ventosa Maru 20 cm", atributos=["ventosa"]),
    _prod(2, "Dildo Realista Baru 21 cm en Silicona", atributos=["ventosa"]),
    _prod(3, "Dildo Ultra Realista Ayron CamToyz Posable", atributos=["ventosa"]),
    _prod(4, "Dildo Consolador Softy CamToyz 22 cm con Ventosa", atributos=["ventosa"]),
]


def _catalogo_fake(filas):
    """Sustituye el único punto que toca la DB en esta ruta."""
    async def fake_fetch(sql, *params):
        return [dict(p) for p in filas]
    return parchar(catalog, _fetch_restricciones=fake_fetch)


def test_no_se_filtra_por_nombre_lo_que_la_faceta_ya_filtro():
    """El SQL ya aplicó `atributos @> ['ventosa']`: los 4 son correctos."""
    with _catalogo_fake(CON_VENTOSA):
        res = asyncio.run(catalog.buscar_por_restricciones(
            {"tipo": "dildo", "atributos": ["ventosa"]}, exclude_ids=[], limit=5,
            user_text="tienen consoladores con ventosa", subtipo="ventosa"))
    assert len(res.productos) == 4, [p["nombre"] for p in res.productos]


def test_sin_el_atributo_en_las_restricciones_si_se_filtra_por_nombre():
    """Si la faceta NO filtró por ventosa, el nombre es lo único que queda."""
    with _catalogo_fake(CON_VENTOSA):
        res = asyncio.run(catalog.buscar_por_restricciones(
            {"tipo": "dildo"}, exclude_ids=[], limit=5,
            user_text="tienen consoladores con ventosa", subtipo="ventosa"))
    nombres = [p["nombre"] for p in res.productos]
    assert len(nombres) == 2, nombres
    assert all("Ventosa" in n for n in nombres), nombres


def test_un_subtipo_que_la_faceta_no_codifica_si_se_filtra_por_nombre():
    """"colegiala" no es faceta de nada (`interpretar_mensaje` devuelve {}):
    ahí el filtro por nombre es lo único que separa colegialas de mucamas."""
    disfraces = [
        _prod(10, "Disfraz Colegiala Inocente Lerot", tipo="lenceria"),
        _prod(11, "Disfraz Mucama Lerot", tipo="lenceria"),
    ]
    with _catalogo_fake(disfraces):
        res = asyncio.run(catalog.buscar_por_restricciones(
            {"tipo": "lenceria"}, exclude_ids=[], limit=5,
            user_text="disfraz de colegiala", subtipo="colegiala"))
    assert [p["nombre"] for p in res.productos] == ["Disfraz Colegiala Inocente Lerot"]


def test_la_zona_no_cuenta_como_faceta_que_ya_filtro():
    """Guarda contra ensanchar la regla a la zona.

    "próstata" está DENTRO de zona=anal, no es equivalente a ella: si coincidir
    en zona bastara para saltarse el filtro, quien pide un estimulador de
    próstata recibiría todos los plugs anales. La regla mira solo `atributos`,
    que sí es una equivalencia exacta.
    """
    anales = [
        _prod(20, "Estimulador de Próstata Ferro", tipo="plug", zona="anal"),
        _prod(21, "Plug Anal Metálico Joya Rosa", tipo="plug", zona="anal"),
    ]
    with _catalogo_fake(anales):
        res = asyncio.run(catalog.buscar_por_restricciones(
            {"tipo": "plug", "zona": "anal"}, exclude_ids=[], limit=5,
            user_text="estimulador de prostata", subtipo="prostat"))
    assert [p["nombre"] for p in res.productos] == ["Estimulador de Próstata Ferro"], \
        [p["nombre"] for p in res.productos]


def test_lo_declarado_sin_cobertura_existe_y_no_se_contradice():
    """`_SUBTIPOS_SIN_COBERTURA` dice "esto no lo vendemos, escala".

    Una entrada que no es un subtipo real no escala nada (nunca se detecta), y
    una que además tiene sinónimos se contradice a sí misma: los sinónimos
    existen para encontrar producto, y la declaración para admitir que no hay.
    """
    desconocidos = set(catalog._SUBTIPOS_SIN_COBERTURA) - set(catalog._SUBTIPO_KEYWORDS)
    assert not desconocidos, f"no son subtipos detectables: {desconocidos}"
    contradictorios = set(catalog._SUBTIPOS_SIN_COBERTURA) & set(catalog._SUBTIPO_SINONIMOS)
    assert not contradictorios, (
        f"declarados sin cobertura pero con sinónimos: {contradictorios}")


def test_las_variantes_de_un_mismo_subtipo_comparten_sinonimos():
    """Regresión de los huecos que encontró la auditoría.

    "bodies" daba 0 y "body" 8; "antifaces" 0 y "antifaz" 2. Con el filtro
    estricto ese plural no muestra menos productos: pausa el bot. Las variantes
    de una misma prenda tienen que resolver al mismo conjunto.
    """
    for variantes in (("body", "bodies", "bodys"), ("antifaz", "antifaces")):
        conjuntos = [set(catalog._SUBTIPO_SINONIMOS.get(v, [v])) for v in variantes]
        base = conjuntos[0]
        for v, c in zip(variantes, conjuntos):
            assert base & c, f"{v!r} no comparte vocabulario con {variantes[0]!r}"


def test_los_sinonimos_con_tilde_casan_porque_se_normalizan_los_dos_lados():
    """Las entradas acentuadas de la tabla ("próstata", "cánula") solo funcionan
    porque `_filtrar_por_subtipo` normaliza ambos lados. Si alguien vuelve a
    comparar en crudo, esto lo caza."""
    prods = [{"nombre": "Estimulador de Próstata Ferro", "descripcion": ""}]
    assert catalog._filtrar_por_subtipo(prods, "prostat"), "sin match con tilde"


def test_un_subtipo_que_no_vendemos_devuelve_vacio_para_que_escale():
    """Antes se degradaba a la categoría completa "para no dejarlo sin fotos".

    El cliente pedía una fusta y recibía esposas y antifaces: saturarlo con lo
    que no pidió. Ahora devuelve vacío, y el orquestador lo convierte en
    `sin_stock_subtipo` → pasa la conversación a un asesor, que además puede
    confirmar si entró mercancía que el catálogo aún no refleja.
    """
    bondage = [
        _prod(30, "Esposas Peluche Rojas", tipo="bondage"),
        _prod(31, "Antifaz Satinado Negro", tipo="bondage"),
    ]
    with _catalogo_fake(bondage):
        res = asyncio.run(catalog.buscar_por_restricciones(
            {"tipo": "bondage"}, exclude_ids=[], limit=5,
            user_text="tienen fustas", subtipo="fusta"))
    assert res.productos == [], [p["nombre"] for p in res.productos]


def test_el_conteo_tambien_da_cero_y_no_promete_ver_mas():
    """Si el conteo siguiera contando la categoría, el bot ofrecería "ver más"
    de algo que no va a poder mostrar."""
    bondage = [_prod(30, "Esposas Peluche Rojas", tipo="bondage")]
    with _catalogo_fake(bondage):
        n = asyncio.run(catalog.contar_por_restricciones(
            {"tipo": "bondage"}, subtipo="fusta"))
    assert n == 0, n


def test_la_degradacion_no_revive_por_la_puerta_de_la_relajacion():
    """`buscar_por_restricciones` cede facetas cuando no encuentra nada.

    Esa escalera NO debe servir de degradación encubierta: si se soltara el
    tipo, "fusta" acabaría devolviendo dildos. El subtipo no se relaja nunca.
    """
    mezcla = [
        _prod(40, "Esposas Peluche Rojas", tipo="bondage"),
        _prod(41, "Dildo Realista Baru 21 cm", tipo="dildo"),
    ]
    with _catalogo_fake(mezcla):
        res = asyncio.run(catalog.buscar_por_restricciones(
            {"tipo": "bondage", "genero_uso": "pareja"}, exclude_ids=[], limit=5,
            permitir_relajar=True, user_text="tienen fustas", subtipo="fusta"))
    assert res.productos == [], [p["nombre"] for p in res.productos]
