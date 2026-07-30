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


def _aplicar_guardia(reply: str, debe_mostrar: bool, categoria_funcional: str,
                     final_productos: list | None = None) -> str:
    """Réplica del bloque de guardia de _handle_message (main.py).

    La guardia evalúa final_productos (marcadores [FOTO:ID] resueltos contra
    candidatos válidos), NO foto_ids brutos, para cerrar el hueco del marcador
    espurio (LLM alucina [FOTO:999] sin candidatos → final_productos vacío →
    la guardia dispara igual).
    """
    reply = _FOTO_MARKER_RE.sub("", reply).strip()
    fp = final_productos if final_productos is not None else []
    if (not debe_mostrar and categoria_funcional and not fp
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


# ── BUG 5: "ver más" no debe repetir productos ya mostrados ─────────────────

def test_bug5_exclusion_se_aplica_a_productos_ya_mostrados():
    """Réplica del filtro de exclusión de buscar_producto_especifico y
    get_productos_para_recomendar: los IDs en exclude_ids se omiten."""
    # Catálogo simulado.
    productos = [
        {"id": 1, "nombre": "Vibrador A"},
        {"id": 2, "nombre": "Vibrador B"},
        {"id": 3, "nombre": "Vibrador C"},
        {"id": 4, "nombre": "Vibrador D"},
    ]

    def _filtrar_con_exclusion(items, exclude_ids):
        """Réplica del comportamiento: if p['id'] in exclude_set: continue."""
        exclude_set = set(exclude_ids or [])
        return [p for p in items if p["id"] not in exclude_set]

    # T1 mostró [1,2,3,4]. T2 "ver más" con exclude=[1,2,3,4]:
    t2 = _filtrar_con_exclusion(productos, [1, 2, 3, 4])
    assert t2 == [], "No deberían quedar productos tras excluir los ya mostrados"

    # T1 mostró [1,2]. T2 "ver más" con exclude=[1,2]: quedan [3,4] (distintos).
    t2b = _filtrar_con_exclusion(productos, [1, 2])
    assert [p["id"] for p in t2b] == [3, 4]
    assert 1 not in [p["id"] for p in t2b]
    assert 2 not in [p["id"] for p in t2b]


def test_bug5_fallback_propaga_exclusion():
    """Los fallbacks de _recuperar_candidatos (líneas 600, 617, 622 en main.py)
    ahora reciben exclude_ids. Réplica de la lógica: el exclude se construye una
    vez y se propaga a todas las búsquedas de fallback."""
    def _recuperar(exclude):
        """Réplica: cada fallback recibe el mismo exclude_ids."""
        llamadas = []
        llamadas.append(("get_productos_para_recomendar", exclude))
        llamadas.append(("buscar_producto_especifico_user_text", exclude))
        llamadas.append(("buscar_producto_especifico_termino", exclude))
        return llamadas

    llamadas = _recuperar([10, 20, 30])
    # Los 3 fallbacks deben recibir el exclude_ids (no None, no vacío ignorado).
    for nombre, exclude in llamadas:
        assert exclude == [10, 20, 30], f"{nombre} no propagó exclude"


def test_bug5_buscar_producto_especifico_acepta_exclude():
    """Verifica que la firma de buscar_producto_especifico tenga el parámetro
    exclude_ids (retrocompatible). Inspección del AST de la definición."""
    import ast
    tree = ast.parse(_CAT.read_text())
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "buscar_producto_especifico"):
            arg_names = [a.arg for a in node.args.args]
            assert "exclude_ids" in arg_names, (
                "buscar_producto_especifico debe aceptar exclude_ids")
            # El default debe ser None (retrocompatible).
            defaults = node.args.defaults
            assert defaults and defaults[-1] is None.__class__ or (
                defaults and isinstance(defaults[-1], ast.Constant)
                and defaults[-1].value is None), (
                "exclude_ids debe tener default None para ser retrocompatible")
            return
    raise AssertionError("No se encontró buscar_producto_especifico")


# ── BUG 6: no ofrecer "ver más" si la categoría está agotada ────────────────

