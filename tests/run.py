#!/usr/bin/env python
"""Corre toda la suite sin pytest.

El `.venv` del proyecto no tiene pip (el que trae `ensurepip` está roto para
3.11), así que no hay pytest y la suite se venía corriendo con one-liners
pegados a mano. Esto hace lo mismo pero de forma reproducible.

Uso:
    .venv/bin/python tests/run.py            # toda la suite
    .venv/bin/python tests/run.py facetas    # solo los módulos que coincidan
    .venv/bin/python tests/run.py -v         # sin silenciar los logs

Sale con código 1 si falla algo, para poder encadenarlo.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import logging
import sys
import traceback
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def main(argv: list[str]) -> int:
    verboso = "-v" in argv
    filtros = [a for a in argv if not a.startswith("-")]
    if not verboso:
        logging.disable(logging.CRITICAL)

    modulos = sorted(p.stem for p in Path(__file__).parent.glob("test_*.py"))
    if filtros:
        modulos = [m for m in modulos if any(f in m for f in filtros)]
    if not modulos:
        print("Ningún módulo de tests coincide con", filtros)
        return 1

    total = 0
    fallos: list[tuple[str, str]] = []
    for nombre in modulos:
        try:
            with contextlib.redirect_stdout(io.StringIO() if not verboso else sys.stdout):
                mod = importlib.import_module(f"tests.{nombre}")
        except Exception:
            fallos.append((f"{nombre} (import)", traceback.format_exc(limit=3)))
            continue
        for attr in sorted(vars(mod)):
            fn = getattr(mod, attr)
            if not attr.startswith("test_") or not callable(fn):
                continue
            total += 1
            try:
                with contextlib.redirect_stdout(io.StringIO() if not verboso else sys.stdout):
                    fn()
            except Exception:
                fallos.append((f"{nombre}::{attr}", traceback.format_exc(limit=3)))

    print(f"\n{total - len(fallos)} pasan / {len(fallos)} fallan  ({total} tests)")
    for nombre, tb in fallos:
        print(f"\n── FALLA {nombre} " + "─" * max(0, 50 - len(nombre)))
        print("   " + tb.strip().replace("\n", "\n   "))
    return 1 if fallos else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
