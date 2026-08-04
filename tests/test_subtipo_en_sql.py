"""El camino SQL (buscar_por_restricciones) debe respetar el subtipo pedido.

Pasó en producción: "Tienen disfraz de colegiala" → se detectaba
subtipo_detectado="colegiala" pero `_consultar_restricciones` solo filtraba por
tipo=lenceria, así que llegaban Mucama y Playboy junto a las colegialas.
El test existente en test_disfraces_especificos.py cubre el camino LEGACY, que
solo corre cuando este ya devolvió vacío — por eso el bug pasó desapercibido.
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
    """Sustituto del `monkeypatch` de pytest (aquí no hay pytest)."""

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


def _prod(pid, nombre):
    return {"id": pid, "nombre": nombre, "descripcion": "", "categoria": "lenceria",
            "precio": 119800, "imagen_url": f"http://x/{pid}.jpg", "galeria_urls": None,
            "permalink": None, "tipo": "lenceria", "zona": None, "vibra": False,
            "control": None, "genero_uso": None, "atributos": []}


DISFRACES = [
    _prod(1, "Disfraz Colegiala Inocente Lerot"),
    _prod(2, "Disfraz Colegiala Rojo Dulce Tentación"),
    _prod(3, "Disfraz Colegiala Negro Dulce Tentación"),
    _prod(4, "Disfraz Mucama Lerot"),
    _prod(5, "Disfraz Playboy Lerot"),
    _prod(6, "Disfraz Policía Lerot"),
    _prod(7, "Disfraz Sailor Moon Lerot"),
]


def _catalogo_fake(filas):
    async def fake_fetch(sql, *params):
        return [dict(p) for p in filas]
    return parchar(catalog, _fetch_restricciones=fake_fetch)


def test_colegiala_filtra_solo_colegialas_en_el_camino_sql():
    with _catalogo_fake(DISFRACES):
        res = asyncio.run(catalog.buscar_por_restricciones(
            {"tipo": "lenceria"}, exclude_ids=[], limit=5,
            user_text="tienen disfraz de colegiala", subtipo="colegiala"))
    nombres = [p["nombre"] for p in res.productos]
    assert len(nombres) == 3, nombres
    assert all("Colegiala" in n for n in nombres), nombres


def test_el_subtipo_con_tilde_en_el_catalogo_tambien_matchea():
    """'policia' (sin tilde, como lo escribe el cliente) debe encontrar
    'Disfraz Policía Lerot'. Con ILIKE en SQL esto daría 0 filas."""
    with _catalogo_fake(DISFRACES):
        res = asyncio.run(catalog.buscar_por_restricciones(
            {"tipo": "lenceria"}, exclude_ids=[], limit=5,
            user_text="disfraz de policia", subtipo="policia"))
    assert [p["nombre"] for p in res.productos] == ["Disfraz Policía Lerot"], res.productos


def test_un_subtipo_sin_vocabulario_en_el_catalogo_no_deja_al_cliente_sin_nada():
    """Degradación: 'con app' no aparece en ningún nombre de disfraz. Filtrar a
    ciegas ahí convertiría un listado válido en un escalado a humano."""
    with _catalogo_fake(DISFRACES):
        res = asyncio.run(catalog.buscar_por_restricciones(
            {"tipo": "lenceria"}, exclude_ids=[], limit=5,
            user_text="disfraces", subtipo="con app"))
    assert len(res.productos) == 5, res.productos


def test_en_ver_mas_el_filtro_es_duro_y_no_rellena_con_otros_disfraces():
    """Ya vio las 3 colegialas: la respuesta correcta es vacío, no Mucama."""
    with _catalogo_fake([p for p in DISFRACES if p["id"] not in (1, 2, 3)]):
        res = asyncio.run(catalog.buscar_por_restricciones(
            {"tipo": "lenceria"}, exclude_ids=[1, 2, 3], limit=5,
            permitir_relajar=False,
            user_text="tienen disfraz de colegiala", subtipo="colegiala"))
    assert res.productos == [], res.productos


def test_el_conteo_para_ver_mas_usa_el_mismo_subtipo():
    """Si el conteo dice 20 y la búsqueda mostró 3, el bot ofrece 'ver más' de
    algo que ya mostró entero. Búsqueda y conteo deben coincidir."""
    with _catalogo_fake(DISFRACES):
        n = asyncio.run(catalog.contar_por_restricciones(
            {"tipo": "lenceria"}, subtipo="colegiala"))
    assert n == 3, n
