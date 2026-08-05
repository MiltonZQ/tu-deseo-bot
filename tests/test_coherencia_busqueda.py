"""Lo encontrado por NOMBRE no puede contradecir lo que se clasificó.

Caso de producción 2026-08-05. El cliente escribió "Hola necesito algo para
durar mas" y recibió un Vibrador Durba Camtoyz de $250.000.

El clasificador LLM acertó: `lubricantes-y-cuidado`, que es donde viven los
retardantes. El fallo vino después:

    "necesito algo para durar mas"
      → LLM: lubricantes-y-cuidado + desensibilizante        ✅
      → _tokens_no_reconocidos → ["durar"]     (un verbo, no vocabulario del catálogo)
      → buscar_producto_especifico: 0 resultados
      → corregir_typos_contra_catalogo: "durar" → "durba"    (ratio 0.800, el mínimo)
      → buscar "durba" → Vibrador Durba Camtoyz
      → es_especifico = True → ese vibrador pasa a ser el ÚNICO candidato

Dos defectos encadenados, y los dos se cubren aquí: el corrector trataba una
palabra española normal como marca mal escrita, y un acierto por nombre podía
sustituir la clasificación sin que nada comprobara que tuviera sentido.
"""
from __future__ import annotations

import asyncio
import io
import contextlib
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.stubs import importar_main, stub_drivers  # noqa: E402

stub_drivers()

main = importar_main()

from app import catalog, db  # noqa: E402


# ── La red de coherencia ──

def test_un_producto_de_otra_categoria_no_pisa_la_clasificacion():
    """El caso exacto del reporte: un vibrador no puede satisfacer una búsqueda
    clasificada como lubricantes-y-cuidado."""
    productos = [{"id": 1, "nombre": "Vibrador Durba Camtoyz Majestic",
                  "descripcion": "", "categoria": "Vibradores"}]
    assert main._filtrar_incoherentes(productos, "lubricantes-y-cuidado") == []


def test_una_marca_de_la_categoria_correcta_si_pasa():
    """La búsqueda por nombre existe para esto y no se toca: el Lush ES un
    vibrador, así que es coherente con su categoría."""
    productos = [{"id": 2, "nombre": "Lovense Lush 3", "descripcion": "",
                  "categoria": "Vibradores"}]
    assert main._filtrar_incoherentes(productos, "vibradores") == productos


def test_sin_categoria_clasificada_no_se_filtra_nada():
    """Sin categoría no hay con qué comparar, y ahí la búsqueda por nombre es la
    única señal que hay: descartarla dejaría el turno sin candidatos, que es
    justo cuando el LLM se inventa productos."""
    productos = [{"id": 3, "nombre": "Icicles No 5", "descripcion": "",
                  "categoria": "Vidrio"}]
    assert main._filtrar_incoherentes(productos, None) == productos


def test_se_conservan_solo_los_coherentes_de_una_mezcla():
    productos = [
        {"id": 1, "nombre": "Vibrador Durba Camtoyz", "descripcion": "",
         "categoria": "Vibradores"},
        {"id": 2, "nombre": "Retardante Sen Intimo", "descripcion": "",
         "categoria": "Lubricantes"},
    ]
    quedan = main._filtrar_incoherentes(productos, "lubricantes-y-cuidado")
    assert [p["id"] for p in quedan] == [2], quedan


# ── El corrector de typos ──

def _corregir(texto: str) -> str:
    """Corre el corrector contra el catálogo real del arnés de evaluación."""
    sys.path.insert(0, str(_ROOT / "scripts" / "eval"))
    import fake_db
    original = getattr(db, "_pool", None)
    fake_db.instalar(db, _ROOT / "scripts" / "eval" / "catalogo.json")
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return asyncio.run(catalog.corregir_typos_contra_catalogo(texto))
    finally:
        db._pool = original


def test_un_verbo_comun_no_se_convierte_en_marca():
    """"durar" no es vocabulario del catálogo, así que se trataba como marca mal
    escrita, y difflib la acercaba a "durba" (Vibrador Durba) con ratio 0.800 —
    justo el mínimo que se aceptaba."""
    texto = "necesito algo para durar mas"
    corregido = _corregir(texto)
    assert "durba" not in corregido.lower(), corregido
    assert corregido == texto, corregido


def test_un_typo_de_marca_real_se_sigue_corrigiendo():
    """La razón de ser del corrector, y el límite de cuánto se puede endurecer."""
    for typo in ("lovenese", "lovence"):
        corregido = _corregir(f"quiero el vibrador {typo}")
        assert "lovense" in corregido.lower(), f"{typo!r} → {corregido!r}"


def test_el_umbral_solo_no_separaba_los_dos_casos():
    """Por qué hay dos señales y no una.

    "lovence"/"lovense" es un typo legítimo y marca 0.857; "durar"/"durba" es una
    coincidencia fortuita y marca 0.800. Un corte por similitud tendría que caer
    en esas siete milésimas. El prefijo común los separa con holgura —3 frente a
    5— porque al teclear mal se falla en medio o al final, no en el arranque.

    Si alguien vuelve a tocar el umbral, este test dice cuánto margen hay.
    """
    import difflib
    import os
    assert difflib.SequenceMatcher(None, "lovence", "lovense").ratio() < 0.86, (
        "un typo real queda por debajo de 0.86: el umbral solo no basta")
    assert len(os.path.commonprefix(["durar", "durba"])) < catalog._PREFIJO_MINIMO_TYPO
    assert len(os.path.commonprefix(["lovence", "lovense"])) >= catalog._PREFIJO_MINIMO_TYPO
