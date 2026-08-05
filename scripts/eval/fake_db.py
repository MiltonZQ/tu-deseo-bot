"""Un `db._pool` de mentira que consulta el catálogo real en memoria.

No hay Postgres donde correr la evaluación (ni Docker, ni servidor local, ni
`asyncpg` en el `.venv`), pero sí hace falta que el catálogo sea el de verdad.
La solución es sustituir la frontera más baja posible —el pool— para que TODO
lo que hay por encima siga siendo código real: `_consultar_restricciones`, la
escalera de relajación de `buscar_por_restricciones`, `_filtrar_por_subtipo`,
`_subtipo_ya_en_restricciones` y el ordenamiento por concordancia.

`catalog.py` levanta el pool desde una quincena de sitios, así que parchear el
pool los cubre todos de golpe.

REGLA NO NEGOCIABLE: ante un SQL que este módulo no sepa interpretar, se lanza
`SQLNoSoportado`. Nunca se devuelve lista vacía. Un falso negativo silencioso
invalidaría la evaluación entera, y encima de la forma más traicionera: el filtro
estricto por subtipo convierte "cero filas" en un escalado a asesor, así que un
SQL mal interpretado se leería como "el bot escaló" en vez de como un fallo del
arnés.
"""
from __future__ import annotations

import json
import re
from pathlib import Path


class SQLNoSoportado(RuntimeError):
    """El evaluador no reconoce esta consulta. Ver la regla de arriba."""


# ── Predicados ──
# Cada entrada es (patrón, constructor). El constructor recibe el match y
# devuelve una función (fila, params) -> bool. El orden importa solo en que los
# patrones más específicos deben ir antes que los genéricos.

def _norm_txt(valor) -> str:
    return (valor or "") if isinstance(valor, str) else ("" if valor is None else str(valor))


def _ilike_substring(campo: str, idx: int):
    def predicado(fila, params):
        aguja = _norm_txt(params[idx]).lower()
        return aguja in _norm_txt(fila.get(campo)).lower()
    return predicado


_PREDICADOS: list[tuple[str, object]] = [
    # (stock_status IS NULL OR stock_status <> 'outofstock')
    (r"^\(?stock_status IS NULL OR stock_status <> 'outofstock'\)?$",
     lambda m: lambda f, p: f.get("stock_status") in (None, "") or f["stock_status"] != "outofstock"),
    (r"^imagen_url IS NOT NULL$", lambda m: lambda f, p: f.get("imagen_url") is not None),
    (r"^imagen_url != ''$", lambda m: lambda f, p: (f.get("imagen_url") or "") != ""),
    (r"^nombre IS NOT NULL$", lambda m: lambda f, p: f.get("nombre") is not None),
    (r"^activo = TRUE$", lambda m: lambda f, p: bool(f.get("activo"))),
    (r"^vibra = TRUE$", lambda m: lambda f, p: bool(f.get("vibra"))),
    # <campo> ILIKE '%' || $n || '%'
    (r"^(\w+) ILIKE '%' \|\| \$(\d+) \|\| '%'$",
     lambda m: _ilike_substring(m.group(1), int(m.group(2)) - 1)),
    # atributos @> $n::text[]
    (r"^atributos @> \$(\d+)::text\[\]$",
     lambda m: (lambda idx: lambda f, p: all(a in (f.get("atributos") or []) for a in p[idx]))(
         int(m.group(1)) - 1)),
    # NOT (id = ANY($n::bigint[]))
    (r"^NOT \(id = ANY\(\$(\d+)::bigint\[\]\)\)$",
     lambda m: (lambda idx: lambda f, p: f.get("id") not in (p[idx] or []))(int(m.group(1)) - 1)),
    # <campo> = $n
    (r"^(\w+) = \$(\d+)$",
     lambda m: (lambda campo, idx: lambda f, p: f.get(campo) == p[idx])(
         m.group(1), int(m.group(2)) - 1)),
]


def _dividir_por(texto: str, separador: str) -> list[str]:
    """Parte por `separador` respetando paréntesis. `AND`/`OR` en may/min."""
    partes, nivel, actual = [], 0, []
    tokens = re.split(rf"(\(|\)|\s+{separador}\s+)", texto, flags=re.I)
    for token in tokens:
        if token is None:
            continue
        if token == "(":
            nivel += 1
            actual.append(token)
        elif token == ")":
            nivel -= 1
            actual.append(token)
        elif nivel == 0 and re.fullmatch(rf"\s+{separador}\s+", token, flags=re.I):
            partes.append("".join(actual))
            actual = []
        else:
            actual.append(token)
    partes.append("".join(actual))
    return [p.strip() for p in partes if p.strip()]


def _compilar_condicion(texto: str):
    """Convierte un WHERE en una función (fila, params) -> bool."""
    texto = texto.strip()

    # Primero los predicados literales: el de stock_status ya viene con sus
    # paréntesis y desenvolverlo lo rompería.
    reconocido = _reconocer(texto)
    if reconocido is not None:
        return reconocido

    for separador, combinar in (("AND", all), ("OR", any)):
        partes = _dividir_por(texto, separador)
        if len(partes) > 1:
            hijos = [_compilar_condicion(p) for p in partes]
            return lambda f, p, _c=combinar, _h=hijos: _c(h(f, p) for h in _h)

    if texto.startswith("(") and texto.endswith(")"):
        return _compilar_condicion(texto[1:-1])

    raise SQLNoSoportado(f"predicado no reconocido: {texto!r}")


