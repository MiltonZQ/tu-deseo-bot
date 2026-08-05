"""A qué producto se refiere el cliente.

Antes esto vivía repartido en cuatro sitios que no se hablaban entre sí:
`_SELECCION_NUMERICA_RE` y `_SELECCION_ORDINAL_RE` decidían si un mensaje era una
elección, `_pide_comprar_sin_numero` decidía si había que pedir un número, y la
Prioridad 0 de `pedidos.py` traducía ese número a un producto — indexando el
array acumulado de `productos_mostrados`, plano y sin orden garantizado. Un "2"
no decía de QUÉ lista, así que tras recorrer disfraces, masturbadores y juegos,
elegir "el juego 2" apuntaba a cualquier cosa.

El diseño aquí es el **mundo cerrado**: no se resuelve contra el catálogo entero
sino contra las últimas rondas mostradas — un puñado de productos que el cliente
acaba de ver. Dos consecuencias:

  - El umbral de matching por nombre puede ser mucho más bajo sin riesgo. Con 300
    referencias, "euforia" sería una apuesta; entre los 6 que tiene delante, es
    una identificación. Por eso `catalog.get_productos_en_texto` (≥70% de
    cobertura de tokens) no sirve: está calibrado para el catálogo entero, y
    contra el mensaje del cliente —que nunca trae el nombre completo— no llega.
  - Una referencia posicional vuelve a ser segura, porque el ámbito es la ronda
    y no el acumulado.

Módulo PURO: sin DB, sin red, sin LLM. Quien llama resuelve los ids de las rondas
a productos y decide qué hacer con el resultado.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from app.catalog import STOP_WORDS, _corregir_typos, _extract_search_tokens


@dataclass
class Resolucion:
    """Qué se entendió del mensaje.

    - `ids`: productos identificados sin ambigüedad. Puede traer varios, de
      rondas distintas ("el disfraz de policía y el juego de dados").
    - `ambiguos`: el cliente nombró algo que encaja con más de un producto. Se
      devuelven para poder preguntar con nombres concretos ("¿el Euforia o el
      Tenera?") en vez del genérico "dime cuál".
    - `hay_intencion_compra`: dijo que quiere comprar, con o sin objeto. Es lo
      que distingue "quiero pedir" (pídele el nombre) de "cuánto vale el envío"
      (no hay nada que resolver).
    """
    ids: list[int] = field(default_factory=list)
    ambiguos: list[dict] = field(default_factory=list)
    hay_intencion_compra: bool = False


# El cliente dice que quiere comprar. Dos formas, ambas venían de main.py:
#
#  A. Fórmula de pedido sin objeto: "quiero pedir", "cómo puedo comprar".
#  B. Verbo de compra + determinante SINGULAR: "quiero ese", "quiero el dildo".
#
# El singular separa B de una búsqueda: "quiero los vibradores" pide un listado,
# "quiero el vibrador" elige uno.
_COMPRA_SIN_OBJETO_RE = re.compile(
    r"\b(quiero|deseo|necesito|me\s+gustar[ií]a)\s+"
    r"(pedir|ordenar|comprar|llevar|hacer\s+(un\s+|el\s+)?pedido)\b|"
    r"\bc[oó]mo\s+(puedo\s+|hago\s+para\s+)?(pedir|ordenar|comprar|"
    r"hacer\s+(el|un)\s+pedido)\b",
    re.IGNORECASE,
)
_COMPRA_OBJETO_VAGO_RE = re.compile(
    r"\b(quiero|dame|deseo|me\s+llevo|ll[eé]vame|sep[aá]r[ae]me|ap[aá]rt[ae]me|"
    r"me\s+quedo\s+con|elijo|prefiero)\s+"
    r"(ese|esa|eso|este|esta|el|la|un|una|los|las)\b",
    re.IGNORECASE,
)

# Selección posicional. Anclada y con el mensaje entero consumido a propósito:
# sin eso, "tengo 2 hijos" o una dirección ("calle 2 con 45") se leían como una
# elección. El ámbito lo pone el llamante — la ÚLTIMA ronda —, no esta regex.
_POSICION_NUMERICA_RE = re.compile(
    r"^\s*(?:me\s+llevo|quiero|dame|deseo|elijo|prefiero|ll[eé]vame)?\s*"
    r"(?:el|la|los|las)?\s*(?:n[uú]mero|opci[oó]n)?\s*"
    r"(\d{1,2})\s*[\.,!]?\s*$",
    re.IGNORECASE,
)
_ORDINALES = {
    "primero": 1, "primera": 1, "primer": 1,
    "segundo": 2, "segunda": 2,
    "tercero": 3, "tercera": 3, "tercer": 3,
    "cuarto": 4, "cuarta": 4,
    "quinto": 5, "quinta": 5,
}
# Mismo anclaje que el numérico, y con artículo obligatorio: sin él, "primero
# quiero saber si es impermeable" —una duda normal— se leía como una elección y
# el bot daba la venta por cerrada.
_POSICION_ORDINAL_RE = re.compile(
    r"^\s*(?:me\s+llevo|quiero|dame|deseo|elijo|prefiero)?\s*"
    r"(?:el|la)\s+(" + "|".join(_ORDINALES) + r")\b",
    re.IGNORECASE,
)

# "el de 40 mil", "el de 25.000", "el de $120000". El precio no está en el nombre,
# así que ninguna regla de tokens lo cubre.
_PRECIO_RE = re.compile(
    r"\bde\s*\$?\s*(\d{1,3}(?:[.,]\d{3})*|\d{1,3})\s*(mil|k)?\b",
    re.IGNORECASE,
)

# Un token tiene que ser lo bastante largo para identificar. Por debajo de esto
# entran fragmentos como "ml" o "x" de los nombres comerciales, que casarían con
# cualquier mensaje que los contenga por casualidad.
_MIN_LARGO_TOKEN = 4


def _normalizar(texto: str) -> str:
    return unicodedata.normalize("NFKD", (texto or "").lower()) \
        .encode("ascii", "ignore").decode()


def _productos_de(rondas: list[dict]) -> list[dict]:
    """Todos los productos mostrados, en orden de ronda."""
    return [p for r in (rondas or []) for p in (r.get("productos") or []) if p]


def resolver(user_text: str, rondas: list[dict]) -> Resolucion:
    """Resuelve la referencia del cliente contra las rondas mostradas.

    `rondas` llega con los productos ya resueltos:
        [{"categoria": "juegos", "productos": [{"id", "nombre", "precio"}, ...]}]

    Cascada, primera que acierta gana. El orden va de la señal más específica a
    la más frágil: un nombre identifica siempre, una posición solo mientras el
    cliente esté mirando la última lista.
    """
    texto_original = user_text or ""
    intencion = bool(_COMPRA_SIN_OBJETO_RE.search(texto_original)
                     or _COMPRA_OBJETO_VAGO_RE.search(texto_original))

    productos = _productos_de(rondas)
    if not productos:
        # Sin lista viva no hay mundo cerrado contra el que resolver, y una
        # posición no significa nada. Antes esto se inferia buscando keycaps en
        # el texto del historial; ahora es un dato del estado.
        return Resolucion(hay_intencion_compra=intencion)

    # Los typos del cliente se corrigen ANTES de comparar: "dizfras" no casa con
    # ningún token de "Disfraz Policia Sexy Mujer".
    texto = _normalizar(_corregir_typos(texto_original))

    ids = _por_nombre_literal(texto, productos)
    if ids:
        return Resolucion(ids=ids, hay_intencion_compra=intencion)

    ids, ambiguos = _por_token(texto, productos)
    if ids:
        return Resolucion(ids=ids, hay_intencion_compra=intencion)

    ids = _por_precio(texto, productos)
    if ids:
        return Resolucion(ids=ids, hay_intencion_compra=intencion)

    ids = _por_posicion(texto, rondas)
    if ids:
        return Resolucion(ids=ids, hay_intencion_compra=intencion)

    return Resolucion(ambiguos=ambiguos, hay_intencion_compra=intencion)


def _por_nombre_literal(texto: str, productos: list[dict]) -> list[int]:
    """El nombre completo aparece tal cual. Es la señal más fuerte que hay."""
    ids: list[int] = []
    for p in productos:
        if _normalizar(p["nombre"]) in texto and p["id"] not in ids:
            ids.append(p["id"])
    return ids


def _por_token(texto: str, productos: list[dict]) -> tuple[list[int], list[dict]]:
    """Palabras del nombre que aparecen en el mensaje.

    La distinción que hace esto seguro: un token vale como identificación solo si
    es ÚNICO dentro del conjunto mostrado. Con las tres rondas del caso real,
    "euforia" solo lo tiene un producto y lo identifica; "juego" lo comparten dos
    y no identifica nada — esos van a `ambiguos` para poder preguntar por nombre
    en vez de elegir por el cliente y equivocarse en silencio.

    Es la misma idea que el umbral de cobertura de `get_productos_en_texto`, pero
    calibrada para un conjunto de seis productos en vez de para 300.
    """
    tokens_por_producto: dict[int, set[str]] = {}
    frecuencia: dict[str, int] = {}
    for p in productos:
        toks = {t for t in _extract_search_tokens(p["nombre"])
                if len(t) >= _MIN_LARGO_TOKEN and t not in STOP_WORDS}
        tokens_por_producto[p["id"]] = toks
        for t in toks:
            frecuencia[t] = frecuencia.get(t, 0) + 1

    palabras = set(re.findall(r"\b[a-z]{2,}\b", texto))

    ids: list[int] = []
    ambiguos: list[dict] = []
    for p in productos:
        presentes = tokens_por_producto[p["id"]] & palabras
        if not presentes:
            continue
        if any(frecuencia[t] == 1 for t in presentes):
            ids.append(p["id"])
        else:
            ambiguos.append(p)
    return ids, ambiguos


def _por_precio(texto: str, productos: list[dict]) -> list[int]:
    """"el de 40 mil", "el de 25.000"."""
    ids: list[int] = []
    for m in _PRECIO_RE.finditer(texto):
        crudo = m.group(1).replace(".", "").replace(",", "")
        try:
            valor = int(crudo)
        except ValueError:
            continue
        if m.group(2):  # "mil" / "k"
            valor *= 1000
        for p in productos:
            if int(p.get("precio") or 0) == valor and p["id"] not in ids:
                ids.append(p["id"])
    return ids


def _por_posicion(texto: str, rondas: list[dict]) -> list[int]:
    """"el 2", "el primero" — SIEMPRE contra la última ronda.

    Aquí es donde muere el bug original. El cliente cuenta sobre lo que tiene
    delante, que es la última lista que recibió; nunca sobre la suma de todo lo
    que ha visto en la conversación. Numerar de forma continua entre rondas fue
    el intento de hacer coincidir ambas cosas, y lo que hizo era imposible de
    ver para el cliente: no hay forma de saber que el disfraz que le mostraron
    hace tres mensajes era el número 7.
    """
    ultima = [p for p in ((rondas[-1].get("productos") if rondas else None) or []) if p]
    if not ultima:
        return []

    m = _POSICION_NUMERICA_RE.match(texto)
    if m:
        n = int(m.group(1))
    else:
        m = _POSICION_ORDINAL_RE.match(texto)
        if not m:
            return []
        n = _ORDINALES[m.group(1).lower()]

    return [ultima[n - 1]["id"]] if 1 <= n <= len(ultima) else []
