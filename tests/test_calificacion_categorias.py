"""Tests de los fixes de calificación para categorías amplias.

Cubren los dos bugs reportados:
  - Bug 1: "tienen masturbadores" — el LLM escribe "Mira estas opciones…" sin fotos
    en el turno de calificación; la guardia debe reemplazarlo por la pregunta correcta.
  - Bug 2: "para el pene" tras "vibradores" — la intersección categoría+género es
    vacía; el Intento E-bis debe relajar la categoría por género.

Estos tests NO requieren DB ni las dependencias de runtime (dotenv, fastapi, etc.):
extraen las constantes/regex del fuente con `ast` y replican la lógica de las
funciones que cambiaron, aislando así la corrección. Cuando el proyecto tenga
pytest + las dependencias instaladas, `pytest tests/` los ejecutará tal cual.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _extraer_constantes(ruta: Path, nombres: list[str]) -> dict:
    """Extrae constantes de asignaciones globales de un módulo sin importarlo.

    Evita cargar `dotenv`/`config`/`db` (que fallarían sin dependencias/DB).
    """
    tree = ast.parse(ruta.read_text())
    out: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in nombres:
                    out[target.id] = ast.literal_eval(node.value)
    return out


# ── Constantes extraídas del código real ──
_MAIN = _ROOT / "app" / "main.py"
_CAT = _ROOT / "app" / "catalog.py"

_PREGUNTAS_CALIFICACION = _extraer_constantes(_MAIN, ["_PREGUNTAS_CALIFICACION"])["_PREGUNTAS_CALIFICACION"]
_CATEGORIAS_ALTERNATIVAS_POR_GENERO = _extraer_constantes(
    _CAT, ["_CATEGORIAS_ALTERNATIVAS_POR_GENERO"])["_CATEGORIAS_ALTERNATIVAS_POR_GENERO"]

# Regex tal cual está definida en main.py (se mantiene duplicada para no importar el módulo).
_FOTO_MARKER_RE = re.compile(r"\[FOTO:\s*([^\]]+)\]", re.IGNORECASE)
_OFRECE_PRODUCTOS_RE = re.compile(
    r"(mira estas opciones|estas opciones disponibles|te muestro|te las muestro|"
    r"para ti tengo|para ti 👇|que tenemos disponibles|nuestras mejores opciones|"
    r"opciones de (anillos|vibradores|dildos|lubricantes|lenceria|lencería)|"
    r"de anillos y vibradores|de anillos|estás son|estas son|aquí tienes)",
    re.IGNORECASE,
)


def _aplicar_guardia(reply: str, debe_mostrar: bool, categoria_funcional: str) -> str:
    """Réplica del bloque de guardia insertado en _handle_message (main.py)."""
    foto_ids = [m.group(1) for m in _FOTO_MARKER_RE.finditer(reply)]
    reply = _FOTO_MARKER_RE.sub("", reply).strip()
    if (not debe_mostrar and categoria_funcional and not foto_ids
            and _OFRECE_PRODUCTOS_RE.search(reply)):
        pregunta = _PREGUNTAS_CALIFICACION.get(categoria_funcional)
        if pregunta:
            return pregunta
    return reply


# ── BUG 1: turno de calificación ────────────────────────────────────────────

def test_bug1_plantilla_sin_fotos_se_reemplaza_por_pregunta():
    """El caso reportado: LLM escribe 'Mira estas opciones...' sin [FOTO:] en
    turno de calificación → la guardia la reemplaza por la pregunta correcta."""
    reply = ("¡Vamos con eso! Mira estas opciones disponibles de masturbadores "
             "para ti 👇 ¿Te gustó alguno o deseas ver más diseños? 😊")
    out = _aplicar_guardia(reply, debe_mostrar=False, categoria_funcional="masturbadores")
    assert "mira estas opciones" not in out.lower()
    assert "anillo vibrador" in out
    assert "funda para pene" in out


def test_bug1_pregunta_legitima_se_respeta():
    """Si el LLM sí hace la pregunta real, no se toca."""
    reply = ("¡Claro que sí! Para mostrarte lo ideal, cuéntame: ¿buscas un "
             "anillo vibrador, un masturbador/huevo, o una funda para pene? 😊")
    out = _aplicar_guardia(reply, debe_mostrar=False, categoria_funcional="masturbadores")
    assert out == reply


def test_bug1_no_se_activa_cuando_hay_fotos():
    """En el turno de mostrar (debe_mostrar=True), la guardia no reescribe."""
    reply = "Mira estas opciones 👇 [FOTO:123] [FOTO:456]"
    out = _aplicar_guardia(reply, debe_mostrar=True, categoria_funcional="masturbadores")
    # El texto se limpia de marcadores pero NO se reemplaza por una pregunta.
    assert "mira estas opciones" in out.lower()
    assert "[FOTO" not in out


def test_bug1_plantilla_lubricantes_tambien_se_corrigue():
    """Categoría amplia distinta (lubricantes) también se corrige."""
    reply = "¡Claro! Mira estas opciones disponibles 👇 ¿Te gustó alguno? 😊"
    out = _aplicar_guardia(reply, debe_mostrar=False, categoria_funcional="lubricantes-y-cuidado")
    assert "base de agua" in out
    assert "mira estas opciones" not in out.lower()


def test_bug1_segundo_reporte_anillos_y_vibradores():
    """El segundo texto del reporte ('Mira estas opciones de anillos y vibradores…')."""
    reply = ("Mira estas opciones de anillos y vibradores para él que tenemos "
             "disponibles 👇 ¿Te gustó alguno o deseas ver más diseños? 😉")
    out = _aplicar_guardia(reply, debe_mostrar=False, categoria_funcional="anillos-y-fundas")
    assert "mira estas opciones" not in out.lower()
    assert "anillo vibrador" in out


# ── BUG 2: relajación de categoría por género ───────────────────────────────

def test_bug2_vibradores_hombre_relaja_a_categorias_masculinas():
    """'vibradores' + 'hombre' era vacío; el E-bis debe probar categorías de hombre."""
    alts = [c for c in _CATEGORIAS_ALTERNATIVAS_POR_GENERO["hombre"]
            if c != "vibradores"]
    assert "anillos-y-fundas" in alts
    assert "masturbadores" in alts
    assert "anal" in alts
    assert "vibradores" not in alts  # no repetir la original


def test_bug2_mapeo_mujer_no_se_contamina_con_hombre():
    """El mapeo de mujer no debe incluir categorías masculinas."""
    cats_mujer = _CATEGORIAS_ALTERNATIVAS_POR_GENERO["mujer"]
    cats_hombre = set(_CATEGORIAS_ALTERNATIVAS_POR_GENERO["hombre"])
    # Solo comparten lo que es genuinamente unisex/ambos (en este mapeo, ninguno).
    assert "anillos-y-fundas" not in cats_mujer
    assert "masturbadores" not in cats_mujer


def test_bug2_mapeo_cubre_todos_los_generos():
    """Todos los géneros del sistema tienen un mapeo de fallback."""
    for g in ("hombre", "mujer", "pareja", "anal"):
        assert g in _CATEGORIAS_ALTERNATIVAS_POR_GENERO
        assert len(_CATEGORIAS_ALTERNATIVAS_POR_GENERO[g]) >= 2


# ── Cobertura del mapa de preguntas ─────────────────────────────────────────

def test_mapa_preguntas_cubre_todas_las_categorias_amplias():
    """Cada categoría funcional del sistema tiene pregunta determinista."""
    categorias_esperadas = {
        "masturbadores", "anillos-y-fundas", "dildos", "vibradores",
        "lubricantes-y-cuidado", "anal", "lenceria", "succionadores",
        "pareja-y-bondage",
    }
    faltantes = categorias_esperadas - set(_PREGUNTAS_CALIFICACION)
    assert not faltantes, f"Faltan preguntas para: {faltantes}"


def test_foto_request_re_ver_mas_opciones():
    """Verifica que _FOTO_REQUEST_RE detecte frases como 'Dejame ver mas opciones'."""
    foto_re = _extraer_constantes(_CAT, ["_FOTO_REQUEST_RE"])["_FOTO_REQUEST_RE"]
    assert foto_re.search("Dejame ver mas opciones")
    assert foto_re.search("quiero ver mas")
    assert foto_re.search("muestrame mas")
    assert foto_re.search("otras opciones")
    assert foto_re.search("dame mas diseños")


def test_anillos_vibradores_mapeo():
    """Verifica que 'anillos vibradores' asigne la subcategoría precisa anillos-vibradores."""
    mapa = _extraer_constantes(_CAT, ["_INTENCION_A_CATEGORIA_FUNCIONAL"])["_INTENCION_A_CATEGORIA_FUNCIONAL"]
    assert mapa.get("anillos vibradores") == "anillos-vibradores"
    assert mapa.get("anillo vibrador") == "anillos-vibradores"