def _reconocer(texto: str):
    for patron, constructor in _PREDICADOS:
        m = re.match(patron, texto.strip(), flags=re.I)
        if m:
            return constructor(m)
    return None


# ── ORDER BY ──

def _clave_orden(expresion: str):
    """Devuelve (funcion_clave, descendente) para un término del ORDER BY."""
    expresion = expresion.strip()
    descendente = bool(re.search(r"\bDESC\b", expresion, re.I))
    cuerpo = re.sub(r"\b(ASC|DESC)\b", "", expresion, flags=re.I).strip()

    m = re.fullmatch(r"LENGTH\((\w+)\)", cuerpo, re.I)
    if m:
        campo = m.group(1)
        return (lambda f: len(_norm_txt(f.get(campo)))), descendente
    m = re.fullmatch(r"\((\w+) IS NULL\)", cuerpo, re.I)
    if m:
        campo = m.group(1)
        return (lambda f: 1 if f.get(campo) is None else 0), descendente
    m = re.fullmatch(r"\((\w+) IS TRUE\)", cuerpo, re.I)
    if m:
        campo = m.group(1)
        return (lambda f: 1 if f.get(campo) else 0), descendente
    if re.fullmatch(r"\w+", cuerpo):
        campo = cuerpo
        return (lambda f: _norm_txt(f.get(campo))), descendente
    raise SQLNoSoportado(f"término de ORDER BY no reconocido: {expresion!r}")


# ── Consulta ──

_RE_CONSULTA = re.compile(
    r"^\s*SELECT\s+(?P<cols>.+?)\s+FROM\s+productos"
    r"(?:\s+WHERE\s+(?P<where>.+?))?"
    r"(?:\s+ORDER\s+BY\s+(?P<order>.+?))?"
    r"(?:\s+LIMIT\s+(?P<limit>\$?\d+))?\s*$",
    re.I | re.S)


class _Consulta:
    def __init__(self, sql: str):
        plano = " ".join(sql.split())
        m = _RE_CONSULTA.match(plano)
        if not m:
            raise SQLNoSoportado(f"forma de consulta no reconocida: {plano[:160]!r}")
        self.columnas = [c.strip() for c in m.group("cols").split(",")]
        self.condicion = _compilar_condicion(m.group("where")) if m.group("where") else None
        self.orden = ([_clave_orden(t) for t in m.group("order").split(",")]
                      if m.group("order") else [])
        self.limite = m.group("limit")

    def ejecutar(self, filas: list[dict], params: tuple) -> list[dict]:
        salida = [f for f in filas if not self.condicion or self.condicion(f, params)]
        # Se ordena de atrás hacia delante para respetar la precedencia del SQL
        # apoyándose en que sorted() es estable.
        for clave, descendente in reversed(self.orden):
            salida.sort(key=clave, reverse=descendente)
        if self.limite:
            tope = (int(params[int(self.limite[1:]) - 1]) if self.limite.startswith("$")
                    else int(self.limite))
            salida = salida[:tope]
        if self.columnas == ["*"]:
            return [dict(f) for f in salida]
        return [{c: f.get(c) for c in self.columnas} for f in salida]


# ── Pool falso ──

class _Conexion:
    def __init__(self, filas: list[dict], registro: list[str]):
        self._filas = filas
        self._registro = registro

    async def fetch(self, sql: str, *params):
        self._registro.append(" ".join(sql.split()))
        return _Consulta(sql).ejecutar(self._filas, params)

    async def fetchrow(self, sql: str, *params):
        filas = await self.fetch(sql, *params)
        return filas[0] if filas else None

    async def fetchval(self, sql: str, *params):
        fila = await self.fetchrow(sql, *params)
        return next(iter(fila.values())) if fila else None

    async def execute(self, sql: str, *params):
        raise SQLNoSoportado("la evaluación es de solo lectura; no debe escribir")


class _Adquirir:
    def __init__(self, conexion):
        self._conexion = conexion

    async def __aenter__(self):
        return self._conexion

    async def __aexit__(self, *exc):
        return False


class PoolFalso:
    def __init__(self, filas: list[dict]):
        self.filas = filas
        self.consultas: list[str] = []

    def acquire(self):
        return _Adquirir(_Conexion(self.filas, self.consultas))


def cargar_catalogo(ruta: str | Path | None = None) -> list[dict]:
    """El snapshot que produce `snapshot_catalogo.py`."""
    ruta = Path(ruta) if ruta else Path(__file__).parent / "catalogo.json"
    if not ruta.exists():
        raise SystemExit(
            f"No existe {ruta}. Genera el snapshot primero:\n"
            "    python3 scripts/eval/snapshot_catalogo.py")
    filas = json.loads(ruta.read_text())
    for fila in filas:
        # `galeria_urls` se guarda como JSON en texto, igual que en la tabla.
        if isinstance(fila.get("galeria_urls"), list):
            fila["galeria_urls"] = json.dumps(fila["galeria_urls"])
    return filas


def instalar(modulo_db, ruta_catalogo=None) -> PoolFalso:
    """Deja `db._pool` apuntando al catálogo real en memoria."""
    pool = PoolFalso(cargar_catalogo(ruta_catalogo))
    modulo_db._pool = pool
    return pool
