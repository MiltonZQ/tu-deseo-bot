"""Preguntar UNA cosa antes de listar, cuando la petición es demasiado amplia.

El caso que lo motivó (1/08/26):

    cliente: quiero ver lubricantes
    bot:     1️⃣ BliX Lubricante Anal   2️⃣ Lubricante Anal Sen Intimo
             3️⃣ BliX H2O Neutro        4️⃣ Lubricante Anal 500 Ml
             5️⃣ Lubricante Neutro

Hay ~20 lubricantes ofrecibles y solo 5 huecos, así que el cliente recibe una
muestra al azar de cosas que no se parecen entre sí en vez de lo que buscaba.
Con una pregunta ("¿neutro, con sabores, anal…?") los 5 huecos se llenan con lo
que pidió.

La pregunta se construye desde el inventario REAL: ofrecer una rama sin producto
detrás es peor que no preguntar, porque el cliente la elige, la consulta exacta
da 0 filas y la escalera de relajación le devuelve justo lo que no pidió.
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

from app import catalog  # noqa: E402


# No hay pytest en el entorno (ver tests/run.py), así que tampoco hay fixture
# `monkeypatch`. Este contextmanager hace lo mismo y deja el módulo como estaba.
class parchar:
    """with parchar(catalog, buscar_por_restricciones=fake): ..."""

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


# ── facetas_disponibles: qué ramas tienen producto ofrecible ──

FILAS = [
    {"atributos": ["agua", "neutro"], "zona": "ninguna", "genero_uso": "unisex"},
    {"atributos": ["agua", "sabor"], "zona": "ninguna", "genero_uso": "mujer"},
    {"atributos": ["desensibilizante"], "zona": "anal", "genero_uso": "unisex"},
]


def test_facetas_disponibles_agrupa_por_atributo_zona_y_genero():
    async def fake(restricciones, exclude_ids, limit):
        return FILAS

    with parchar(catalog, _consultar_restricciones=fake):
        res = asyncio.run(catalog.facetas_disponibles({"tipo": "lubricante"}))
    assert res["atributos"]["agua"] == 2
    assert res["atributos"]["sabor"] == 1
    assert res["zonas"]["anal"] == 1
    assert res["zonas"]["ninguna"] == 2
    assert res["generos"]["unisex"] == 2


def test_facetas_disponibles_no_revienta_sin_db():
    """Una excepción aquí no puede tumbar el turno: sin recuentos, no se pregunta."""
    async def boom(*a, **k):
        raise RuntimeError("sin pool")

    with parchar(catalog, _consultar_restricciones=boom):
        res = asyncio.run(catalog.facetas_disponibles({"tipo": "lubricante"}))
    assert res == {"atributos": {}, "zonas": {}, "generos": {}}


def test_facetas_disponibles_sin_restricciones_no_consulta():
    llamadas = []

    async def fake(restricciones, exclude_ids, limit):
        llamadas.append(restricciones)
        return FILAS

    with parchar(catalog, _consultar_restricciones=fake):
        res = asyncio.run(catalog.facetas_disponibles({}))
    assert res == {"atributos": {}, "zonas": {}, "generos": {}}
    assert not llamadas, "sin ancla, contar el catálogo entero no dice nada"


# ── El menú: qué se le pregunta al cliente ──

from app import facetas, preguntas  # noqa: E402

DISPONIBLE = {"atributos": {"neutro": 5, "sabor": 6, "calor": 3, "silicona": 1},
              "zonas": {"anal": 4, "ninguna": 16}, "generos": {}}


def test_solo_ofrece_ramas_con_stock():
    texto = preguntas.construir("lubricante", DISPONIBLE)
    assert "sabores" in texto and "anal" in texto
    assert "silicona" not in texto, "1 producto no es una rama del menú"


def test_respeta_el_maximo_de_ramas():
    """Más de 4 opciones en un mensaje de WhatsApp no se leen, se saltan."""
    abundante = {"atributos": {a: 9 for a in
                               ("neutro", "sabor", "calor", "frio", "silicona", "hibrido")},
                 "zonas": {"anal": 9}, "generos": {}}
    texto = preguntas.construir("lubricante", abundante)
    ofrecidas = [etq for _c, _g, etq in preguntas._MENUS["lubricante"]
                 if etq.strip("*") in texto or etq in texto]
    assert len(ofrecidas) == preguntas.MAX_RAMAS


def test_sin_ramas_suficientes_no_pregunta():
    """Con una sola rama viva no hay nada que preguntar: se lista y ya."""
    assert preguntas.construir("lubricante", {"atributos": {"sabor": 9},
                                              "zonas": {}, "generos": {}}) is None


def test_tipo_sin_menu_no_pregunta():
    assert preguntas.construir("enema", DISPONIBLE) is None
    assert preguntas.construir(None, DISPONIBLE) is None


def test_recuentos_vacios_no_preguntan():
    assert preguntas.construir("lubricante", {"atributos": {}, "zonas": {}, "generos": {}}) is None


def test_la_respuesta_del_cliente_filtra_de_verdad():
    """Cada rama del menú debe ser interpretable por el vocabulario cliente.

    Si no, se pregunta algo que luego no se sabe leer: el cliente responde
    "híbrido", el mensaje no aporta ninguna restricción, y se le lista lo mismo
    que si no hubiera contestado.
    """
    for tipo, menu in preguntas._MENUS.items():
        for clave, grupo, _etiqueta in menu:
            leido = facetas.interpretar_mensaje(clave)
            assert leido, f"[{tipo}] la rama {clave!r} no la entiende interpretar_mensaje"
            campo = {"atributos": "atributos", "zonas": "zona", "generos": "genero_uso"}[grupo]
            assert campo in leido, (
                f"[{tipo}] la rama {clave!r} debería aportar {campo}, aportó {leido}")


def test_las_ramas_existen_en_el_vocabulario_cerrado():
    """Una rama mal escrita nunca tendría recuento y jamás se ofrecería."""
    for tipo, menu in preguntas._MENUS.items():
        assert tipo in facetas.TIPOS
        for clave, grupo, _etiqueta in menu:
            if grupo == "zonas":
                assert clave in facetas.ZONAS, f"zona desconocida: {clave}"
            elif grupo == "generos":
                assert clave in facetas.GENEROS, f"género desconocido: {clave}"
            else:
                assert clave in facetas.ATRIBUTOS, f"atributo desconocido: {clave}"


# ── La compuerta: cuándo se pregunta y cuándo no ──

from tests.stubs import importar_main  # noqa: E402

main = importar_main()

DISP_REAL = {"atributos": {"neutro": 3, "sabor": 3, "calor": 3, "frio": 2},
             "zonas": {"anal": 3, "ninguna": 17}, "generos": {}}

PRODS = [{"id": i, "nombre": f"Lubricante {i}", "precio": 30000,
          "imagen_url": "http://x/i.jpg", "tipo": "lubricante",
          "zona": "ninguna", "atributos": ["agua"]} for i in range(1, 6)]


def _catalogo(total=20, disponibles=None, productos=None):
    """Parcha las tres consultas a la DB que toca el camino de restricciones."""
    disponibles = DISP_REAL if disponibles is None else disponibles
    productos = PRODS if productos is None else productos

    async def contar(restricciones):
        return total

    async def disp(restricciones):
        return disponibles

    async def buscar(restricciones, exclude_ids=None, limit=5, permitir_relajar=True,
                     user_text=""):
        return catalog.Resultado(productos=list(productos), restricciones=restricciones)

    # Los caminos de respaldo (búsqueda por nombre, corrección de typos) tocan el
    # pool directamente; sin silenciarlos el test revienta con 'NoneType has no
    # attribute acquire' en vez de decir qué falló.
    async def sin_nombre(*a, **k):
        return []

    async def sin_typos(texto, *a, **k):
        return texto

    return parchar(catalog, contar_por_restricciones=contar,
                   facetas_disponibles=disp, buscar_por_restricciones=buscar,
                   buscar_producto_especifico=sin_nombre,
                   corregir_typos_contra_catalogo=sin_typos)


def _estado(**kwargs):
    base = {"categoria_busqueda": "lubricantes-y-cuidado",
            "categoria_funcional": "lubricantes-y-cuidado", "genero": None,
            "calificado": False, "productos_mostrados": [],
            "restricciones": {"tipo": "lubricante"}, "preguntas_hechas": []}
    base.update(kwargs)
    return base


# Un turno que YA iba a listar: el estado viene calificado, así que la regla
# híbrida anti-bucle enciende debe_mostrar. Es el caso del reporte.
_IBA_A_LISTAR = dict(calificado=True)


def test_categoria_amplia_y_grande_pregunta_en_vez_de_listar():
    with _catalogo(total=20):
        cands, info = asyncio.run(
            main._recuperar_candidatos("lubricantes", [], _estado(**_IBA_A_LISTAR)))
    assert info["pregunta_faceta"], "debía preguntar antes de listar"
    assert "sabores" in info["pregunta_faceta"]
    assert cands == [], "un turno de pregunta no lleva productos"
    assert info["debe_mostrar"] is False


def test_categoria_pequena_lista_directo():
    """Con 5 opciones, dos rondas las cubren: preguntar solo añade fricción."""
    with _catalogo(total=5):
        cands, info = asyncio.run(
            main._recuperar_candidatos("lubricantes", [], _estado(**_IBA_A_LISTAR)))
    assert not info.get("pregunta_faceta")
    assert cands


def test_el_turno_que_iba_a_calificar_pregunta_sin_umbral():
    """"Quiero ver lubricantes" en frío no lista: el bot califica primero.

    Ahí iba a preguntar de todos modos, con el texto fijo de
    `_PREGUNTAS_CALIFICACION` — que ofrece "base de agua, silicona, anal
    desensibilizante o sabores" sin comprobar que existan. Se sustituye por la
    pregunta construida del inventario, y no hace falta umbral: no se está
    quitando ninguna lista.
    """
    with _catalogo(total=3):
        cands, info = asyncio.run(
            main._recuperar_candidatos("quiero ver lubricantes", [], None))
    assert info["pregunta_faceta"]
    assert cands == []


def test_no_pregunta_si_el_cliente_ya_fue_especifico():
    with _catalogo(total=20):
        cands, info = asyncio.run(main._recuperar_candidatos(
            "lubricante con sabores", [], _estado(**_IBA_A_LISTAR)))
    assert not info.get("pregunta_faceta"), "ya dijo qué quiere"
    assert cands


def test_no_pregunta_dos_veces_el_mismo_tipo():
    with _catalogo(total=20):
        _c, info = asyncio.run(main._recuperar_candidatos(
            "lubricantes", [], _estado(preguntas_hechas=["lubricante"], calificado=True)))
    assert not info.get("pregunta_faceta")


def test_no_pregunta_en_ver_mas():
    with _catalogo(total=20):
        _c, info = asyncio.run(main._recuperar_candidatos(
            "mas diseños", [], _estado(calificado=True, productos_mostrados=[1, 2, 3])))
    assert not info.get("pregunta_faceta"), "pidió más de lo mismo, no otra cosa"


def test_no_pregunta_a_mitad_de_un_listado():
    """Con productos ya mostrados, preguntar ahora borraría el hilo del listado."""
    with _catalogo(total=20):
        _c, info = asyncio.run(main._recuperar_candidatos(
            "lubricantes", [], _estado(calificado=True, productos_mostrados=[1, 2])))
    assert not info.get("pregunta_faceta")


def test_sin_ramas_vivas_lista_como_siempre():
    """Si el inventario no da para dos ramas, no se pregunta: se lista."""
    with _catalogo(total=20, disponibles={"atributos": {"agua": 20}, "zonas": {},
                                          "generos": {}}):
        cands, info = asyncio.run(
            main._recuperar_candidatos("lubricantes", [], _estado(**_IBA_A_LISTAR)))
    assert not info.get("pregunta_faceta")
    assert cands


def test_si_no_se_pueden_contar_las_facetas_lista_como_siempre():
    """Un fallo contando facetas no puede quitarle la lista al cliente."""
    with _catalogo(total=20, disponibles={"atributos": {}, "zonas": {}, "generos": {}}):
        cands, info = asyncio.run(
            main._recuperar_candidatos("lubricantes", [], _estado(**_IBA_A_LISTAR)))
    assert not info.get("pregunta_faceta")
    assert cands


def test_la_respuesta_a_la_pregunta_muestra_productos():
    """Lo que cierra el bucle: tras responder, toca mostrar — y filtrado."""
    with _catalogo(total=6):
        cands, info = asyncio.run(main._recuperar_candidatos(
            "con sabores", [], _estado(preguntas_hechas=["lubricante"])))
    assert cands, "tras responder la pregunta hay que mostrar, no re-preguntar"
    assert "sabor" in (info["restricciones"].get("atributos") or [])


# ── Regresión: "Te mostré todas las opciones" sin haber mostrado ninguna ──
#
#   [3:43] cliente: tienen masturbadores      → bot lista 5 masturbadores
#   [3:45] cliente: lubricantes tambien manejan
#   [3:46] bot:     "Te mostré todas las opciones de lubricantes disponibles 😊"
#
# El estado guarda los productos ya mostrados para no repetirlos, y `exclude` se
# arrastraba aunque el cliente hubiese cambiado de tema. Eso hacía dos daños:
# excluía productos válidos del tema nuevo, y con `exclude` no vacío disparaba
# `categoria_agotada`.
#
# El reset por cambio de tema tapa el caso cuando la clasificación aporta
# categoría E intención, pero hay más de 20 palabras que cambian el `tipo` sin
# aportar ambas ("tapon"→plug, "gel intimo"→lubricante, "strap on"→arnes,
# "jenga"→juego), y ahí el reset no salta.

ESTADO_MASTURBADORES = {"categoria_busqueda": "masturbadores",
                        "categoria_funcional": "masturbadores", "genero": None,
                        "calificado": True,
                        "productos_mostrados": [101, 102, 103],
                        "restricciones": {"tipo": "masturbador"},
                        "preguntas_hechas": []}


def _recuperar_espiando_exclude(mensaje, estado, productos=()):
    """Corre un turno devolviendo (info, excludes con los que se consultó)."""
    vistos = []

    async def buscar(restricciones, exclude_ids=None, limit=5, permitir_relajar=True,
                     user_text=""):
        vistos.append(list(exclude_ids or []))
        return catalog.Resultado(productos=list(productos), restricciones=restricciones)

    async def sin_recomendar(**k):
        return []

    with _catalogo(total=20):
        with parchar(catalog, buscar_por_restricciones=buscar,
                     get_productos_para_recomendar=sin_recomendar):
            _c, info = asyncio.run(
                main._recuperar_candidatos(mensaje, [], dict(estado)))
    return info, vistos


def test_cambiar_de_tipo_no_dice_que_ya_se_mostro_todo():
    info, _ = _recuperar_espiando_exclude("muestrame tapones", ESTADO_MASTURBADORES)
    assert info["restricciones"]["tipo"] == "plug"
    assert not info["categoria_agotada"], \
        "pidió plugs por primera vez: no se le ha mostrado ninguno"


def test_cambiar_de_tipo_no_excluye_productos_del_tema_nuevo():
    _info, vistos = _recuperar_espiando_exclude("muestrame tapones",
                                                ESTADO_MASTURBADORES)
    assert vistos and vistos[0] == [], \
        f"se arrastró el exclude del tema viejo: {vistos}"


def test_el_arrastre_no_depende_de_la_palabra():
    """Las cuatro divergencias reales entre el vocabulario de tipos y el de
    intenciones: si alguna vuelve a arrastrar el exclude, el bot le dirá al
    cliente que ya le mostró algo que nunca vio."""
    for mensaje, tipo in (("muestrame tapones", "plug"),
                          ("quiero ver gel intimo", "lubricante"),
                          ("fotos de strap on", "arnes"),
                          ("me muestras jenga", "juego")):
        info, vistos = _recuperar_espiando_exclude(mensaje, ESTADO_MASTURBADORES)
        assert info["restricciones"]["tipo"] == tipo, mensaje
        assert not info["categoria_agotada"], mensaje
        # "gel intimo" tiene menú, así que ese turno pregunta y no llega a
        # buscar. Lo que se comprueba es que ninguna búsqueda que sí ocurra
        # arrastre los IDs del tema viejo.
        assert all(e == [] for e in vistos), f"{mensaje}: {vistos}"


def test_seguir_en_el_mismo_tipo_si_respeta_lo_ya_mostrado():
    """La otra cara: dentro del mismo tema, el exclude tiene que seguir aplicando."""
    _info, vistos = _recuperar_espiando_exclude(
        "mas diseños", _estado(calificado=True, productos_mostrados=[1, 2, 3]),
        productos=PRODS)
    assert vistos and vistos[0] == [1, 2, 3]


# ── Panel: los atributos son editables a mano ──
#
# Eran la materia prima del menú pero no se podían corregir: la celda era texto
# plano y el POST releía los atributos de la DB y los reescribía igual. Un
# lubricante que se llama "Cereza" no aparecía en la rama "con sabores" y no
# había forma de arreglarlo sin tocar reglas y correr el backfill.

def test_atributos_del_panel_descarta_los_desconocidos():
    from app import admin
    assert admin._atributos_validos("sabor,agua") == ["agua", "sabor"]
    assert admin._atributos_validos("sabor,inventado,  agua ") == ["agua", "sabor"]
    assert admin._atributos_validos("") == [], "vacío es 'sin atributos', no un error"
    assert admin._atributos_validos("SABOR") == ["sabor"]


def test_el_vocabulario_publico_de_atributos_coincide_con_las_reglas():
    assert set(facetas.ATRIBUTOS) == (set(facetas._ATRIBUTOS)
                                      | set(facetas._ATRIBUTOS_ACOTADOS))
    assert list(facetas.ATRIBUTOS) == sorted(facetas.ATRIBUTOS)
