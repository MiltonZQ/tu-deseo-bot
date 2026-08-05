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
    # "juego"/"juegos" NO son claves aquí, y se midió por qué. El nombre se
    # evalúa en una pasada propia que gana con confianza 1.0 y corta antes de
    # mirar la descripción, así que "Juego de Kegel con Pesas" dejaba de ser
    # `bolas` y "Muñeca Inflable" dejaba de ser `masturbador`: tres regresiones
    # sobre el catálogo de producción y ninguna mejora. En castellano "juego de
    # X" es un CONJUNTO de X, y los juegos de verdad ya entran por su forma
    # concreta. El hueco estaba en el lado del cliente, no aquí.
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
#
# EL LUBRICANTE NO ESTÁ AQUÍ: sí distingue anal de uso general, y esa distinción
# es la que el cliente pide más ("lubricante anal"). Mientras estuvo en la lista,
# `zona` se forzaba a "ninguna" y la petición no se podía filtrar: la consulta
# exacta daba 0 filas y la escalera de relajación devolvía lubricantes cualesquiera.
_TIPOS_SIN_ZONA = {"cosmetica", "lenceria", "bondage", "juego", "accesorio"}

# Pero un lubricante solo admite ESAS dos zonas. Las demás que las reglas puedan
# inferir del nombre ("piel sensible" → cuerpo) no describen para qué sirve y lo
# sacarían de las búsquedas generales sin aportar nada.
_ZONAS_PERMITIDAS_POR_TIPO = {"lubricante": {"anal", "ninguna"}}

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
    "sabor": ("sabor", "sabores", "comestible"),
    "agua": ("base de agua", "base agua", "h2o", "hidrosoluble", "acuoso"),
    "silicona": ("silicona",),
    "hibrido": ("hibrido", "hibrida"),
    # "retardant" cubre los "Retardante en Spray/Creama" del catálogo: el
    # cliente pide "para demorar" y esos productos son justamente los retardantes
    # clásicos. Sin esta clave el SQL los perdía (atributo vacío) y la Fase 2
    # escalaba a asesor por una venta que sí teníamos.
    "desensibilizante": ("desensibiliz", "anestesico", "relajante anal", "retardant"),
    "calor": ("caliente", "sensacion caliente", "calor"),
    "frio": ("frio", "menta", "efecto frio"),
    # "pequeño" NO es clave: describe el tamaño, no al público. Por ahí entraban
    # un baby doll y un arnés de bondage como productos "para principiantes".
    "principiante": ("principiante", "primera vez", "iniciacion"),
    "recargable": ("recargable", "usb"),
    "impermeable": ("impermeable", "sumergible", "waterproof"),
}

# Atributos que solo se buscan EN EL NOMBRE y solo para ciertos tipos.
#
# Los saborizados casi nunca llevan la palabra "sabor": se llaman por la fruta
# ("BliX Cereza 30 Ml"). Pero esas mismas palabras aparecen en descripciones de
# productos que no son lubricantes —un anillo "compatible con lubricantes de
# sabores", una bomba, un kit de webcam "de tono neutro"— y ahí marcaban el
# producto entero. Medido contra el catálogo de producción: 3 falsos positivos
# de 32 cambios. Acotar por tipo Y mirar solo el nombre los elimina.
#
# "menta" NO está entre los sabores: es la clave de `frio`, y esos lubricantes
# se venden por el efecto, no por el sabor.
_ATRIBUTOS_ACOTADOS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "sabor": (("cereza", "fresa", "chocolate", "maracuya", "sandia", "mango",
               "lychee", "chicle", "whisky", "bombom"),
              ("lubricante", "cosmetica")),
    "neutro": (("neutro", "neutra", "sin sabor", "sin olor"),
               ("lubricante", "cosmetica")),
    # "doble densidad" es la frase comercial de CUALQUIER dildo ultrarrealista, y
    # vive en la descripción: marcaba como dobles a cuatro que no lo son, más
    # anillos, plugs y balas. Solo cuenta si lo dice el nombre, y solo en tipos
    # donde "doble" significa dos penetraciones y no dos capas de silicona.
    #
    # "double" en inglés es obligatorio: media docena de dobles reales solo lo
    # dicen así ("Satisfyer Double Joy", "Satisfyer Double Fun"…).
    #
    # "dual" a secas NO, y esto se midió: en "Bolas Vaginales Kegel Dual
    # Recargable" y "Dual Motor Kegel System" significa dos motores, no doble
    # penetración. Solo entra la frase completa, que sí es inequívoca y recupera
    # el "Accommodator Dual Penetrator".
    "doble": (("doble", "double", "doble penetracion", "doble estimulacion",
               "dual penetrator", "double penetration"),
              ("dildo", "vibrador", "arnes", "anillo")),
}

