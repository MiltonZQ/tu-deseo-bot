"""Clasificación de productos por FACETAS independientes.

Sustituye a la lista ordenada de palabras que asignaba una única "categoría
funcional" por producto. Aquella aproximación mezclaba en un mismo cajón cosas
que el cliente jamás confundiría: la categoría `anal` contenía enemas de
limpieza, plugs sin vibración, plugs vibradores, arneses strap-on y masajeadores
de próstata. Cuando alguien pedía "vibrador anal" el sistema no tenía forma de
distinguirlos y respondía con enemas.

Aquí cada producto se describe con atributos INDEPENDIENTES:

    Enema Lito              tipo=enema      zona=anal      vibra=False
    Plug Basix              tipo=plug       zona=anal      vibra=False
    Plug Lovense Hush 2     tipo=plug       zona=anal      vibra=True
    Masajeador Edge 2       tipo=vibrador   zona=anal      vibra=True

Así "vibrador anal" es una intersección consultable (`vibra=True AND zona=anal`)
en vez de un cajón.

Dos principios que vienen de bugs reales y no deben perderse:

  1. EL NOMBRE MANDA. Las reglas se evalúan primero contra el nombre; solo si el
     nombre no da señal se mira la descripción. Antes se concatenaba todo y una
     palabra suelta de la descripción se llevaba el producto ("Compatible con
     lubricantes a base de agua" convertía un succionador en lubricante).

  2. LÍMITES DE PALABRA. Las claves deben empezar palabra. Comparar con `in`
     pelado hacía coincidir "funda" dentro de "proFUNDA" y "pene" dentro de
     "PENEtración", con lo que vibradores de mujer terminaban marcados como
     productos masculinos.

Este módulo es puro: no conoce la base de datos ni HTTP. Se usa desde el
backfill (`scripts/clasificar_catalogo.py`) y desde el sync de WooCommerce.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

# ── Vocabulario cerrado ──
# Cambiar estos valores obliga a revisar las consultas y el panel, así que la
# lista es deliberadamente corta y estable.

TIPOS = (
    "vibrador", "succionador", "plug", "bolas", "dildo", "anillo", "funda",
    "masturbador", "bomba", "arnes", "enema", "lubricante", "cosmetica",
    "lenceria", "bondage", "juego", "accesorio",
)
ZONAS = ("clitoris", "vaginal", "anal", "pene", "pezones", "cuerpo", "ninguna")
CONTROLES = ("app", "remoto", "manual", "ninguno")
GENEROS = ("hombre", "mujer", "pareja", "unisex")


@dataclass
class Facetas:
    tipo: str | None = None
    zona: str = "ninguna"
    vibra: bool = False
    control: str = "ninguno"
    genero_uso: str = "unisex"
    atributos: list[str] = field(default_factory=list)
    confianza: float = 0.0

    def as_dict(self) -> dict:
        return {
            "tipo": self.tipo,
            "zona": self.zona,
            "vibra": self.vibra,
            "control": self.control,
            "genero_uso": self.genero_uso,
            "atributos": list(self.atributos),
        }


def normalizar(texto: str | None) -> str:
    """ASCII sin acentos ni mayúsculas."""
    if not texto:
        return ""
    return unicodedata.normalize("NFKD", texto.lower()).encode("ascii", "ignore").decode()


# Claves que deben coincidir como PALABRA COMPLETA (admitiendo plural). El resto
# se comparan con límite de palabra solo a la izquierda, para conservar las
# raíces deliberadas: "lubricant" debe pillar lubricante/lubricantes, "prostat"
# próstata, "succion" succionador.
# "pene" está aquí porque arranca "penetracion"/"Penetrix", que el límite
# izquierdo no filtra.
_CLAVES_PALABRA_COMPLETA = frozenset({"pene", "anal", "cola", "bala", "sado", "kit"})


@lru_cache(maxsize=2048)
def _patron(clave: str) -> "re.Pattern[str]":
    patron = r"\b" + re.escape(clave)
    if clave in _CLAVES_PALABRA_COMPLETA:
        patron += r"(?:es|s)?\b"
    return re.compile(patron)


def contiene(clave: str, texto: str) -> bool:
    """True si la clave aparece empezando palabra (no dentro de otra)."""
    return bool(_patron(clave).search(texto))


def _alguna(claves, texto: str) -> bool:
    return any(contiene(c, texto) for c in claves)


# ── TIPO: qué ES el producto ──
# Orden de arriba a abajo; gana la primera. Lo más específico primero, para que
# un "enema" no lo capture la regla genérica de "anal" ni un "masajeador
# prostático" caiga en accesorio.
_REGLAS_TIPO: tuple[tuple[tuple[str, ...], str], ...] = (
    (("enema", "ducha anal", "duchas anales", "irrigador", "pera anal", "canula"), "enema"),
    (("bolas anales", "bolas anal", "bola anal", "ben wa", "bolas chinas"), "bolas"),
    (("arnes", "strap on", "strap-on", "strapless", "harness", "panty con dildo"), "arnes"),
    (("succionador", "succion", "air pulse", "ondas de presion", "satisfyer pro"), "succionador"),
    (("bomba", "bombas"), "bomba"),
    (("masturbador", "huevo masturb", "vagina artificial", "masturbacion"), "masturbador"),
    (("plug", "dilatador"), "plug"),
    # Próstata: es un vibrador con forma específica, no un plug pasivo.
    (("masajeador prostatico", "masajeador de prostata", "estimulador prostatico",
      "sonda universal de prostata", "prostat"), "vibrador"),
    (("anillo", "cockring", "flexring"), "anillo"),
    (("funda", "extensor", "engrosador"), "funda"),
    (("dildo", "consolador"), "dildo"),
    (("vibrador", "vibradora", "vibrating", "bala vibradora", "huevo vibrador",
      "hitachi", "varita", "wand", "rabbit"), "vibrador"),
    (("lubricant", "gel intimo", "gel anal"), "lubricante"),
    (("limpiador", "toallitas", "estimulant", "retardant", "desensibiliz",
      "potenciador", "potencializador", "afrodisiaco", "elixir", "booster",
      "serum", "crema", "aceite", "spray", "vela", "electrizante"), "cosmetica"),
    (("suspensorio", "suspensor", "pechera", "body", "baby doll", "babydoll",
      "conjunto", "lenceria", "disfraz", "disfra", "pezonera", "liguero",
      "pantuflas", "tanga", "encaje"), "lenceria"),
    (("bondage", "bdsm", "esposas", "antifaz", "amarre", "fusta", "latigo",
      "mordaza", "venda", "cepo", "collar", "sadomaso", "sado", "tapa ojos"), "bondage"),
    (("juego de mesa", "jenga", "cartas", "dado", "dados", "ruleta"), "juego"),
)

# ── ZONA: dónde se usa ──
# La FORMA del juguete implica la zona aunque el texto no la nombre: una varita
# tipo Hitachi o una bala son externas (clítoris); un rabbit o un doble son de
# inserción (vaginal). Sin estas reglas esos productos quedaban en zona
# "ninguna" y no aparecían en ninguna búsqueda por zona.
_REGLAS_ZONA: tuple[tuple[tuple[str, ...], str], ...] = (
    (("clitoris", "clitorial", "clitor", "succionador", "air pulse", "ondas de presion",
      "hitachi", "varita", "wand", "bala", "huevo vibrador"), "clitoris"),
    (("anal", "prostat", "recto", "esfinter", "plug", "enema", "ducha anal"), "anal"),
    (("pene", "peneano", "prepucio", "glande", "escroto", "testicul"), "pene"),
    (("punto g", "vaginal", "vagina", "dildo", "consolador", "penetracion",
      "rabbit", "doble estimulacion", "doble"), "vaginal"),
    (("pezon", "pezonera", "pecho", "senos"), "pezones"),
    (("masaje corporal", "cuerpo", "corporal", "piel"), "cuerpo"),
)

# El tipo determina la zona cuando el texto no la nombra. Un dildo es vaginal
# salvo que diga anal; un anillo es de pene; un succionador de clítoris.
_ZONA_POR_TIPO = {
    "succionador": "clitoris",
    "plug": "anal",
    "bolas": "anal",
    "enema": "anal",
    "anillo": "pene",
    "funda": "pene",
    "bomba": "pene",
    "masturbador": "pene",
    "dildo": "vaginal",
    "arnes": "vaginal",
}

# Tipos que por naturaleza no se aplican a una zona concreta.
_TIPOS_SIN_ZONA = {"lubricante", "cosmetica", "lenceria", "bondage", "juego", "accesorio"}

_CLAVES_VIBRA = ("vibrador", "vibradora", "vibra", "vibracion", "vibrante",
                 "vibrating", "pulsante", "modos de vibracion")
_CLAVES_APP = ("con app", "control por app", "app control", "aplicacion",
               "lovense remote", "control remoto app", "conectividad")
_CLAVES_REMOTO = ("control remoto", "inalambrico", "mando a distancia", "remoto")

# ── ATRIBUTOS: subtipos y características que el cliente pide por nombre ──
_ATRIBUTOS = {
    "realista": ("realista", "ultrarrealista", "ultrarealista", "hiperrealista", "textura piel"),
    "ventosa": ("ventosa",),
    "vidrio": ("vidrio", "cristal"),
    "doble": ("doble", "doble estimulacion", "doble penetracion"),
    "sabor": ("sabor", "sabores", "comestible"),
    "agua": ("base de agua", "base agua", "h2o"),
    "silicona": ("silicona",),
    "desensibilizante": ("desensibiliz", "anestesico", "relajante anal"),
    "calor": ("caliente", "sensacion caliente", "calor"),
    "frio": ("frio", "menta", "efecto frio"),
    "principiante": ("principiante", "primera vez", "iniciacion", "pequeño", "pequeno"),
    "recargable": ("recargable", "usb"),
    "impermeable": ("impermeable", "sumergible", "waterproof"),
}

# ── GÉNERO/USO ──
_REGLAS_GENERO: tuple[tuple[tuple[str, ...], str], ...] = (
    (("para pareja", "parejas", "en pareja", "we vibe", "we-vibe", "chorus",
      "doble estimulacion", "strap on", "strap-on", "strapless"), "pareja"),
    (("clitoris", "clitorial", "punto g", "vaginal", "rabbit", "para ella",
      "femenino", "baby doll", "babydoll", "pezonera"), "mujer"),
    (("pene", "peneano", "prostat", "masturbador", "suspensorio", "suspensor",
      "para el hombre", "masculino", "escroto", "glande"), "hombre"),
)


def clasificar_por_reglas(nombre: str, descripcion: str | None = "",
                          cat_origen: str | None = "") -> Facetas:
    """Clasifica un producto con reglas deterministas.

    Devuelve `tipo=None` y `confianza=0` cuando no hay señal suficiente: en ese
    caso decide el LLM. Es preferible admitir que no se sabe a inventar un tipo,
    porque un tipo equivocado se propaga a todas las búsquedas.
    """
    n = normalizar(nombre)
    d = normalizar(descripcion)
    origen = normalizar(cat_origen)
    completo = f"{n} {d} {origen}".strip()

    # TIPO — el nombre manda; la descripción es el respaldo.
    tipo, confianza = None, 0.0
    for claves, valor in _REGLAS_TIPO:
        if _alguna(claves, n):
            tipo, confianza = valor, 1.0
            break
    if tipo is None:
        for claves, valor in _REGLAS_TIPO:
            if _alguna(claves, f"{d} {origen}"):
                tipo, confianza = valor, 0.6
                break

    # VIBRA — vale cualquier parte del texto: es un hecho del producto.
    vibra = _alguna(_CLAVES_VIBRA, completo)

    # CONTROL
    if _alguna(_CLAVES_APP, completo):
        control = "app"
    elif _alguna(_CLAVES_REMOTO, completo):
        control = "remoto"
    elif vibra:
        control = "manual"
    else:
        control = "ninguno"

    # ZONA — primero lo que dice el nombre, luego el texto completo, y si nada
    # lo aclara, lo que implique el tipo.
    zona = None
    for claves, valor in _REGLAS_ZONA:
        if _alguna(claves, n):
            zona = valor
            break
    if zona is None and tipo in _ZONA_POR_TIPO:
        zona = _ZONA_POR_TIPO[tipo]
    if zona is None:
        for claves, valor in _REGLAS_ZONA:
            if _alguna(claves, f"{d} {origen}"):
                zona = valor
                break
    if zona is None or tipo in _TIPOS_SIN_ZONA:
        zona = "ninguna"

    # ATRIBUTOS
    atributos = sorted(
        attr for attr, claves in _ATRIBUTOS.items() if _alguna(claves, completo)
    )

    # GÉNERO
    genero = "unisex"
    for claves, valor in _REGLAS_GENERO:
        if _alguna(claves, completo):
            genero = valor
            break

    return Facetas(tipo=tipo, zona=zona, vibra=vibra, control=control,
                   genero_uso=genero, atributos=atributos, confianza=confianza)


# ── Respaldo con LLM para los productos que las reglas no deciden ──

UMBRAL_CONFIANZA = 0.6

_PROMPT_FACETAS = f"""Clasificas productos de una sex shop. Responde SOLO un JSON válido.

