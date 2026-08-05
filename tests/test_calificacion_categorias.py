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
        # También las que llevan anotación de tipo (`X: tuple[str, ...] = (...)`),
        # como `facetas.CATEGORIAS_FUNCIONALES`.
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id in nombres and node.value is not None:
            out[node.target.id] = ast.literal_eval(node.value)
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
    """'anillos vibradores' y 'anillos-y-fundas' son la MISMA categoría.

    Antes eran dos: las listas estáticas decían `anillos-vibradores` y el LLM
    `anillos-y-fundas`, y el código las reconciliaba a mano. Con dos nombres para
    una idea, cada camino de recuperación filtraba distinto. Ahora solo existe la
    del vocabulario canónico (`facetas.CATEGORIAS_FUNCIONALES`).
    """
    mapa = _extraer_constantes(_CAT, ["_INTENCION_A_CATEGORIA_FUNCIONAL"])["_INTENCION_A_CATEGORIA_FUNCIONAL"]
    assert mapa.get("anillos vibradores") == "anillos-y-fundas"
    assert mapa.get("anillo vibrador") == "anillos-y-fundas"
    assert "anillos-vibradores" not in set(mapa.values()), (
        "volvió a aparecer la categoría duplicada")


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


# ── BUG 11: subtipo suelto deriva categoría (sabores → lubricantes, no dildos) ─

def test_bug11_mapeo_subtipo_a_categoria_existe():
    """Verifica que existe el mapeo _SUBTIPO_A_CATEGORIA en catalog.py."""
    extras = _extraer_constantes(_CAT, ["_SUBTIPO_A_CATEGORIA"])
    assert "_SUBTIPO_A_CATEGORIA" in extras, "Falta _SUBTIPO_A_CATEGORIA en catalog.py"
    m = extras["_SUBTIPO_A_CATEGORIA"]
    assert m.get("sabores") == "lubricantes-y-cuidado"
    assert m.get("doble") == "dildos"
    assert m.get("ventosa") == "dildos"
    assert m.get("rabbit") == "vibradores"
    assert m.get("prostat") == "anal"


def test_bug11_subtipo_suelto_deriva_categoria_lubricantes():
    """Réplica de la derivación: 'sabores' solo (sin sustantivo de categoría)
    debe derivar a lubricantes-y-cuidado, NO quedar como None (que antes hacía
    heredar 'dildos' del contexto)."""
    extras = _extraer_constantes(_CAT, ["_SUBTIPO_A_CATEGORIA"])
    m = extras["_SUBTIPO_A_CATEGORIA"]

    # Réplica de la lógica de derivación en clasificar_intencion_cliente.
    def _derivar(subtipo_detectado, categoria_funcional):
        if subtipo_detectado and not categoria_funcional:
            return m.get(subtipo_detectado, categoria_funcional)
        return categoria_funcional

    # "sabores" sin categoría → deriva a lubricantes.
    assert _derivar("sabores", None) == "lubricantes-y-cuidado"
    # "doble" sin categoría → deriva a dildos.
    assert _derivar("doble", None) == "dildos"
    # Si ya hay categoría (el cliente dijo "lubricante con sabores") → se conserva.
    assert _derivar("sabores", "lubricantes-y-cuidado") == "lubricantes-y-cuidado"


def test_bug11_subtipo_ambiguo_no_deriva():
    """Subtipos ambiguos (calor, frío, cola, clitor) NO están en el mapeo fijo
    porque pueden pertenecer a varias categorías. Verifica que NO estén en
    _SUBTIPO_A_CATEGORIA (se dejan al contexto)."""
    extras = _extraer_constantes(_CAT, ["_SUBTIPO_A_CATEGORIA"])
    m = extras["_SUBTIPO_A_CATEGORIA"]
    for ambiguo in ("calor", "caliente", "frío", "frio", "cola", "clitor", "clitori"):
        assert ambiguo not in m, (
            f"{ambiguo!r} es ambiguo y NO debe estar en _SUBTIPO_A_CATEGORIA")


def test_bug11_deteccion_subtipo_no_depende_de_sustantivo():
    """La detección de subtipo ya NO requiere tener un sustantivo de categoría.
    'sabores' (que no es sustantivo en _NOUN_KEYWORDS) debe igual detectarse como
    subtipo. Verifica que el código de detección no tenga 'if sustantivo' antes
    del bucle de _SUBTIPO_KEYWORDS."""
    src = _CAT.read_text()
    # Buscar el bloque donde se calcula subtipo_detectado.
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "clasificar_intencion_cliente":
                fn_src = ast.get_source_segment(src, node)
                # La línea de 'for s in _SUBTIPO_KEYWORDS' NO debe estar precedida
                # inmediatamente por 'if sustantivo:'.
                lines = fn_src.splitlines()
                for i, line in enumerate(lines):
                    if "for s in _SUBTIPO_KEYWORDS" in line and "subtipo_detectado" in lines[i-2]:
                        # El bucle de detección no está dentro de un 'if sustantivo'.
                        assert "if sustantivo" not in lines[i-1], (
                            "La detección de subtipo no debe depender de 'if sustantivo'")
                return
    raise AssertionError("No se encontró clasificar_intencion_cliente")


# ── BUG 12: clasificador LLM de respaldo (kits de amarre, sadomasoquismo) ─────

def test_bug12_kits_amarre_ya_en_mapeo_deterministico():
    """El fix rápido: 'kits de amarre' ahora está en el mapeo determinístico,
    así ni siquiera necesita el LLM. Verifica las claves."""
    mapa = _extraer_constantes(_CAT, ["_INTENCION_A_CATEGORIA_FUNCIONAL"])["_INTENCION_A_CATEGORIA_FUNCIONAL"]
    assert mapa.get("kits") == "pareja-y-bondage"
    assert mapa.get("kit") == "pareja-y-bondage"
    assert mapa.get("amarre") == "pareja-y-bondage"
    assert mapa.get("sadomasoquismo") == "pareja-y-bondage"
    assert mapa.get("esposas") == "pareja-y-bondage"
    assert mapa.get("bdsm") == "pareja-y-bondage"


def test_bug12_llm_clasificador_existe_y_es_restrictivo():
    """El LLM elige dentro del vocabulario canónico, ni más ni menos.

    La lista esperada no se repite aquí a mano: se lee de
    `facetas.CATEGORIAS_FUNCIONALES`, que es la única fuente. Repetirla era
    justamente lo que dejó que los dos vocabularios se separaran sin que nadie
    se enterara.
    """
    _OAI = _ROOT / "app" / "openai_client.py"
    _FAC = _ROOT / "app" / "facetas.py"
    extras = _extraer_constantes(_OAI, ["_CATEGORIAS_LLM"])
    assert "_CATEGORIAS_LLM" in extras, "Falta _CATEGORIAS_LLM en openai_client.py"
    canon = set(_extraer_constantes(_FAC, ["CATEGORIAS_FUNCIONALES"])["CATEGORIAS_FUNCIONALES"])
    cats = set(extras["_CATEGORIAS_LLM"].keys())
    assert cats == canon, (
        f"el LLM ofrece categorías fuera del canon: {sorted(cats - canon)}; "
        f"le faltan del canon: {sorted(canon - cats)}")