# Atributos que solo tienen sentido en ciertos tipos, se digan donde se digan.
#
# A diferencia de `_ATRIBUTOS_ACOTADOS` estos SÍ se leen de la descripción: un
# lubricante no suele decir "base de agua" en el nombre. Lo que se corta es que
# se peguen a otro tipo de producto. Medido sobre los 246 ofrecibles: un arnés y
# una funda para el pene salían "a base de agua" porque su ficha dice "usar
# lubricante a base de agua", y un dildo ultrarrealista salía "con efecto calor".
_ATRIBUTOS_POR_TIPO: dict[str, tuple[str, ...]] = {
    "agua": ("lubricante", "cosmetica"),
    "sabor": ("lubricante", "cosmetica"),
    "calor": ("lubricante", "cosmetica"),
    "frio": ("lubricante", "cosmetica"),
}

# Vocabulario público de atributos, para el panel y para quien tenga que
# validar una entrada manual. A diferencia de TIPOS/ZONAS/CONTROLES/GENEROS,
# esta lista sí crece: son características, no una taxonomía cerrada.
ATRIBUTOS = tuple(sorted(set(_ATRIBUTOS) | set(_ATRIBUTOS_ACOTADOS)))


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

    # Ningún nombre del catálogo dice "base de agua", pero un lubricante que no
    # declara silicona ni híbrido lo es. Sin esta inferencia la rama "base de
    # agua" del menú quedaba vacía (0 de 20 productos) y la pregunta al cliente
    # no filtraba nada.
    if tipo == "lubricante" and not ({"silicona", "hibrido"} & set(atributos)):
        atributos = sorted(set(atributos) | {"agua"})

    # Un desensibilizante es de uso anal aunque el nombre no lo diga: es para lo
    # único que se vende. Es lo que hace filtrable "lubricante anal".
    if tipo in ("lubricante", "cosmetica") and "desensibilizante" in atributos:
        zona = "anal"

    _permitidas = _ZONAS_PERMITIDAS_POR_TIPO.get(tipo or "")
    if _permitidas and zona not in _permitidas:
        zona = "ninguna"

    # GÉNERO
    genero = "unisex"
    for claves, valor in _REGLAS_GENERO:
        if _alguna(claves, completo):
            genero = valor
            break

    f = Facetas(tipo=tipo, zona=zona, vibra=vibra, control=control,
                genero_uso=genero, atributos=atributos, confianza=confianza)
    aplicar_atributos_acotados(nombre, f)
    return f


def aplicar_atributos_acotados(nombre: str, f: Facetas) -> None:
    """Ajusta los atributos de `f` que dependen del tipo del producto.

    Añade los de `_ATRIBUTOS_ACOTADOS` (que solo cuentan si lo dice el NOMBRE) y
    quita los de `_ATRIBUTOS_POR_TIPO` que se hayan pegado a un tipo donde no
    significan nada.

    Va aparte porque hay que aplicarlo dos veces: tras las reglas y, cuando el
    tipo lo decide el LLM, otra vez con ese tipo ya conocido.
    """
    n = normalizar(nombre)
    for attr, (claves, tipos_validos) in _ATRIBUTOS_ACOTADOS.items():
        if f.tipo in tipos_validos and attr not in f.atributos and _alguna(claves, n):
            f.atributos = sorted(f.atributos + [attr])
    # Sin tipo no se puede podar: lo decidirá el LLM y este mismo pase corre otra
    # vez después. Podar aquí borraría el atributo antes de saber si era válido.
    if f.tipo:
        f.atributos = [a for a in f.atributos
                       if f.tipo in _ATRIBUTOS_POR_TIPO.get(a, (f.tipo,))]


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


# ══════════════════════════════════════════════════════════════════════════
# Lo que pide el CLIENTE (no lo que ES el producto)
# ══════════════════════════════════════════════════════════════════════════
#
# La intención del cliente es multidimensional y se construye entre turnos:
#
#   "tienen vibradores"  → {tipo: vibrador}
#   "anal"               → {tipo: vibrador, zona: anal}     ← REFINA
#   "con app"            → {tipo: vibrador, zona: anal, control: app}
#   "ahora lubricantes"  → {tipo: lubricante}               ← REEMPLAZA
#
# Antes esto era UNA variable que se sobrescribía, así que "anal" borraba
# "vibradores" y el cliente que pedía un vibrador anal recibía enemas. Los
# parches previos (lista fija de sustantivos de cambio de tema, caso especial de
# lubricantes) eran aproximaciones a la regla que está abajo, en
# `fusionar_restricciones`.