Campos y valores permitidos (no inventes otros):
- "tipo": uno de {list(TIPOS)}
- "zona": uno de {list(ZONAS)}
- "vibra": true o false
- "control": uno de {list(CONTROLES)}
- "genero_uso": uno de {list(GENEROS)}

Reglas importantes:
- "enema" es para higiene/limpieza anal, NO es un juguete de estimulación.
- "plug" es un tapón anal; si además vibra, vibra=true (sigue siendo plug).
- "vibrador" incluye masajeadores de próstata y varitas tipo Hitachi.
- "arnes" es un strap-on que se lleva puesto para penetrar a la pareja.
- Si el producto no se aplica a una zona concreta (lubricantes, lencería,
  bondage, juegos), usa zona "ninguna".

Ejemplo: {{"tipo":"plug","zona":"anal","vibra":true,"control":"app","genero_uso":"unisex"}}"""


async def clasificar_con_llm(nombre: str, descripcion: str | None = "") -> Facetas | None:
    """Clasifica con el LLM. Devuelve None ante cualquier fallo.

    Sigue el patrón ya probado de `openai_client.clasificar_intencion_llm`:
    conjunto cerrado de valores, temperatura 0 y validación de todo lo que
    vuelve. Nunca debe romper el flujo: quien llama se queda con las reglas.
    """
    if not nombre or not nombre.strip():
        return None
    try:
        import json

        from app import config, openai_client

        texto = f"Nombre: {nombre}\nDescripción: {(descripcion or '')[:400]}"
        resp = await openai_client._get_client().chat.completions.create(
            model=config.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": _PROMPT_FACETAS},
                {"role": "user", "content": texto},
            ],
            max_tokens=120,
            temperature=0.0,
            timeout=10.0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(0))
    except Exception:
        return None

    tipo = str(data.get("tipo", "")).strip().lower()
    if tipo not in TIPOS:
        return None
    zona = str(data.get("zona", "")).strip().lower()
    control = str(data.get("control", "")).strip().lower()
    genero = str(data.get("genero_uso", "")).strip().lower()
    return Facetas(
        tipo=tipo,
        zona=zona if zona in ZONAS else "ninguna",
        vibra=bool(data.get("vibra", False)),
        control=control if control in CONTROLES else "ninguno",
        genero_uso=genero if genero in GENEROS else "unisex",
        atributos=[],
        confianza=0.8,
    )


async def clasificar(nombre: str, descripcion: str | None = "",
                     cat_origen: str | None = "",
                     permitir_llm: bool = True) -> tuple[Facetas, str]:
    """Clasificación híbrida. Devuelve (facetas, origen) con origen reglas|llm.

    Las reglas resuelven la mayoría sin coste. El LLM entra solo cuando no hay
    señal suficiente, y sus atributos se completan siempre con los de las reglas
    (el LLM no los devuelve).
    """
    f = clasificar_por_reglas(nombre, descripcion, cat_origen)
    if f.confianza >= UMBRAL_CONFIANZA or not permitir_llm:
        return f, "reglas"
    f_llm = await clasificar_con_llm(nombre, descripcion)
    if f_llm is None:
        return f, "reglas"
    f_llm.atributos = f.atributos
    if f.vibra:
        f_llm.vibra = True
    return f_llm, "llm"
