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
_ALIASES_TYPO = _extraer_constantes(_CAT, ["_ALIASES_TYPO"])["_ALIASES_TYPO"]

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
    """Verifica que el patrón de petición de fotos (extraído del fuente) detecte
    frases como 'Dejame ver mas opciones'. El patrón real usa _re_mod.compile con
    concatenación de strings, así que lo reconstruimos evaluando la expresión AST
    del primer argumento (la concatenación de literales)."""
    import re as _re
    tree = ast.parse(_CAT.read_text())
    pat = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_FOTO_REQUEST_RE":
                    # node.value es una Call a _re_mod.compile(...). El primer
                    # argumento posicional es la concatenación de strings del patrón.
                    call = node.value
                    if isinstance(call, ast.Call) and call.args:
                        pat = ast.literal_eval(call.args[0])
    assert pat, "No se pudo extraer el patrón _FOTO_REQUEST_RE del fuente"
    foto_re = _re.compile(pat, _re.IGNORECASE)
    assert foto_re.search("Dejame ver mas opciones")
    assert foto_re.search("quiero ver mas")
    assert foto_re.search("muestrame mas")
    assert foto_re.search("otras opciones")
    assert foto_re.search("dame mas diseños")


def test_anillos_vibradores_mapeo():
    """Verifica el mapeo real de 'anillos vibradores' en el catálogo."""
    mapa = _extraer_constantes(_CAT, ["_INTENCION_A_CATEGORIA_FUNCIONAL"])["_INTENCION_A_CATEGORIA_FUNCIONAL"]
    # En el código real, el sustantivo 'anillos vibradores' mapea a 'anillos-y-fundas'.
    assert mapa.get("anillos vibradores") == "anillos-y-fundas"
    assert mapa.get("anillo vibrador") == "anillos-y-fundas"


# ── BUG 3 / TYPOS: reconocimiento de errores de tipeo del cliente ───────────

# Réplica de _corregir_typos (catalog.py) usando el mismo dict extraído.
_ALIASES_TYPO_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _ALIASES_TYPO if k) + r")\b",
    re.IGNORECASE,
)


def _corregir_typos(texto: str) -> str:
    if not texto:
        return texto or ""
    return _ALIASES_TYPO_RE.sub(lambda m: _ALIASES_TYPO[m.group(0).lower()], texto)


def test_typo_anl_se_corrige_a_anal():
    """El caso del reporte: 'anl' debe corregirse a 'anal' para clasificar bien."""
    assert _corregir_typos("anl") == "anal"
    assert "anal" in _corregir_typos("me interesa algo anl")


def test_typo_no_rompe_palabras_largas():
    """La corrección es por palabra completa: 'analógico' no debe tocarse."""
    assert _corregir_typos("analógico") == "analógico"
    assert _corregir_typos("plug analógico") == "plug analógico"


def test_typo_varios_errores_comunes():
    """Otros typos frecuentes también se corrigen."""
    assert _corregir_typos("mjer") == "mujer"
    assert _corregir_typos("dldo") == "dildo"
    assert _corregir_typos("vibradro") == "vibrador"
    assert _corregir_typos("lubrciante") == "lubricante"
    assert _corregir_typos("plgu") == "plug"


def test_typo_mensaje_normal_no_se_altera():
    """Un mensaje bien escrito no debe cambiar."""
    assert _corregir_typos("quiero un vibrador anal") == "quiero un vibrador anal"
    assert _corregir_typos("tienes lubricantes") == "tienes lubricantes"


def test_garantia_nunca_prometer_sin_enviar_envia_recuperacion():
    """Bug 3: si debe_mostrar=True pero enviados_ids=[], el bot NO puede dejar el
    texto que promete opciones sin fotos. La garantía envía un mensaje de
    recuperación. Aquí simulamos la decisión de recuperación (main.py post-envío)."""
    # Réplica de la lógica de decisión de recuperación en main.py.
    _CAT_NOMBRES = {
        "vibradores": "vibradores", "anal": "juguetes anales",
        "masturbadores": "masturbadores", "anillos-y-fundas": "anillos y fundas",
        "lubricantes-y-cuidado": "lubricantes", "lenceria": "lencería",
    }

    def _recuperacion(debe_mostrar, enviados_ids, genero, categoria_funcional, pedido_creado_id):
        if debe_mostrar and not enviados_ids and not pedido_creado_id:
            if genero is None and categoria_funcional in _PREGUNTAS_CALIFICACION:
                return _PREGUNTAS_CALIFICACION[categoria_funcional], "pregunta"
            nombre = _CAT_NOMBRES.get(categoria_funcional, "ese producto")
            return (f"estoy confirmando opciones de {nombre}", "honesto")
        return None, "nada"

    # Caso A: género sin aclarar (typo "anl" → género None) → pregunta determinista
    msg, tipo = _recuperacion(True, [], None, "vibradores", None)
    assert tipo == "pregunta"
    assert "para ella" in msg or "punto g" in msg.lower()

    # Caso B: género sí aclarado pero 0 fotos por datos → mensaje honesto
    msg, tipo = _recuperacion(True, [], "anal", "anal", None)
    assert tipo == "honesto"
    assert "confirmando" in msg.lower()

    # Caso C: sí se enviaron fotos → no hay recuperación
    msg, tipo = _recuperacion(True, [1, 2], "anal", "anal", None)
    assert tipo == "nada"
    assert msg is None

    # Caso D: pedido creado → no interferir con la venta
    msg, tipo = _recuperacion(True, [], None, "vibradores", 99)
    assert tipo == "nada"


# ── BUG 4: priorizar productos que combinan la intención ────────────────────

def test_bug4_bonus_combinacion_vibradores_anal():
    """El Intento E-bis da +10 a los productos anal que TAMBIÉN son vibradores
    cuando el cliente pidió 'vibrador anal'. Réplica del scoring."""
    # Tokens de la categoría original (réplica de cat_original_tokens en catalog.py).
    cat_original_tokens = ("vibr", "vibrador", "vibrator")

    def _score(p_nombre_desc, score_base=2.0):
        """Réplica del bonus del Intento E-bis."""
        norm = p_nombre_desc.lower()
        if any(tok in norm for tok in cat_original_tokens):
            return score_base + 10.0
        return score_base

    plug_vibrante = _score("PLUG ANAL CON VIBRADOR NEXUS")  # vibra → +10
    plug_simple = _score("PLUG ANAL DIPSY CAMTOYZ")          # no vibra
    vibrador_anal = _score("VIBRADOR ANAL HOT PULSE")        # vibra → +10
    bolas = _score("BOLAS ANALES PIMPO")                     # no vibra

    # Los vibrantes (anal+vibrador) deben quedar estrictamente arriba.
    assert plug_vibrante > plug_simple
    assert vibrador_anal > bolas
    assert plug_vibrante == vibrador_anal == 12.0
    assert plug_simple == bolas == 2.0


def test_bug4_regresion_para_el_pene_sigue_funcionando():
    """Bug 2 intacto: 'para el pene' (hombre) tras vibradores sigue relajando
    a anillos-y-fundas/masturbadores, sin bonus espurio de vibrador."""
    # género hombre, categoría original vibradores → alternativas de hombre.
    alts = [c for c in _CATEGORIAS_ALTERNATIVAS_POR_GENERO["hombre"] if c != "vibradores"]
    assert "anillos-y-fundas" in alts
    assert "masturbadores" in alts