# (claves que puede decir el cliente, restricciones que aportan). Orden: lo más
# específico primero, porque gana la primera coincidencia por campo.
_VOCABULARIO_CLIENTE: tuple[tuple[tuple[str, ...], dict], ...] = (
    # ── tipo ──
    (("ducha anal", "duchas anales", "enema", "enemas", "irrigador"), {"tipo": "enema"}),
    (("bolas anales", "bolas chinas", "kegel", "ben wa"), {"tipo": "bolas"}),
    (("strap on", "strap-on", "arnes", "arneses"), {"tipo": "arnes"}),
    (("succionador", "succionadores", "succion", "satisfyer pro"), {"tipo": "succionador"}),
    (("masturbador", "masturbadores", "huevo masturb"), {"tipo": "masturbador"}),
    (("bomba", "bombas"), {"tipo": "bomba"}),
    (("plug", "plugs", "tapon"), {"tipo": "plug"}),
    (("anillo", "anillos", "cockring"), {"tipo": "anillo"}),
    (("funda", "fundas", "extensor", "engrosador"), {"tipo": "funda"}),
    (("dildo", "dildos", "consolador", "consoladores"), {"tipo": "dildo"}),
    # "vibrador" aporta el tipo Y la vibración. Es lo que conserva el sentido de
    # la petición si más adelante hay que ceder en el tipo: al pedir "vibrador
    # para el pene" no hay ningún tipo=vibrador con zona=pene, pero sí anillos
    # que vibran, y son la respuesta correcta.
    (("vibrador", "vibradores", "vibradorcito"), {"tipo": "vibrador", "vibra": True}),
    (("lubricante", "lubricantes", "lubricacion", "gel intimo"), {"tipo": "lubricante"}),
    (("estimulante", "retardante", "afrodisiaco", "potenciador",
      "multiorgasmo", "multiorgasmos"), {"tipo": "cosmetica"}),
    # Los suspensorios del catálogo son todos masculinos: la palabra aporta el
    # tipo Y el género. Sin esto, "suspensores de hombre" devolvía conjuntos de
    # mujer, que fue el fallo reportado.
    (("suspensorio", "suspensorios", "suspensor", "suspensores"),
     {"tipo": "lenceria", "genero_uso": "hombre"}),
    (("lenceria", "baby doll", "babydoll", "bodys", "bodies", "body",
      "conjunto", "disfraz", "disfraces",
      "pechera", "liguero", "tanga"), {"tipo": "lenceria"}),
    (("bondage", "bdsm", "esposas", "antifaz", "antifaces", "latigo", "latigos",
      "fusta", "fustas", "mordaza", "amarre", "amarres", "vendas", "sado",
      "sadomasoquismo", "collar"), {"tipo": "bondage"}),
    # "juegos" a secas es lo que escribe un cliente real; las demás claves son
    # las formas concretas. Va después de las de producto por el mismo motivo
    # que en `_REGLAS_TIPO`: "juego de anillos" son anillos.
    (("juego de mesa", "juegos de mesa", "jenga", "cartas", "dados", "ruleta",
      "juego", "juegos"), {"tipo": "juego"}),
    # ── zona ──
    (("prostata", "prostatico"), {"zona": "anal"}),
    (("anal", "anales", "por atras", "el culo", "recto", "cola"), {"zona": "anal"}),
    (("clitoris", "clitorial", "clitorion"), {"zona": "clitoris"}),
    (("punto g", "vaginal", "vagina", "penetracion"), {"zona": "vaginal"}),
    (("pene", "chimbo", "miembro", "verga", "pito", "glande"), {"zona": "pene"}),
    (("pezon", "pezones"), {"zona": "pezones"}),
    # ── control ──
    (("con app", "control por app", "por aplicacion", "con aplicacion",
      "app control"), {"control": "app"}),
    (("control remoto", "a distancia", "inalambrico"), {"control": "remoto"}),
    # ── género / uso ──
    # OJO CON EL ORDEN: gana la primera coincidencia por campo, y las claves
    # llevan límite de palabra solo a la izquierda. "para el" coincidiría dentro
    # de "para ella", así que mujer va ANTES que hombre.
    (("en pareja", "para pareja", "para parejas", "pareja", "parejas",
      "los dos", "con mi novia", "con mi esposa", "con mi pareja",
      "mi novia", "mi esposa", "mi novio", "mi esposo",
      "we vibe", "we-vibe", "chorus"), {"genero_uso": "pareja"}),
    (("para ella", "de mujer", "para mujer", "para mi novia", "para mi esposa",
      "femenino", "femenina", "mujer", "mujeres", "dama", "damas",
      "clit"), {"genero_uso": "mujer"}),
    (("de hombre", "para hombre", "para el", "para él", "para mi",
      "masculino", "masculina", "hombre", "hombres", "caballero", "caballeros",
      "gallo", "pito"), {"genero_uso": "hombre"}),
)

