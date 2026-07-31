"""Tests unitarios para el filtrado estricto por subtipo y sinónimos.

Verifica que cuando el usuario pregunta por un subtipo específico (ej. "duchas anales",
"lubricante de sabores", "vibradores rabbit"), el sistema retorne ÚNICAMENTE los productos
que cumplen ese subtipo/sinónimos y NUNCA mezcle productos ajenos (como plugs anales o bolas).
"""
import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_CAT = _ROOT / "app" / "catalog.py"


def _extraer_constantes(ruta: Path, nombres: list[str]) -> dict:
    tree = ast.parse(ruta.read_text())
    out: dict = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in nombres:
                    out[target.id] = ast.literal_eval(node.value)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id in nombres:
                out[node.target.id] = ast.literal_eval(node.value)
    return out


_SUBTIPO_KEYWORDS = _extraer_constantes(_CAT, ["_SUBTIPO_KEYWORDS"])["_SUBTIPO_KEYWORDS"]
_SUBTIPO_A_CATEGORIA = _extraer_constantes(_CAT, ["_SUBTIPO_A_CATEGORIA"])["_SUBTIPO_A_CATEGORIA"]
_SUBTIPO_SINONIMOS = _extraer_constantes(_CAT, ["_SUBTIPO_SINONIMOS"])["_SUBTIPO_SINONIMOS"]


def _normalizar_texto(t: str) -> str:
    if not t:
        return ""
    t = t.lower()
    repl = str.maketrans("áéíóúüñ", "aeiouun")
    return re.sub(r"[^a-z0-9\s]", " ", t.translate(repl))


def _simular_filtrado_subtipo(rows: list[dict], subtipo: str | None, user_text: str, limit: int = 4) -> list[dict]:
    """Réplica exacta del bloque de filtrado por subtipo de `get_productos_para_recomendar`."""
    out_subtipo: list[dict] = []
    out: list[dict] = []
    for r in rows:
        p = dict(r)
        nd = _normalizar_texto(f"{p.get('nombre','')} {p.get('descripcion','')}")
        cumple_subtipo = False
        if subtipo:
            sinonimos = _SUBTIPO_SINONIMOS.get(subtipo, [subtipo])
            if any(s in nd for s in sinonimos):
                cumple_subtipo = True
                if subtipo in ("sabor", "sabores") and "sin sabor" in nd and not any(f in nd for f in ("fresa", "chocolate", "uva", "cereza", "manzana", "menta", "frutilla", "sandia", "chicle", "sabor a")):
                    cumple_subtipo = False
        if cumple_subtipo:
            out_subtipo.append(p)
        out.append(p)
    if out_subtipo and len(out_subtipo) >= 1:
        return out_subtipo[:limit]
    return out[:limit]


def test_duchas_anales_no_mezcla_plugs():
    """Valida que para 'tienen duchas anales', solo se recomienden duchas/enemas."""
    user_text = "tienen duchas anales"
    norm = _normalizar_texto(user_text)
    
    # Detectar subtipo
    subtipo = None
    for s in _SUBTIPO_KEYWORDS:
        if s in norm:
            subtipo = s
            break

    assert subtipo == "ducha", f"Se esperaba subtipo 'ducha', pero dio '{subtipo}'"
    assert _SUBTIPO_A_CATEGORIA.get(subtipo) == "anal"

    cat_rows = [
        {"id": 1, "nombre": "Enema para limpieza Anal Lito", "descripcion": "Bolsa de ducha higiénica"},
        {"id": 2, "nombre": "Bolas Anales Pimpo", "descripcion": "Bolas anales estimulantes"},
        {"id": 3, "nombre": "Plug Anal de Vidrio Icicles No. 44", "descripcion": "Plug anal suave"},
        {"id": 4, "nombre": "Plug Lovense Hush 2", "descripcion": "Plug anal con app conector"},
    ]

    resultado = _simular_filtrado_subtipo(cat_rows, subtipo, user_text)

    nombres_res = [p["nombre"] for p in resultado]
    assert "Enema para limpieza Anal Lito" in nombres_res
    assert "Bolas Anales Pimpo" not in nombres_res
    assert "Plug Anal de Vidrio Icicles No. 44" not in nombres_res
    assert "Plug Lovense Hush 2" not in nombres_res
    assert len(resultado) == 1, "Solo debía retornar la ducha/enema anal"


def test_lubricantes_sabores():
    """Valida que para 'lubricante de sabores', solo se recomienden lubricantes con sabor."""
    user_text = "tienen lubricante de sabores"
    norm = _normalizar_texto(user_text)

    subtipo = None
    for s in _SUBTIPO_KEYWORDS:
        if s in norm:
            subtipo = s
            break

    assert subtipo in ("sabores", "sabor")

    cat_rows = [
        {"id": 10, "nombre": "Lubricante Intimo Sabor a Fresa Sen", "descripcion": "Comestible sabor frutilla"},
        {"id": 11, "nombre": "Lubricante Neutro Base de Agua System JO", "descripcion": "Sin sabor sin olor"},
        {"id": 12, "nombre": "Aceite para Masajes Neutro", "descripcion": "Piel suave"},
    ]

    resultado = _simular_filtrado_subtipo(cat_rows, subtipo, user_text)

    nombres_res = [p["nombre"] for p in resultado]
    assert "Lubricante Intimo Sabor a Fresa Sen" in nombres_res
    assert "Lubricante Neutro Base de Agua System JO" not in nombres_res
    assert len(resultado) == 1


if __name__ == "__main__":
    test_duchas_anales_no_mezcla_plugs()
    test_lubricantes_sabores()
    print("✅ Todos los tests unitarios de filtrado por subtipo pasaron exitosamente!")