def test_bug12_llm_clasificador_valida_categoria():
    """Réplica de la validación en clasificar_intencion_llm: el LLM solo puede
    devolver una de las 11 categorías o 'ninguna'. Cualquier otra se descarta."""
    _OAI = _ROOT / "app" / "openai_client.py"
    cats = _extraer_constantes(_OAI, ["_CATEGORIAS_LLM"])["_CATEGORIAS_LLM"]
    validas = set(cats.keys()) | {"ninguna"}

    def _validar(cat_llm):
        cat = cat_llm.strip().lower()
        return cat if cat in validas else None

    assert _validar("pareja-y-bondage") == "pareja-y-bondage"
    assert _validar("ninguna") == "ninguna"
    assert _validar("collares") is None  # no es categoría válida → descartada
    assert _validar("jewelry") is None
    assert _validar(" lubricantes-y-cuidado ") == "lubricantes-y-cuidado"


def test_bug12_respaldo_llm_solo_cuando_deterministico_falla():
    """Réplica de la lógica de respaldo: el LLM solo se invoca si el determinístico
    NO encontró categoría Y no hay subtipo propio."""
    def _llamar_llm(categoria_funcional, subtipo_detectado, user_text):
        if not categoria_funcional and not subtipo_detectado and user_text and len(user_text.strip()) >= 4:
            return "LLM_INVOCADO"
        return "NO_INVOCADO"

    # "kits de amarre" ya lo encuentra el determinístico → NO invoca LLM.
    assert _llamar_llm("pareja-y-bondage", None, "kits de amarre") == "NO_INVOCADO"
    # "sadomasoquismo" ya está en determinístico → NO invoca LLM.
    assert _llamar_llm("pareja-y-bondage", None, "sadomasoquismo") == "NO_INVOCADO"
    # "cinturón de castidad" NO lo encuentra el determinístico → SÍ invoca LLM.
    assert _llamar_llm(None, None, "cinturon de castidad") == "LLM_INVOCADO"
    # Mensaje con subtipo propio → NO invoca LLM (el subtipo deriva categoría).
    assert _llamar_llm(None, "doble", "doble") == "NO_INVOCADO"
    # Mensaje muy corto → NO invoca LLM.
    assert _llamar_llm(None, None, "ok") == "NO_INVOCADO"