# Atributos que el cliente pide por nombre. `solo_para` declara con qué tipos
# tiene sentido el atributo: si el tipo activo no está en la lista, el atributo
# IMPLICA un cambio de tipo. Así "sabores" tras hablar de bombas de vacío pasa a
# lubricantes (una bomba no tiene sabores) pero tras hablar de lubricantes solo
# filtra. Esto sustituye al caso especial que había escrito a mano.
_ATRIBUTOS_CLIENTE: tuple[tuple[tuple[str, ...], str, tuple[str, ...], str | None], ...] = (
    (("sabor", "sabores", "saborizado", "comestible"), "sabor",
     ("lubricante", "cosmetica"), "lubricante"),
    (("base de agua", "base agua", "a base de agua", "con agua", "hidrosoluble"), "agua",
     ("lubricante", "cosmetica"), "lubricante"),
    # Solo las palabras inequívocas. "normal" o "sencillo" también significan
    # neutro para un cliente, pero como `tipo_implicito` es lubricante, un
    # "quiero algo sencillo" hablando de vibradores cambiaría de tema.
    (("neutro", "neutra", "sin sabor", "sin olor"), "neutro",
     ("lubricante", "cosmetica"), "lubricante"),
    (("hibrido", "hibrida"), "hibrido", ("lubricante", "cosmetica"), "lubricante"),
    (("silicona", "de silicona"), "silicona", (), None),
    (("desensibilizante", "anestesico", "relajante anal"), "desensibilizante",
     ("lubricante", "cosmetica"), "lubricante"),
    (("realista", "textura piel"), "realista", ("dildo", "funda"), "dildo"),
    (("ventosa", "con ventosa"), "ventosa", ("dildo",), "dildo"),
    (("vidrio", "cristal"), "vidrio", ("dildo", "plug"), None),
    (("doble", "doble estimulacion"), "doble", (), None),
    (("primera vez", "principiante", "para empezar", "iniciacion"), "principiante", (), None),
    (("recargable",), "recargable", (), None),
    (("calor", "caliente"), "calor", ("lubricante", "cosmetica"), "lubricante"),
    (("frio", "efecto frio"), "frio", ("lubricante", "cosmetica"), "lubricante"),
)

# "que vibre" / "con vibración" sin nombrar el tipo.
_CLAVES_PIDE_VIBRACION = ("que vibre", "con vibracion", "vibracion", "vibre", "vibrante")


def interpretar_mensaje(texto: str) -> dict:
    """Restricciones que aporta ESTE mensaje (vacío si no habla de productos).

    No mira el historial: solo lo que dice el cliente ahora. Combinar con lo
    anterior es tarea de `fusionar_restricciones`.
    """
    t = normalizar(texto)
    if not t.strip():
        return {}
    encontradas: dict = {}
    for claves, aporte in _VOCABULARIO_CLIENTE:
        for campo, valor in aporte.items():
            if campo in encontradas:
                continue  # ya resuelto por una clave más específica
            if _alguna(claves, t):
                encontradas[campo] = valor
    atributos: list[str] = []
    implicitos: list[tuple[str, tuple[str, ...], str | None]] = []
    for claves, attr, solo_para, tipo_implicito in _ATRIBUTOS_CLIENTE:
        if _alguna(claves, t):
            atributos.append(attr)
            implicitos.append((attr, solo_para, tipo_implicito))
    if atributos:
        encontradas["atributos"] = sorted(set(atributos))
        encontradas["_implicitos"] = implicitos
    if _alguna(_CLAVES_PIDE_VIBRACION, t):
        encontradas["vibra"] = True
    # En "arnés con pene", el término pene describe la prótesis incorporada al arnés, no la zona corporal del usuario.
    if encontradas.get("tipo") == "arnes" and encontradas.get("zona") == "pene":
        encontradas.pop("zona", None)
    return encontradas


