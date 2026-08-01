"""Stubs de las dependencias de runtime que no están instaladas en el entorno.

El `.venv` del proyecto está vacío (la app corre en Docker), así que los tests
sostienen los imports con módulos falsos. `setdefault`/comprobación previa: si
las dependencias están instaladas de verdad, se usan las reales.

No se llama `test_*.py` a propósito — `tests/run.py` solo recoge esos.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def stub_drivers() -> None:
    """Drivers de red/DB que la lógica bajo prueba no ejecuta."""
    for nombre in ("asyncpg", "httpx", "openai", "qdrant_client", "redis",
                   "redis.asyncio", "tiktoken", "PIL", "PIL.Image"):
        mod = types.ModuleType(nombre)
        mod.__getattr__ = lambda _n: type("_Any", (), {"__init__": lambda *a, **k: None})  # type: ignore[attr-defined]
        sys.modules.setdefault(nombre, mod)


def stub_web() -> None:
    """fastapi/starlette. Solo tienen que sostener el import de app.main."""
    if "fastapi" in sys.modules:
        return

    class _App:
        def __init__(self, *a, **k):
            pass

        def _deco(self, *a, **k):
            return lambda fn: fn

        get = post = put = delete = middleware = _deco

        def add_middleware(self, *a, **k):
            pass

        def include_router(self, *a, **k):
            pass

    def _param(*a, **k):
        return None

    fa = types.ModuleType("fastapi")
    fa.FastAPI = _App
    fa.Request = fa.BackgroundTasks = type("_X", (), {})
    fa.HTTPException = type("HTTPException", (Exception,), {})
    fa.Header = fa.Query = fa.Form = fa.Depends = fa.Cookie = fa.Body = _param
    fa.status = types.SimpleNamespace(HTTP_302_FOUND=302, HTTP_401_UNAUTHORIZED=401)
    fa.APIRouter = _App
    resp = types.ModuleType("fastapi.responses")
    resp.PlainTextResponse = resp.HTMLResponse = resp.JSONResponse = type("_R", (), {})
    resp.RedirectResponse = type("_R", (), {})
    fa.responses = resp
    tmpl = types.ModuleType("fastapi.templating")
    tmpl.Jinja2Templates = type("_T", (), {"__init__": lambda self, *a, **k: None})
    st = types.ModuleType("starlette")
    mw = types.ModuleType("starlette.middleware")
    sess = types.ModuleType("starlette.middleware.sessions")
    sess.SessionMiddleware = type("_M", (), {})
    for nombre, mod in (("fastapi", fa), ("fastapi.responses", resp),
                        ("fastapi.templating", tmpl), ("starlette", st),
                        ("starlette.middleware", mw),
                        ("starlette.middleware.sessions", sess)):
        sys.modules[nombre] = mod


def importar_main():
    """app.main con todo lo necesario stubeado."""
    stub_drivers()
    stub_web()
    return importlib.import_module("app.main")