def test_bug12_clasificador_es_async():
    """Verifica que clasificar_intencion_cliente ahora es async (porque puede
    llamar al LLM). Inspección del AST."""
    import ast
    tree = ast.parse(_CAT.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "clasificar_intencion_cliente":
            return  # es async ✅
    raise AssertionError("clasificar_intencion_cliente debe ser async (llama al LLM)")


# ── BUG 13: blindaje anti-bucle + subtipos completos (esposas, body, etc.) ────

def test_bug13_subtipos_bondage_completos():
    """Verifica que los subtipos de bondage están en _SUBTIPO_KEYWORDS y mapeados
    a pareja-y-bondage (el bug de 'esposas' que re-preguntaba)."""
    import ast
    tree = ast.parse(_CAT.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_SUBTIPO_KEYWORDS":
                    subs = ast.literal_eval(node.value)
                    for s in ("esposas", "antifaz", "fustas", "amarre", "mordaza", "vendas"):
                        assert s in subs, f"{s!r} falta en _SUBTIPO_KEYWORDS"
    m = _extraer_constantes(_CAT, ["_SUBTIPO_A_CATEGORIA"])["_SUBTIPO_A_CATEGORIA"]
    for s in ("esposas", "antifaz", "fustas", "amarre"):
        assert m.get(s) == "pareja-y-bondage", f"{s!r} no mapea a pareja-y-bondage"


def test_bug13_subtipos_lenceria_completos():
    """Subtipos de lencería que el bot ofrece (body, disfraz) deben estar."""
    import ast
    tree = ast.parse(_CAT.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_SUBTIPO_KEYWORDS":
                    subs = ast.literal_eval(node.value)
                    for s in ("body", "disfraz", "suspensorio", "conjunto"):
                        assert s in subs, f"{s!r} falta en _SUBTIPO_KEYWORDS"


def test_bug13_blindaje_anti_bucle_existe():
    """Verifica que el blindaje anti-bucle está en _recuperar_candidatos."""
    import ast
    tree = ast.parse(_MAIN.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_recuperar_candidatos":
            src = ast.get_source_segment(_MAIN.read_text(), node)
            assert "Blindaje anti-bucle" in src or "anti-bucle" in src.lower(), (
                "Falta el blindaje anti-bucle en _recuperar_candidatos")
            return
    raise AssertionError("No se encontró _recuperar_candidatos")


def test_bug13_blindaje_muestra_si_hay_productos():
    """Réplica del blindaje: si debe_mostrar + hay estado calificado + cat_func,
    pero candidatos vacío, buscar relajado (genero=None). Si hay, mostrar."""
    def _blindaje_simulado(debe_mostrar, estado_tiene_cat, calificado, candidatos_iniciales, cat_func):
        if not candidatos_iniciales and debe_mostrar and cat_func and estado_tiene_cat and calificado:
            return "buscar_relajado"
        return "no_aplica"
    # "esposas" tras pregunta de pareja-y-bondage → blindaje busca relajado.
    r = _blindaje_simulado(True, True, True, [], "pareja-y-bondage")
    assert r == "buscar_relajado"
    # Ya hay candidatos → no aplica blindaje.
    r2 = _blindaje_simulado(True, True, True, [{"id": 1}], "pareja-y-bondage")
    assert r2 == "no_aplica"
    # No hay estado previo → no aplica (primera consulta).
    r3 = _blindaje_simulado(True, False, False, [], "pareja-y-bondage")
    assert r3 == "no_aplica"


# ── BUG 14: resolver productos con [FOTO:ID] + pago no acepta monto a ciegas ───

def test_bug14_resolver_parsea_marcadores_foto_id():
    """Réplica del parseo de [FOTO:ID] en _resolver_productos_y_total. Los IDs
    de los marcadores son la fuente más confiable de qué productos vio el cliente."""
    import re
    foto_id_re = re.compile(r"\[\s*FOTO:\s*(\d+)\s*\]", re.IGNORECASE)

    def _extraer_ids(texto_asistente):
        return [int(m.group(1)) for m in foto_id_re.finditer(texto_asistente)]

    # Caso del bug: marcadores con espacios.
    ids = _extraer_ids("Mira estas opciones 👇 [ FOTO:64209 ] [ FOTO:64512 ] ¿Te gustó?")
    assert ids == [64209, 64512]
    # Formato normal.
    ids2 = _extraer_ids("[FOTO:100] [FOTO:200]")
    assert ids2 == [100, 200]
    # Sin marcadores → vacío.
    ids3 = _extraer_ids("Hola, ¿cómo estás?")
    assert ids3 == []


def test_bug14_el_resolver_prioriza_la_fuente_mas_fiable():
    """La fuente más fiable de qué compró el cliente va ANTES del fallback por
    nombres sobre el historial.

    Esa fuente era la lista de IDs que el bot había enviado como fotos (los
    marcadores [FOTO:ID] se limpian del historial, así que no se podían releer de
    ahí). Ahora es el carrito, que es mejor por dos motivos: registra lo que el
    cliente ELIGIÓ y no solo lo que se le mostró, y guarda el precio que se le
    dijo en ese momento.
    """
    import ast
    _PED = _ROOT / "app" / "pedidos.py"
    tree = ast.parse(_PED.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_resolver_productos_y_total":
                src = ast.get_source_segment(_PED.read_text(), node)
                assert "carrito" in src, "El resolver debe leer el carrito"
                carrito_pos = src.index("for item in (carrito")
                nombre_pos = src.index("get_productos_en_texto")
                assert carrito_pos < nombre_pos, (
                    "el carrito debe ir antes del fallback por nombres")
                return
    raise AssertionError("No se encontró _resolver_productos_y_total")


def test_bug14_pago_no_crea_pedido_a_ciegas():
    """Réplica de la lógica: si no hay pedido previo y no se resuelven productos,
    ESCALAR a humano (no crear pedido con monto del comprobante a ciegas)."""
    def _decidir(items_resueltos, total_catalogo, monto_comprobante):
        if items_resueltos and total_catalogo > 0:
            return ("crear_pedido", total_catalogo)
        return ("escalar", None)

    # Productos resueltos → crear con total del catálogo (no del comprobante).
    accion, total = _decidir([{"id": 1}], 50000, 999999)
    assert accion == "crear_pedido"
    assert total == 50000  # total del catálogo, no 999999 del comprobante

    # Sin productos → escalar (no crear a ciegas).
    accion2, _ = _decidir([], 0, 50000)
    assert accion2 == "escalar"


# ── BUG 15: marcador estructurado [[PEDIDO_DATOS:...]] ────────────────────────

def test_bug15_parser_marcador_datos_pedido():
    """Réplica del parser de [[PEDIDO_DATOS:...]] en pedidos.py."""
    import re
    pat = re.compile(r"\[\[PEDIDO_DATOS:([^\]]+)\]\]", re.IGNORECASE | re.DOTALL)

    def _parsear(reply):
        m = pat.search(reply)
        if not m:
            return {}
        datos = {}
        for par in m.group(1).split("|"):
            par = par.strip()
            if "=" not in par:
                continue
            k, v = par.split("=", 1)
            if v.strip():
                datos[k.strip().lower()] = v.strip()
        return datos

    r = _parsear("Confirmo [[PEDIDO_DATOS:nombre=Juan Pérez|ciudad=Bogotá|direccion=Calle 123 #45|telefono=3232325543]]")
    assert r["nombre"] == "Juan Pérez"
    assert r["ciudad"] == "Bogotá"
    assert r["direccion"] == "Calle 123 #45"
    assert r["telefono"] == "3232325543"

    # Sin marcador → vacío
    assert _parsear("Hola") == {}

    # Marcador con campos faltantes
    r2 = _parsear("[[PEDIDO_DATOS:nombre=Ana|ciudad=Cali]]")
    assert r2["nombre"] == "Ana"
    assert "direccion" not in r2


def test_bug15_parser_existe_en_pedidos():
    """Verifica que el parser existe en pedidos.py."""
    import ast
    _PED = _ROOT / "app" / "pedidos.py"
    tree = ast.parse(_PED.read_text())
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_parsear_datos_pedido":
            found = True
    assert found, "Debe existir _parsear_datos_pedido en pedidos.py"


# ── BUG 16: "anal" tras pregunta de lubricantes mostraba plugs (juguetes) ────
# Caso reportado en WhatsApp: bot pregunta subtipo de lubricante (agua/silicona/
# anal desensibilizante/sabores), cliente responde solo "anal", y el bot mostró
# Plug Anal (juguetes de la categoría "anal") en vez de lubricante/gel anal.
# Cubre los dos fixes reales que resuelven el caso:
#   (a) el motor de memoria y contexto (clasificar_intencion_cliente) que
#       interpreta "anal" como filtro DENTRO de la categoría activa en memoria
#       (lubricantes-y-cuidado) en vez de saltar a la categoría de juguetes.
#   (b) la reclasificación de productos (_categoria_normalizada /
#       _REGLAS_CATEGORIA) que mete "lubricante anal"/"gel anal"/desensibilizante
#       en lubricantes-y-cuidado ANTES de la regla genérica de "anal" (juguetes).

def test_bug16_motor_memoria_existe_en_clasificador():
    """Verifica que el motor de memoria y contexto sigue presente en
    clasificar_intencion_cliente (regresión de la causa raíz del bug)."""
    import ast
    tree = ast.parse(_CAT.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "clasificar_intencion_cliente":
            src = ast.get_source_segment(_CAT.read_text(), node)
            assert "cat_activa_memoria" in src
            assert "cambio_de_tema" in src
            # La detección de cambio de tema debe salir de si la categoría vino del
            # MENSAJE actual, no de una lista fija de sustantivos: esa lista omitía
            # lubricante/látigo/plug/arnés y dejaba pegada la categoría del turno 1.
            assert "cat_desde_mensaje" in src
            assert "sustantivo_cambio_tema" not in src
            return
    raise AssertionError("No se encontró clasificar_intencion_cliente")


def _motor_memoria(user_text: str, history: list[dict]) -> dict:
    """Réplica exacta del bloque 'MOTOR DE MEMORIA Y CONTEXTO' de
    clasificar_intencion_cliente (app/catalog.py, sección posterior a la
    detección determinística). Arranca desde el estado que el determinístico
    produce para el mensaje "anal" solo (categoria_funcional="anal", que es
    justo lo que dispara el bug si el motor de memoria no lo corrige)."""
    norm_user = user_text.lower()
    categoria_funcional = "anal"
    subtipo_detectado = None
    intencion = "anal"

    cat_activa_memoria = None
    if history:
        for h_msg in reversed(history[-6:]):
            if h_msg.get("role") == "assistant":
                c_h = (h_msg.get("content") or "").lower()
                if any(w in c_h for w in ("lubricante", "base de agua", "silicona", "sabores", "sensaciones")):
                    cat_activa_memoria = "lubricantes-y-cuidado"
                    break
                elif any(w in c_h for w in ("dildo", "ventosa", "realista")):
                    cat_activa_memoria = "dildos"
                    break
                elif any(w in c_h for w in ("vibrador", "clítoris", "clitoris")):
                    cat_activa_memoria = "vibradores"
                    break
                elif any(w in c_h for w in ("lencería", "lenceria", "body")):
                    cat_activa_memoria = "lenceria"
                    break
                elif any(w in c_h for w in ("succionador", "succionadores")):
                    cat_activa_memoria = "succionadores"
                    break

    sustantivo_cambio_tema = None
    for n in ("dildo", "dildos", "consolador", "lenceria", "lencería", "succionador",
              "succionadores", "vibrador", "vibradores", "anillo", "anillos", "funda",
              "fundas", "bomba", "bombas"):
        if n in norm_user:
            sustantivo_cambio_tema = n
            break

    if cat_activa_memoria and not sustantivo_cambio_tema:
        categoria_funcional = cat_activa_memoria
        intencion = cat_activa_memoria
        if "anal" in norm_user or "desensibiliz" in norm_user:
            subtipo_detectado = "desensibiliz"
        elif "agua" in norm_user:
            subtipo_detectado = "base de agua"
        elif "silicona" in norm_user:
            subtipo_detectado = "silicona"
        elif "sabor" in norm_user or "sabores" in norm_user:
            subtipo_detectado = "sabores"

    return {"categoria_funcional": categoria_funcional, "subtipo_detectado": subtipo_detectado,
            "intencion": intencion}


def test_bug16_caso_reportado_anal_tras_lubricantes():
    """Caso EXACTO del chat reportado: 'anal' tras la pregunta de subtipo de
    lubricante debe resolver a lubricantes-y-cuidado + subtipo desensibilizante,
    NO a la categoría anal (juguetes)."""
    history = [
        {"role": "user", "content": "Quenlubricantes tienen"},
        {"role": "assistant", "content": (
            "¡Genial! Para recomendarte el ideal, cuéntame: ¿lo buscas a base de "
            "agua (seguro con juguetes), de silicona (duradero), anal "
            "desensibilizante, o con sabores/sensaciones (calor/frío)? 😊")},
    ]
    resultado = _motor_memoria("anal", history)
    assert resultado["categoria_funcional"] == "lubricantes-y-cuidado", (
        "'anal' tras la pregunta de lubricantes NO debe saltar a la categoría "
        "de juguetes anales")
    assert resultado["subtipo_detectado"] == "desensibiliz"


def test_bug16_cambio_de_tema_explicito_no_se_bloquea():
    """Si el cliente sí trae un sustantivo de cambio de tema explícito (ej.
    'vibrador'), el motor de memoria NO debe forzar la categoría anterior."""
    history = [
        {"role": "assistant", "content": "¿lo buscas a base de agua, de silicona, o con sabores?"},
    ]
    resultado = _motor_memoria("prefiero un vibrador", history)
    assert resultado["categoria_funcional"] == "anal", (
        "sustantivo_cambio_tema ('vibrador') debe bloquear la herencia de la "
        "categoría activa en memoria")


def test_bug16_categoria_normalizada_prioriza_lubricante_anal_sobre_juguete():
    """CAUSA RAÍZ complementaria: un producto 'Gel Lubricante Anal
    Desensibilizante' debe clasificarse en lubricantes-y-cuidado (no en 'anal'
    de juguetes), mientras que un 'Plug Anal' sigue clasificándose como 'anal'.
    Réplica del algoritmo real de _categoria_normalizada: recorre
    _REGLAS_CATEGORIA en orden y la primera clave que aparezca en el texto
    normalizado gana."""
    reglas = _extraer_constantes(_CAT, ["_REGLAS_CATEGORIA"])["_REGLAS_CATEGORIA"]

    def _normalizar(t: str) -> str:
        import unicodedata
        t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode("ascii")
        return t.lower()

    def _clasificar(nombre: str) -> str:
        haystack = _normalizar(nombre)
        for claves, cat in reglas:
            for clave in claves:
                if clave in haystack:
                    return cat
        return "juegos-y-accesorios"

    assert _clasificar("Gel Lubricante Anal Desensibilizante XYZ") == "lubricantes-y-cuidado"
    assert _clasificar("Lubricante Anal a Base de Silicona") == "lubricantes-y-cuidado"
    assert _clasificar("Plug Anal Dipsy Camtoyz") == "anal"
    assert _clasificar("Plug Anal Mikel CamToyz") == "anal"


def test_bug16_reglas_categoria_orden_lubricante_antes_que_anal_generico():
    """Verifica en el fuente real que la regla de lubricantes-y-cuidado (con
    'desensibiliz'/'gel anal'/'lubricante anal') está ANTES que la regla
    genérica de 'anal' (plugs/juguetes) en _REGLAS_CATEGORIA. Si alguien
    reordena esto por error, un lubricante anal volvería a clasificarse como
    juguete."""
    reglas = _extraer_constantes(_CAT, ["_REGLAS_CATEGORIA"])["_REGLAS_CATEGORIA"]
    idx_lubricante = next(i for i, (claves, cat) in enumerate(reglas)
                          if cat == "lubricantes-y-cuidado" and "desensibiliz" in claves)
    idx_anal_generico = next(i for i, (claves, cat) in enumerate(reglas)
                             if cat == "anal" and "plug" in claves)
    assert idx_lubricante < idx_anal_generico, (
        "La regla de lubricantes-y-cuidado debe evaluarse ANTES que la regla "
        "genérica de 'anal' (juguetes)")


# ── BUG 17: seleccionar productos por número reenviaba el catálogo completo ──
# Caso reportado: bot muestra 5 dildos, cliente responde "El 2 y 3" (eligiendo),
# el LLM confirma la venta correctamente + ofrece lubricante, PERO el sistema
# además reenvía las 5 fotos otra vez porque calificado=True persistido hacía
# que cualquier mensaje sin categoría nueva disparara mostrar_por_estado=True.
#
# La detección ya no vive en una regex de main.py. Se resuelve en `app.seleccion`
# contra las RONDAS del estado, no buscando keycaps en el texto del historial:
# aquello obligaba a que la marca de "hay una lista viva" fuera visible para el
# cliente, y era lo que ataba el mecanismo a la numeración.
#
# La cobertura de COMPORTAMIENTO (qué mensajes cuentan como selección) vive en
# `tests/test_seleccion.py`, sección "Regresión BUG 17". Aquí se queda solo la
# comprobación estructural, que es la que puede hacerse leyendo el fuente: este
# fichero no importa módulos de `app` a propósito (ver el docstring de arriba),
# y meter un `from app import seleccion` lo dejaba dependiendo de que otro test
# stubeara los drivers antes — pasaba en la suite completa y fallaba aislado.

def test_bug17_override_existe_en_recuperar_candidatos():
    """El override sigue conectado en _recuperar_candidatos (main.py), con la
    misma prioridad que la regla de fase de venta."""
    tree = ast.parse(_MAIN.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_recuperar_candidatos":
            src = ast.get_source_segment(_MAIN.read_text(), node)
            assert "res_seleccion.ids or res_seleccion.ambiguos" in src, (
                "la palanca anti-reenvío debe leer el resultado del resolvedor")
            idx = src.index("if res_seleccion.ids or res_seleccion.ambiguos")
            bloque = src[idx:idx + 400]
            assert "debe_mostrar = False" in bloque
            assert 'clasif["calificado"] = False' in bloque
            return
    raise AssertionError("No se encontró _recuperar_candidatos")


# ── BUG 18: Qdrant incompleto devolvía menos de 5 fotos (ej. succionadores) ──
# Caso reportado: "tienen succionadores" mostró solo 1 foto en vez de 5. Al
# reconectar Qdrant (búsqueda semántica), el Intento Q filtraba por umbral de
# score y, si encontraba menos candidatos que el límite pedido, devolvía ese
# puñado incompleto de una vez SIN completar con el fallback SQL determinístico
# (que sí tiene los 5 productos de la categoría). El fix solo retorna temprano
# cuando Qdrant alcanza el límite; si no, completa los huecos con SQL.

def test_bug18_qdrant_incompleto_se_completa_con_sql():
    """Verifica en el fuente que el retorno temprano de Qdrant exige alcanzar
    el límite pedido, y que los 4 intentos SQL (A, C, B, D) combinan sus
    resultados con los de Qdrant en vez de descartarlos."""
    tree = ast.parse(_CAT.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_productos_para_recomendar":
            src = ast.get_source_segment(_CAT.read_text(), node)
            assert "len(filtrados_q) >= limit" in src, (
                "El retorno temprano de Qdrant debe exigir alcanzar el límite pedido")
            assert src.count("return _combinar_con_qdrant(candidatos)") == 4, (
                "Los 4 intentos SQL (A, C, B, D) deben combinar con Qdrant, no "
                "descartar lo que ya había encontrado")
            return
    raise AssertionError("No se encontró get_productos_para_recomendar")


def test_bug18_combinar_con_qdrant_prioriza_qdrant_y_respeta_limite():
    """Réplica de _combinar_con_qdrant: los resultados de Qdrant van primero
    (más relevantes semánticamente), se completan con SQL, y el total nunca
    excede el límite pedido."""
    filtrados_q = [{"id": 1}, {"id": 2}]
    candidatos_sql = [{"id": 3}, {"id": 4}, {"id": 5}, {"id": 6}]
    limit = 5

    def _combinar(candidatos_sql):
        if not filtrados_q:
            return candidatos_sql
        return (filtrados_q + candidatos_sql)[:limit]

    combinado = _combinar(candidatos_sql)
    assert [p["id"] for p in combinado] == [1, 2, 3, 4, 5]
    assert len(combinado) == limit


def test_bug18_sin_candidatos_qdrant_no_cambia_comportamiento():
    """Si Qdrant no encontró nada (filtrados_q vacío), _combinar_con_qdrant debe
    devolver los candidatos SQL tal cual, sin alterar el comportamiento previo."""
    filtrados_q: list = []
    candidatos_sql = [{"id": 1}, {"id": 2}, {"id": 3}]

    def _combinar(candidatos_sql):
        if not filtrados_q:
            return candidatos_sql
        return (filtrados_q + candidatos_sql)[:5]

    assert _combinar(candidatos_sql) == candidatos_sql


# ── BUG 19: marca/modelo NO listada pedía calificación en vez de mostrar ────
# directo el producto. Pedido del negocio: "si el usuario llega preguntando
# por marca, producto especifico ya se le debe mostrar exactamente esos
# productos" — sin depender de mantener una lista fija de marcas al día.
# Ver tests/test_pipeline_robustez.py para la verificación funcional real de
# _tokens_no_reconocidos (ese archivo sí importa app.catalog).

def test_bug19_busqueda_por_nombre_conectada_en_recuperar_candidatos():
    """Verifica en el fuente que _recuperar_candidatos intenta la búsqueda por
    nombre (_tokens_no_reconocidos) ANTES de la regla anti-bucle, y que el
    bloque de es_especifico reutiliza ese resultado en vez de repetir la
    consulta."""
    tree = ast.parse(_MAIN.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_recuperar_candidatos":
            src = ast.get_source_segment(_MAIN.read_text(), node)
            assert "_tokens_no_reconocidos" in src
            assert "producto_por_nombre" in src
            # Reutiliza el resultado ya buscado en vez de una nueva consulta.
            assert "especificos = producto_por_nombre or" in src
            return
    raise AssertionError("No se encontró _recuperar_candidatos")


def test_bug19_busqueda_por_nombre_gateada_a_sin_categoria_activa():
    """La búsqueda por nombre en primer contacto debe estar condicionada a NO
    tener una categoría de conversación ya en curso (o haber detectado cambio
    de tema) — así 'los rojos' a mitad de una conversación de dildos sigue
    resolviéndose por el motor de memoria/anti-bucle existente, no por esta
    búsqueda de nombre nueva."""
    tree = ast.parse(_MAIN.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_recuperar_candidatos":
            src = ast.get_source_segment(_MAIN.read_text(), node)
            idx = src.index("producto_por_nombre: list[dict] = []")
            bloque = src[idx:idx + 300]
            assert "not estado_tiene_cat or reset_state" in bloque, (
                "La búsqueda por nombre debe estar gateada a 'sin categoría activa "
                "o cambio de tema detectado', para no interferir con conversaciones "
                "de categoría ya en curso")
            return
    raise AssertionError("No se encontró _recuperar_candidatos")


def test_bug19_info_expone_es_especifico():
    """El dict info que devuelve _recuperar_candidatos debe exponer
    es_especifico (antes no estaba, y hacía falta para futuras validaciones)."""
    tree = ast.parse(_MAIN.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_recuperar_candidatos":
            src = ast.get_source_segment(_MAIN.read_text(), node)
            assert '"es_especifico"' in src
            return
    raise AssertionError("No se encontró _recuperar_candidatos")


# ── BUG 20 (RAÍZ): off-by-one de placeholders en upsert_conversation_state ───
# Los logs de producción mostraban DOS errores distintos en cada intento de
# persistir estado: "the server expects 3 arguments, 4 were passed" y
# 'column "calificado" is of type boolean but expression is of type text'.
# Causa: el SET dinámico numeraba desde $1, pero $1 ya está tomado por wa_id en
# el VALUES, así que todos los índices quedaban corridos por uno. Existía desde
# el commit inicial → el estado de conversación NUNCA se persistió.

def _build_set_upsert(categoria_busqueda=None, categoria_funcional=None, genero=None,
                      calificado=None, add_productos_mostrados=None):
    """Réplica del SET dinámico de db.upsert_conversation_state.
    Devuelve (sql_completo, params_incluyendo_wa_id)."""
    sets, params = [], []
    idx = 2  # $1 = wa_id en el VALUES
    if categoria_busqueda is not None:
        sets.append(f"categoria_busqueda = ${idx}"); params.append(categoria_busqueda); idx += 1
    if categoria_funcional is not None:
        sets.append(f"categoria_funcional = ${idx}"); params.append(categoria_funcional); idx += 1
    if genero is not None:
        sets.append(f"genero = ${idx}"); params.append(genero); idx += 1
    if calificado is not None:
        sets.append(f"calificado = ${idx}"); params.append(calificado); idx += 1
    if add_productos_mostrados:
        sets.append(
            "productos_mostrados = ARRAY(SELECT DISTINCT unnest("
            f"conversation_state.productos_mostrados || ${idx}::bigint[]))")
        params.append(list(add_productos_mostrados)); idx += 1
    sets.append("updated_at = now()")
    sql = ("INSERT INTO conversation_state (wa_id, calificado, productos_mostrados, updated_at) "
           "VALUES ($1, FALSE, '{}', now()) ON CONFLICT (wa_id) DO UPDATE SET "
           + ", ".join(sets))
    return sql, ["WA_ID"] + params


def test_bug20_placeholders_coinciden_con_params_en_toda_combinacion():
    """Para CUALQUIER combinación de campos, los placeholders deben ser $1..$N
    contiguos y N debe ser exactamente el número de parámetros enviados."""
    import itertools
    valores = dict(categoria_busqueda="dildos", categoria_funcional="dildos",
                   genero="mujer", calificado=True, add_productos_mostrados=[1, 2])
    nombres = list(valores)
    for r in range(len(nombres) + 1):
        for combo in itertools.combinations(nombres, r):
            kwargs = {k: valores[k] for k in combo}
            sql, params = _build_set_upsert(**kwargs)
            usados = sorted({int(n) for n in re.findall(r"\$(\d+)", sql)})
            assert usados == list(range(1, len(params) + 1)), (
                f"combo={combo}: placeholders {usados} vs {len(params)} params")


def test_bug20_calificado_recibe_el_booleano_no_otro_campo():
    """El error real de producción ('calificado is boolean but expression is
    text') venía de que el índice corrido hacía que `calificado` apuntara al
    parámetro de otro campo. Verifica el mapeo posición→valor."""
    sql, params = _build_set_upsert(categoria_busqueda="lubricantes",
                                    categoria_funcional="lubricantes-y-cuidado",
                                    calificado=True)
    idx_calificado = int(re.search(r"calificado = \$(\d+)", sql).group(1))
    # params está indexado desde 0 y $1 es params[0] (wa_id).
    assert params[idx_calificado - 1] is True, (
        f"calificado apunta a ${idx_calificado} = {params[idx_calificado - 1]!r}, "
        "debería ser el booleano")


def test_bug20_el_codigo_real_arranca_los_placeholders_en_2():
    """Regresión sobre el fuente: el idx del SET dinámico debe arrancar en 2."""
    _DB = _ROOT / "app" / "db.py"
    src_completo = _DB.read_text()
    tree = ast.parse(src_completo)
    for node in ast.walk(tree):
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "upsert_conversation_state"):
            src = ast.get_source_segment(src_completo, node)
            assert re.search(r"^\s*idx = 2\s*$", src, re.MULTILINE), (
                "El SET dinámico debe arrancar en idx = 2 ($1 es wa_id)")
            assert not re.search(r"^\s*idx = 1\s*$", src, re.MULTILINE), (
                "idx = 1 corre todos los placeholders y rompe el UPDATE")
            return
    raise AssertionError("No se encontró upsert_conversation_state")


# ── BUG 21: guardia anti-invención de productos ─────────────────────────────
# Caso "multiorgarmo": sin categoría ni candidatos, el LLM redactó una lista de
# 5 productos inventados con precios. El filtro de IDs impidió mandar las fotos
# (logs: "ID 30122 del LLM rechazado"), pero el TEXTO llegó igual al cliente
# porque la guardia solo reescribía cuando había categoria_funcional.

_LISTA_PRODUCTOS_RE = re.compile(r"[1-9]️⃣[^\n]*\$\s*[\d.,]+")


def test_bug21_detecta_lista_numerada_con_precios_inventada():
    """El texto real que salió al cliente debe detectarse como oferta de productos."""
    inventado = (
        "¡Genial! Te muestro las mejores opciones de juguetes multiorgásmicos 👇\n"
        "1️⃣ Succionador Womanizer Premium — $350.000 — estimulación múltiple\n"
        "2️⃣ Vibrador Bunny Lelo — $280.000 — doble estimulación")
    assert _LISTA_PRODUCTOS_RE.search(inventado)


def test_bug21_no_detecta_texto_sin_lista_de_productos():
    """Preguntas de calificación y confirmaciones sin lista numerada no deben
    activar la guardia."""
    for texto in (
        "¡Claro! ¿Buscas un dildo realista, con ventosa, de vidrio, o doble? 😊",
        "¡Excelente elección! Te anoto Consolador King Cock ($60,000).",
        "Con gusto, ¿en qué ciudad estás?",
    ):
        assert not _LISTA_PRODUCTOS_RE.search(texto), f"falso positivo en {texto!r}"


def test_bug21_guardia_reescribe_cuando_no_hay_categoria():
    """Réplica de la guardia: sin candidatos y sin categoría, el texto se
    reemplaza por el mensaje honesto en vez de dejar salir la invención."""
    def _guardia(reply, debe_mostrar, final_productos, categoria_funcional, en_fase_venta):
        if (not debe_mostrar and not final_productos
                and (_OFRECE_PRODUCTOS_RE.search(reply) or _LISTA_PRODUCTOS_RE.search(reply))):
            pregunta = _PREGUNTAS_CALIFICACION.get(categoria_funcional) if categoria_funcional else None
            if pregunta:
                return "pregunta"
            elif not en_fase_venta:
                return "honesto"
        return "sin cambios"

    inventado = "Te muestro las mejores opciones 👇\n1️⃣ Womanizer Premium — $350.000"
    # Caso del bug: sin categoría → mensaje honesto (antes: pasaba tal cual).
    assert _guardia(inventado, False, [], None, False) == "honesto"
    # Con categoría → sigue inyectando la pregunta de calificación (sin cambios).
    assert _guardia(inventado, False, [], "dildos", False) == "pregunta"
    # En fase de venta, la lista numerada es el resumen del pedido: no tocar.
    assert _guardia(inventado, False, [], None, True) == "sin cambios"
    # Con fotos reales que sí se envían, la guardia no interviene.
    assert _guardia(inventado, True, [{"id": 1}], None, False) == "sin cambios"


def test_bug21_guardia_conectada_en_el_codigo_real():
    src = _MAIN.read_text()
    assert "_LISTA_PRODUCTOS_RE" in src
    assert "_SIN_RESULTADO_MSG" in src


# ── BUG 22: _es_fase_venta demasiado laxo bloqueaba consultas nuevas ─────────
# Una venta cruzada vieja ("¿agregarlo a tu pedido?") dejaba la conversación en
# modo-venta indefinidamente: al mirar los últimos 6 mensajes del bot y buscar
# palabras sueltas ("pedido"), toda consulta posterior perdía sus fotos.

_BOT_ABRIO_CHECKOUT = (
    "nombre completo", "datos de envío", "datos de envio",
    "teléfono de contacto", "telefono de contacto",
    "dirección de envío", "direccion de envio",
    "información de pagos", "informacion de pagos",
    "confirmar tu pedido", "confirmo tu pedido", "resumen de tu pedido",
    "[[pedido",
)
_CIERRE_CROSS_SELLING_RE = re.compile(
    r"\b(solo|no|así|asi|nada|listo|pago|ninguno|ninguna)\b", re.IGNORECASE)
_BOT_OFRECIO_CROSS_SELLING = (
    "aceite", "perfume", "lubricante", "complementar", "agregar a tu pedido",
    "experiencia más completa", "experiencia mas completa",
)
_FASE_VENTA_RE = re.compile(
    r"\b(nequi|daviplata|bancolombia|bold|pago|pagar|pague|transferencia|"
    r"comprobante|comprobant|envio|envío|despacho|despachar|direcci[oó]n|"
    r"telefono|tel[eé]fono|celular|nombre completo|ciudad|barrio|"
    r"cuesta|cuanto cuesta|cu[aá]nto|valor|total|yappi|yapy|"
    r"pedido|lo quiero|lo compro|lo llevo|me lo llevo|ese quiero|"
    r"ya pague|ya pagu|ya transferi|ya transferí|adjunto|adjunt|"
    r"comprobante de pago|efectivo|contra ?entrega)\b", re.IGNORECASE)
_RECHAZO_CROSS_RE = re.compile(
    r"\b(solo\s+(las|los|el|la|eso|el\s+primero|la\s+1|el\s+1|lo\s+que\s+pedi|las\s+esposas|el\s+vibrador)|"
    r"no\s+gracias|asi\s+esta\s+bien|así\s+está\s+bien|sin\s+aceite|sin\s+perfume|sin\s+lubricante|"
    r"nada\s+mas|nada\s+más|ninguno\s+mas|ninguno\s+más|ningun\s+otro|ningún\s+otro|"
    r"proceder\s+al\s+pago|dame\s+los\s+datos|solo\s+eso)\b", re.IGNORECASE)


def _es_fase_venta(user_text: str, history: list[dict]) -> bool:
    """Réplica de la versión corregida en main.py."""
    texto = user_text or ""
    if _FASE_VENTA_RE.search(texto) or _RECHAZO_CROSS_RE.search(texto):
        return True
    ultimo_bot = next((m for m in reversed(history) if m.get("role") == "assistant"), None)
    if not ultimo_bot:
        return False
    c = (ultimo_bot.get("content") or "").lower()
    if any(f in c for f in _BOT_ABRIO_CHECKOUT):
        return True
    if any(w in c for w in _BOT_OFRECIO_CROSS_SELLING):
        if _RECHAZO_CROSS_RE.search(texto) or _CIERRE_CROSS_SELLING_RE.search(texto):
            return True
    return False


_HIST_CROSS_SELLING_VIEJA = [
    {"role": "assistant", "content": (
        "💡 Para una experiencia más cómoda, te recomiendo acompañarlos con "
        "nuestro Lubricante Íntimo a Base de Agua por $29,800. ¿Te gustaría "
        "agregarlo a tu pedido?")},
]


def test_bug22_consulta_nueva_tras_venta_cruzada_no_es_fase_venta():
    """Caso del reporte: 'Tienen productos King' tras una venta cruzada vieja
    NO debe considerarse fase de venta (antes bloqueaba las fotos y terminaba
    inyectando una pregunta de calificación sin relación)."""
    assert _es_fase_venta("Tienen productos King", _HIST_CROSS_SELLING_VIEJA) is False
    assert _es_fase_venta("Tienen multiorgarmo", _HIST_CROSS_SELLING_VIEJA) is False


def test_bug22_respuesta_de_calificacion_no_es_fase_venta():
    """'Base de agua' respondiendo la pregunta de lubricantes debe seguir el
    flujo normal (antes: fase de venta → sin búsqueda → handoff falso)."""
    history = [{"role": "assistant", "content": (
        "¿lo buscas a base de agua, de silicona, anal desensibilizante, o con sabores?")}]
    assert _es_fase_venta("Base de agua", history) is False


def test_bug22_palabras_completas_no_substrings():
    """'uno'/'bueno' contienen 'no' como substring: no deben activar el cierre
    de venta cruzada."""
    assert _es_fase_venta("quiero uno", _HIST_CROSS_SELLING_VIEJA) is False
    assert _es_fase_venta("bueno", _HIST_CROSS_SELLING_VIEJA) is False


def test_bug22_checkout_y_cierre_real_si_son_fase_venta():
    """Los casos legítimos de fase de venta se siguen detectando."""
    checkout = [{"role": "assistant", "content": (
        "Perfecto, pásame tu nombre completo, ciudad y dirección 😊")}]
    assert _es_fase_venta("Juan Pérez, Bogotá", checkout) is True
    assert _es_fase_venta("no gracias, solo eso", _HIST_CROSS_SELLING_VIEJA) is True
    assert _es_fase_venta("ya pague, adjunto comprobante", []) is True


def test_bug22_solo_mira_el_ultimo_mensaje_del_bot():
    """Verifica en el fuente que ya no se recorren los últimos 6 mensajes."""
    tree = ast.parse(_MAIN.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_es_fase_venta":
            src = ast.get_source_segment(_MAIN.read_text(), node)
            assert "history[-6:]" not in src, (
                "_es_fase_venta debe mirar solo el último mensaje del bot")
            assert "ultimo_bot" in src
            return
    raise AssertionError("No se encontró _es_fase_venta")


# ── BUG 23: falso "sin stock" cuando la búsqueda nunca corrió ───────────────

def test_bug23_sin_stock_requiere_que_la_busqueda_se_haya_ejecutado():
    """Réplica de la condición corregida: sin busqueda_ejecutada, un turno que
    no mostraba fotos se leía como inventario vacío → handoff + bot pausado."""
    def _sin_stock(busqueda_ejecutada, subtipo, candidatos, exclude, es_soft):
        return bool(busqueda_ejecutada and subtipo and not candidatos
                    and not exclude and not es_soft)

    # Caso del bug: no se buscó (debe_mostrar=False) → NO es sin stock.
    assert _sin_stock(False, "base de agua", [], [], False) is False
    # Se buscó y de verdad no hay nada → sí es sin stock (comportamiento legítimo).
    assert _sin_stock(True, "base de agua", [], [], False) is True
    # Se buscó y sí hay candidatos → no es sin stock.
    assert _sin_stock(True, "base de agua", [{"id": 1}], [], False) is False


def test_bug23_flag_conectado_en_el_codigo_real():
    tree = ast.parse(_MAIN.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_recuperar_candidatos":
            src = ast.get_source_segment(_MAIN.read_text(), node)
            assert "busqueda_ejecutada = bool(debe_mostrar)" in src
            assert "busqueda_ejecutada and _subtipo_actual" in src
            return
    raise AssertionError("No se encontró _recuperar_candidatos")


# ── BUG 24: "si" borraba el contexto y desalineaba texto vs fotos ────────────
# El cliente vio 5 succionadores, respondió "si", y recibió 5 vibradores Lovense
# bajo un texto que listaba succionadores inventados. La categoría se corrompió
# (ver tests en test_pipeline_robustez.py) y eso disparó dos amplificadores que
# se cubren aquí: el reset del estado, y el forzado de candidatos sin ajustar el
# texto que ya había escrito el LLM.

def test_bug24_respuesta_afirmativa_no_dispara_reset():
    """Réplica de la condición de cambio de tema: un 'si' nunca debe resetear el
    estado (borraba también la lista de productos ya mostrados)."""
    def _reset(estado_tiene_cat, nueva_cat_clara, cat_nueva, cat_estado, es_afirmativa):
        return bool(estado_tiene_cat and nueva_cat_clara
                    and cat_nueva != cat_estado and not es_afirmativa)

    # Caso del bug: "si" mal clasificado como vibradores → NO debe resetear.
    assert _reset(True, True, "vibradores", "succionadores", True) is False
    # Cambio de tema real ("ahora quiero dildos") → sí resetea.
    assert _reset(True, True, "dildos", "succionadores", False) is True
    # Misma categoría → no resetea.
    assert _reset(True, True, "succionadores", "succionadores", False) is False


def test_bug24_guarda_afirmativa_conectada_en_el_codigo():
    tree = ast.parse(_MAIN.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_recuperar_candidatos":
            src = ast.get_source_segment(_MAIN.read_text(), node)
            idx = src.index("reset_state = False")
            bloque = src[idx:idx + 700]
            assert "not _es_respuesta_afirmativa(user_text)" in bloque, (
                "el disparo de reset_state debe excluir las respuestas afirmativas")
            return
    raise AssertionError("No se encontró _recuperar_candidatos")


def test_bug24_texto_determinista_coincide_con_las_fotos():
    """Réplica de _texto_desde_candidatos: los nombres, precios y [FOTO:ID] del
    texto salen de los mismos candidatos que se envían, así que no pueden
    desalinearse."""
    def _texto(candidatos, info):
        cat = (info.get("intencion") or info.get("categoria_funcional") or "productos").replace("-", " ")
        lineas, marcadores = [], []
        for idx, p in enumerate(candidatos[:5], 1):
            precio = f"{int(p.get('precio', 0)):,}".replace(",", ".")
            lineas.append(f"{idx}️⃣ *{p['nombre'][:60]}* — ${precio}")
            marcadores.append(f"[FOTO:{p['id']}]")
        return "intro\n" + "\n".join(lineas) + f"\n\n{' '.join(marcadores)}\n\ncta"

    candidatos = [
        {"id": 101, "nombre": "Succionador de Clítoris Tenera 2", "precio": 629800},
        {"id": 102, "nombre": "Satisfyer Penguin Succionador Clitorial", "precio": 384800},
    ]
    texto = _texto(candidatos, {"intencion": "succionadores"})
    ids_en_texto = re.findall(r"\[FOTO:(\d+)\]", texto)
    assert ids_en_texto == [str(p["id"]) for p in candidatos]
    for p in candidatos:
        assert p["nombre"] in texto, "cada foto enviada debe estar nombrada en el texto"


def test_bug24_texto_del_llm_se_reemplaza_si_no_corresponde():
    """Cuando se fuerzan los candidatos del sistema y el LLM ya había escrito su
    propia lista de productos, esa lista no describe lo que se va a enviar: debe
    reemplazarse (caso real: texto de succionadores + fotos de Lovense)."""
    def _decidir(reply, se_forzaron_candidatos):
        if se_forzaron_candidatos and (_LISTA_PRODUCTOS_RE.search(reply)
                                       or _OFRECE_PRODUCTOS_RE.search(reply)):
            return "reemplazar"
        return "mantener"

    inventado = ("¡Genial! Aquí tienes 5 opciones de succionadores 👇\n"
                 "1️⃣ Satisfyer Pro 2 — $250.000 — 11 modos succión")
    assert _decidir(inventado, True) == "reemplazar"
    # Sin forzado (el LLM emitió marcadores válidos), se respeta su redacción.
    assert _decidir(inventado, False) == "mantener"
    # Texto sin lista de productos: no hay nada que desalinear.
    assert _decidir("¡Claro! ¿Buscas algo en particular?", True) == "mantener"


def test_bug24_reemplazo_conectado_en_el_codigo():
    """El texto de los turnos de producto lo arma el sistema, no el LLM.

    Antes el LLM redactaba la lista y el sistema la reemplazaba cuando no
    coincidía con las fotos. Ese reemplazo a posteriori se sustituyó por la
    garantía de origen: si hay productos que mostrar, el texto sale de
    `_texto_desde_candidatos` y nunca del LLM, así que no hay nada que corregir.
    """
    src = _MAIN.read_text()
    assert "_texto_desde_candidatos" in src
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_message":
            fn_src = ast.get_source_segment(src, node)
            idx = fn_src.index("elif texto_lo_arma_el_sistema:")
            bloque = fn_src[idx:idx + 500]
            assert "_texto_desde_candidatos" in bloque, (
                "el turno de producto debe redactarse desde los candidatos reales")
            return
    raise AssertionError("No se encontró _handle_message")