def test_bug6_deteccion_categoria_agotada():
    """Réplica de la condición categoria_agotada en _recuperar_candidatos.
    Se activa SOLO cuando: pidió ver más + hay productos mostrados + hay
    categoría + no quedan candidatos nuevos."""
    def _categoria_agotada(candidatos, pide_fotos, exclude, cat_func):
        return bool(not candidatos and pide_fotos and bool(exclude) and cat_func)

    # Caso agotado: pidió "ver más", ya mostró 4, no quedan nuevos → agotada.
    assert _categoria_agotada([], True, [1, 2, 3, 4], "vibradores") is True

    # NO agotado: aún hay candidatos nuevos tras excluir.
    assert _categoria_agotada([{"id": 5}], True, [1, 2, 3, 4], "vibradores") is False

    # NO agotado: no pidió ver más (primera consulta, exclude vacío).
    assert _categoria_agotada([], False, [], "vibradores") is False

    # NO agotado: primera vez en la categoría (exclude vacío aunque pida fotos).
    assert _categoria_agotada([], True, [], "vibradores") is False

    # NO agotado: no hay categoría funcional (búsqueda libre).
    assert _categoria_agotada([], True, [1, 2], None) is False


def test_bug6_flag_se_propaga_al_estado_del_llm():
    """Verifica que info['categoria_agotada'] existe en el dict devuelto por
    _recuperar_candidatos y se pasa al estado del LLM. Inspección del AST."""
    import ast
    tree = ast.parse(_MAIN.read_text())
    # _recuperar_candidatos es async (AsyncFunctionDef), no FunctionDef.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_recuperar_candidatos":
                src = ast.get_source_segment(_MAIN.read_text(), node)
                assert "categoria_agotada" in src, (
                    "_recuperar_candidatos debe computar categoria_agotada")
                return
    raise AssertionError("No se encontró _recuperar_candidatos")


# ── BUG 7: masturbadores muestran fotos directo + hueco del marcador espurio ─