def fusionar_restricciones(previas: dict | None, nuevas: dict) -> dict:
    """Combina lo que ya sabíamos con lo que aporta el mensaje nuevo.

    LA REGLA, que sustituye a todos los parches por palabra:
      - un TIPO distinto = cambio de tema → reemplaza todo.
      - cualquier otra faceta (zona, control, atributo, género) = refinamiento →
        se suma a lo que ya había.

    Un atributo cuyo `solo_para` no incluya el tipo activo también cuenta como
    cambio de tema ("sabores" cuando veníamos de bombas de vacío).
    """
    previas = dict(previas or {})
    previas.pop("_implicitos", None)
    nuevas = dict(nuevas or {})
    implicitos = nuevas.pop("_implicitos", [])
    if not nuevas:
        return previas

    tipo_previo = previas.get("tipo")
    tipo_nuevo = nuevas.get("tipo")

    # Un atributo incompatible con el tipo activo implica cambio de tipo.
    if not tipo_nuevo and tipo_previo:
        for _attr, solo_para, tipo_implicito in implicitos:
            if solo_para and tipo_previo not in solo_para and tipo_implicito:
                tipo_nuevo = nuevas["tipo"] = tipo_implicito
                break
    # Sin tipo previo, un atributo puede aportarlo ("sabores" en frío).
    if not tipo_nuevo and not tipo_previo:
        for _attr, _solo_para, tipo_implicito in implicitos:
            if tipo_implicito:
                tipo_nuevo = nuevas["tipo"] = tipo_implicito
                break

    if tipo_nuevo and tipo_nuevo != tipo_previo:
        return nuevas  # cambio de tema: se descarta el contexto anterior

    fusion = dict(previas)
    for campo, valor in nuevas.items():
        if campo == "atributos":
            fusion["atributos"] = sorted(set(fusion.get("atributos", [])) | set(valor))
        else:
            fusion[campo] = valor
    return fusion


# ── VOCABULARIO CANÓNICO DE CATEGORÍAS ──
# La ÚNICA lista de categorías funcionales válidas del sistema. Todo lo demás
# —las listas de palabras de `catalog.py`, el vocabulario cerrado del
# clasificador LLM, el mapa a tipos, Qdrant y el estado de conversación— debe
# beber de aquí.
#
# Existe porque había dos vocabularios divergentes: las listas estáticas
# producían `anillos-vibradores`, y el LLM `anillos-y-fundas`, para lo mismo. De
# 13 categorías solo se compartían 7, y el código lo iba parcheando a mano
# (`if categoria_funcional in ("anillos-vibradores", "anillos-y-fundas")`). Con
# dos nombres para una idea, cada camino de recuperación filtraba distinto y el
# cliente recibía productos de otra categoría.
#
# `anillos-vibradores` se retiró a favor de `anillos-y-fundas`, que es la que
# entiende el LLM y la que describe el catálogo real (anillos Y fundas de pene).
CATEGORIAS_FUNCIONALES: tuple[str, ...] = (
    "vibradores", "succionadores", "anal", "dildos", "anillos-y-fundas",
    "fundas-pene", "masturbadores", "bombas-pene", "lubricantes-y-cuidado",
    "lenceria", "pareja-y-bondage", "juegos-y-accesorios",
)

# Puente con la "categoría funcional" que todavía usan el estado de conversación,
# Qdrant y el prompt. Se mantiene mientras dure la transición.
_TIPO_A_CATEGORIA_LEGACY = {
    "vibrador": "vibradores", "succionador": "succionadores",
    "plug": "anal", "bolas": "anal", "enema": "anal", "arnes": "pareja-y-bondage",
    "dildo": "dildos", "anillo": "anillos-y-fundas", "funda": "fundas-pene",
    "masturbador": "masturbadores", "bomba": "bombas-pene",
    "lubricante": "lubricantes-y-cuidado", "cosmetica": "lubricantes-y-cuidado",
    "lenceria": "lenceria", "bondage": "pareja-y-bondage",
    "juego": "juegos-y-accesorios", "accesorio": "juegos-y-accesorios",
}


def categoria_legacy(restricciones: dict) -> str | None:
    return _TIPO_A_CATEGORIA_LEGACY.get((restricciones or {}).get("tipo") or "")


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
    # Los atributos acotados dependen del tipo, y aquí el tipo lo acaba de
    # decidir el LLM. Sin este segundo pase, un "BliX Cereza 30 Ml" —cuyo nombre
    # no dice "lubricante", así que las reglas no lo clasifican— se quedaba sin
    # el atributo `sabor` y no aparecía en esa rama del menú.
    aplicar_atributos_acotados(nombre, f_llm)
    return f_llm, "llm"