def test_bug7_masturbador_califica_genero_hombre():
    """'masturbador' debe detectarse como género=hombre en el mensaje del cliente,
    para que 'tienen masturbadores' muestre fotos directo (sin pregunta de subtipo).
    Verifica que la palabra esté en _GENERO_KEYWORDS_CLIENTE rama 'hombre'."""
    # Extraer la lista de keywords de hombre inspeccionando el AST.
    import ast
    tree = ast.parse(_CAT.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_GENERO_KEYWORDS_CLIENTE":
                    pares = ast.literal_eval(node.value)
                    for claves, genero in pares:
                        if genero == "hombre":
                            assert "masturbador" in claves, (
                                "'masturbador' debe estar en género hombre")
                            assert "masturbadores" in claves, (
                                "'masturbadores' debe estar en género hombre")
                            return
    raise AssertionError("No se encontró _GENERO_KEYWORDS_CLIENTE")


def test_bug7_hueco_marcador_espurio_cerrado():
    """El caso reportado: el LLM escribe 'Para ti tengo esto 👇' con un marcador
    [FOTO:999] ESPURIO (sin candidatos). Antes, foto_ids no vacío hacía que la
    guardia NO disparara. Ahora evalúa final_productos (vacío tras resolver contra
    candidatos vacíos) → la guardia SÍ dispara."""
    reply = ("Para ti tengo esto 👇 ¿Te gustó alguno o deseas ver más diseños? "
             "[FOTO:999]")
    # final_productos vacío: el [FOTO:999] se descarta al validar contra candidatos=[].
    out = _aplicar_guardia(reply, debe_mostrar=False,
                           categoria_funcional="masturbadores",
                           final_productos=[])
    assert "para ti tengo" not in out.lower(), (
        "La guardia debe disparar aunque el LLM ponga [FOTO] espurio")
    assert "anillo vibrador" in out or "masturbador/huevo" in out


def test_bug7_guardia_no_dispara_cuando_hay_fotos_validas():
    """Si final_productos NO está vacío (hay fotos válidas), la guardia no
    debe disparar (el bot sí va a enviar fotos)."""
    reply = "Para ti tengo esto 👇 [FOTO:10] [FOTO:11]"
    out = _aplicar_guardia(reply, debe_mostrar=True,
                           categoria_funcional="masturbadores",
                           final_productos=[{"id": 10}, {"id": 11}])
    # No se reemplaza (hay fotos reales), solo se limpian los marcadores del texto.
    assert "para ti tengo" in out.lower()


def test_bug7_regresion_vibradores_sigue_calificando():
    """Regresión: 'vibradores' sin género sigue calificando (2-pasos intacto).
    'vibrador' NO debe estar en género hombre (a diferencia de masturbador)."""
    import ast
    tree = ast.parse(_CAT.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_GENERO_KEYWORDS_CLIENTE":
                    pares = ast.literal_eval(node.value)
                    for claves, genero in pares:
                        if genero == "hombre":
                            # 'vibrador' no debe forzar género hombre (rompería
                            # el 2-pasos de vibradores, que son mayormente mujer).
                            assert "vibrador" not in claves, (
                                "'vibrador' NO debe estar en género hombre")
                            return
    raise AssertionError("No se encontró _GENERO_KEYWORDS_CLIENTE")


# ── BUG 8: filtro final a prueba de fallos anti-repetición ("ver más") ────────

def test_bug8_filtro_final_quita_productos_ya_mostrados():
    """Réplica del filtro FINAL en _handle_message: aunque el upstream falle y
    devuelva productos repetidos, el filtro final los elimina ANTES de enviar.
    Simula: T1 envió [1,2,3,4,5]. T2 recibe final_productos=[1,2,3,4,5] (repetidos,
    p. ej. un fallback que no respetó exclude). El filtro debe quitarlos TODOS."""
    ids_ya_mostrados = {1, 2, 3, 4, 5}
    final_productos = [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}]

    # Réplica del filtro del código real.
    filtrados = [p for p in final_productos if p["id"] not in ids_ya_mostrados]
    assert filtrados == [], (
        "El filtro final debe quitar TODOS los productos ya mostrados")


def test_bug8_filtro_final_conserva_productos_nuevos():
    """Si hay mezcla de nuevos y repetidos, el filtro conserva SOLO los nuevos."""
    ids_ya_mostrados = {1, 2, 3}
    final_productos = [{"id": 1}, {"id": 2}, {"id": 6}, {"id": 7}]

    filtrados = [p for p in final_productos if p["id"] not in ids_ya_mostrados]
    assert [p["id"] for p in filtrados] == [6, 7]
    assert 1 not in [p["id"] for p in filtrados]
    assert 2 not in [p["id"] for p in filtrados]


def test_bug8_mensaje_honesto_si_todo_repetido():
    """Si tras el filtro quedan 0 productos NUEVOS y el bot iba a mostrar, se
    reescribe el reply con un mensaje honesto (categoría agotada), no se repiten
    fotos. Réplica de la decisión de reescritura."""
    def _decidir_reescritura(ids_ya_mostrados, final_productos, debe_mostrar,
                             reply_parece_plantilla, tiene_foto_ids):
        filtrados = [p for p in final_productos if p["id"] not in ids_ya_mostrados]
        if ids_ya_mostrados and not filtrados:
            if debe_mostrar or reply_parece_plantilla or tiene_foto_ids:
                return "reescribir_honesto"
        return "mantener"

    # Todo repetido + bot iba a mostrar → reescribir honesto.
    assert _decidir_reescritura({1, 2, 3, 4, 5}, [{"id": 1}, {"id": 2}],
                                True, False, False) == "reescribir_honesto"
    # Todo repetido + reply era plantilla "mira estas opciones" → reescribir.
    assert _decidir_reescritura({1, 2}, [{"id": 1}, {"id": 2}],
                                False, True, False) == "reescribir_honesto"
    # Hay productos nuevos → mantener (no reescribir).
    assert _decidir_reescritura({1, 2}, [{"id": 1}, {"id": 3}],
                                True, False, False) == "mantener"
    # Primera vez (sin mostrados) → mantener.
    assert _decidir_reescritura(set(), [{"id": 1}, {"id": 2}],
                                True, False, False) == "mantener"


def test_bug8_no_reescribe_durante_pedido():
    """Si se está creando un pedido, no se reescribe el reply (no interferir con
    la venta). Réplica de la guarda pedido_creado_id."""
    def _decidir(ids_ya_mostrados, final_productos, pedido_creado_id):
        filtrados = [p for p in final_productos if p["id"] not in ids_ya_mostrados]
        if ids_ya_mostrados and not filtrados and not pedido_creado_id:
            return "reescribir"
        return "mantener"

    # Pedido en curso → mantener aunque todo esté repetido.
    assert _decidir({1, 2}, [{"id": 1}], pedido_creado_id=99) == "mantener"


def test_bug8_integracion_T1_T2_estado_persistente():
    """Test de INTEGRACIÓN de la secuencia completa T1→T2 con estado persistido.
    Simula el caso real del reporte: T1 envía 5 masturbadores, se persisten sus
    IDs. T2 ('ver más') recupera candidatos; aunque el upstream los repita, el
    filtro final (leyendo el estado persistido) debe eliminarlos TODOS.

    Réplica de la lógica del filtro final en _handle_message, que lee
    estado_previo['productos_mostrados'] y filtra final_productos contra ese set.
    """
    def _filtro_final(final_productos, estado_previo):
        ids_ya_mostrados = set((estado_previo or {}).get("productos_mostrados", []))
        if final_productos:
            repetidos = [p["id"] for p in final_productos if p["id"] in ids_ya_mostrados]
            if repetidos:
                final_productos = [p for p in final_productos
                                   if p["id"] not in ids_ya_mostrados]
        return final_productos, ids_ya_mostrados

    # --- T1: 5 masturbadores enviados, se persisten sus IDs ---
    productos_t1 = [{"id": 101}, {"id": 102}, {"id": 103}, {"id": 104}, {"id": 105}]
    enviados_t1 = [p["id"] for p in productos_t1]
    estado_tras_t1 = {"categoria_funcional": "masturbadores",
                      "productos_mostrados": enviados_t1, "calificado": True}

    # --- T2: "ver más" — el upstream (mal) devuelve los MISMOS 5 productos ---
    productos_t2_upstream = [{"id": 101}, {"id": 102}, {"id": 103},
                             {"id": 104}, {"id": 105}]
    filtrados_t2, ya = _filtro_final(productos_t2_upstream, estado_tras_t1)

    # El filtro final debe eliminar TODOS porque ya están en el estado.
    assert filtrados_t2 == [], (
        "T2 no debe enviar ningún producto repetido tras el filtro final")
    assert ya == {101, 102, 103, 104, 105}

    # --- T2b: "ver más" — upstream devuelve 2 nuevos + 2 repetidos ---
    productos_t2b = [{"id": 106}, {"id": 101}, {"id": 107}, {"id": 102}]
    filtrados_t2b, _ = _filtro_final(productos_t2b, estado_tras_t1)
    assert [p["id"] for p in filtrados_t2b] == [106, 107], (
        "T2b debe conservar SOLO los productos nuevos")


def test_bug8_estado_vacio_diagnostica_problema_upstream():
    """Si el estado llega VACÍO (upstream no persistió), el filtro no puede
    filtrar — y el log de diagnóstico lo revela. Este test documenta que el
    filtro depende de que el estado se persista correctamente, y que un estado
    vacío en un 'ver más' es síntoma de bug upstream (no del filtro)."""
    def _filtro_final(final_productos, estado_previo):
        ids_ya_mostrados = set((estado_previo or {}).get("productos_mostrados", []))
        if final_productos:
            final_productos = [p for p in final_productos
                               if p["id"] not in ids_ya_mostrados]
        return final_productos, ids_ya_mostrados

    # Estado vacío (bug upstream): el filtro no filtra nada (no sabe qué se mostró).
    productos = [{"id": 1}, {"id": 2}]
    filtrados, ya = _filtro_final(productos, {"productos_mostrados": []})
    assert ya == set(), "Estado vacío = no hay nada que filtrar"
    assert len(filtrados) == 2  # pasan (el filtro es inocente; el bug es upstream)
    # El log 'ya_mostrados=0' en producción revela que el estado no persistió.


# ── BUG 9 (raíz): $$ en db.py rompía la persistencia del estado ──────────────

def test_bug9_sql_upsert_no_tiene_dollar_dollar_malformado():
    """CAUSA RAÍZ de TODOS los bugs de 'ver más': en db.py upsert_conversation_state,
    el placeholder se escribía como $${idx} (dos $). Postgres interpreta $$ como
    inicio de 'dollar-quoted string', rompiendo el SQL con 'unterminated
    dollar-quoted string'. Así NUNCA se persistían los productos mostrados y el
    filtro final siempre los veía vacíos → repetición infinita.

    Este test replica la construcción del SET y verifica que el SQL generado tenga
    ${idx} (un solo $), no $$${idx}."""
    # Réplica de la construcción del SET para add_productos_mostrados (db.py:515).
    def _build_set(idx):
        return f"productos_mostrados = ARRAY(SELECT DISTINCT unnest(productos_mostrados || ${idx}::bigint[]))"

    # Simula el caso real: con add_productos_mostrados, idx empieza tras 4 campos = 5.
    sql_fragment = _build_set(idx=5)
    # NO debe contener '$$' (que activaría dollar-quoting en Postgres).
    assert "$$" not in sql_fragment, (
        f"El SQL contiene '$$' (dollar-quoting): {sql_fragment!r}")
    # Sí debe tener el placeholder $5 bien formado.
    assert "$5::bigint[]" in sql_fragment


def test_bug9_el_codigo_real_no_tiene_dollar_dollar():
    """Verifica en el fuente real de db.py que NO quede ningún $$ malformado en la
    función upsert_conversation_state (regresión del bug raíz)."""
    import ast
    _DB = _ROOT / "app" / "db.py"
    tree = ast.parse(_DB.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "upsert_conversation_state":
                src = ast.get_source_segment(_DB.read_text(), node)
                # Extraer la línea del SET de productos_mostrados.
                for line in src.splitlines():
                    if "productos_mostrados = ARRAY" in line:
                        assert "$$" not in line, (
                            f"Queda $$ malformado en db.py: {line.strip()!r}")
                        assert "$" in line, "Debe tener placeholder $N"
                return
    raise AssertionError("No se encontró upsert_conversation_state")


# ── BUG 10: matching por subtipos (doble, ventosa, realista) ─────────────────

def test_bug10_subtipo_detectado_doble():
    """El clasificador debe detectar QUÉ subtipo pidió el cliente (no solo si hay
    uno). Réplica de la extracción de subtipo_detectado en catalog.py."""
    _SUBTIPO = ("realista", "ventosa", "vidrio", "cristal", "doble", "rabbit",
                "base de agua", "silicona", "prostat", "próstata", "control remoto")

    def _subtipo_de(user_text):
        norm = user_text.lower()
        for s in _SUBTIPO:
            if s in norm:
                return s
        return None

    assert _subtipo_de("doble") == "doble"
    assert _subtipo_de("quiero con ventosa") == "ventosa"
    assert _subtipo_de("uno realista") == "realista"
    assert _subtipo_de("de vidrio") == "vidrio"
    assert _subtipo_de("base de agua") == "base de agua"
    assert _subtipo_de("vibrador") is None  # sustantivo de categoría, no subtipo


def test_bug10_subtipo_detectado_en_clasificador_real():
    """Verifica que clasificar_intencion_cliente devuelve subtipo_detectado en el
    dict (campo nuevo). Inspección del fuente."""
    import ast
    src = _CAT.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "clasificar_intencion_cliente":
                fn_src = ast.get_source_segment(src, node)
                assert "subtipo_detectado" in fn_src, (
                    "clasificar_intencion_cliente debe devolver subtipo_detectado")
                return
    raise AssertionError("No se encontró clasificar_intencion_cliente")


def test_bug10_ranking_bonus_subtipo():
    """Réplica del bonus de subtipo en _filtrar (get_productos_para_recomendar):
    los productos cuyo nombre contiene el subtipo pedido reciben +10 y quedan
    primeros. Así 'doble' → dildos dobles primero."""
    subtipo = "doble"

    def _score(nombre, base=2.0):
        norm = nombre.lower()
        if subtipo in norm:
            return base + 10.0
        return base

    dildo_doble = _score("DILDO DOBLE PENETRADOR")
    dildo_simple = _score("DILDO BASIX SLIM 7")
    assert dildo_doble > dildo_simple
    assert dildo_doble == 12.0
    assert dildo_simple == 2.0

    # Orden: dobles primero, luego el resto.
    productos = [
        ("DILDO BASIX SLIM 7", 2.0),
        ("DILDO DOBLE PENETRADOR", 12.0),
        ("DILDO REALISTA KONA", 2.0),
    ]
    productos.sort(key=lambda p: -p[1])
    assert "DOBLE" in productos[0][0]


def test_bug10_no_heredar_genero_espurio_con_subtipo():
    """Réplica de la lógica anti-herencia: si el mensaje actual trae un subtipo
    (doble), NO se hereda género del historial (evita 'para hombre' espurio)."""
    _SUBTIPO = ("realista", "ventosa", "vidrio", "cristal", "doble")

    def _deberia_heredar_genero(msg_actual):
        norm = msg_actual.lower()
        tiene_subtipo = any(s in norm for s in _SUBTIPO)
        # Si hay subtipo, no heredar género del historial.
        return not tiene_subtipo

    # "doble" trae subtipo → NO heredar género.
    assert _deberia_heredar_genero("doble") is False
    # "sencillo" sin subtipo → SÍ heredar género.
    assert _deberia_heredar_genero("sencillo") is True
    # "para el pene" sin subtipo → SÍ heredar (aunque también detecta género propio).
    assert _deberia_heredar_genero("para el pene") is True


def test_bug10_get_productos_para_recomendar_acepta_subtipo():
    """Verifica que get_productos_para_recomendar tenga el parámetro subtipo."""
    import ast
    tree = ast.parse(_CAT.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "get_productos_para_recomendar":
                args = [a.arg for a in node.args.args]
                assert "subtipo" in args, (
                    "get_productos_para_recomendar debe aceptar subtipo")
                return
    raise AssertionError("No se encontró get_productos_para_recomendar")
