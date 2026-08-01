"""Pregunta UNA cosa antes de listar, cuando la petición es demasiado amplia.

Un "quiero ver lubricantes" tiene ~20 productos ofrecibles detrás y solo 5
huecos en el mensaje: el cliente recibe una muestra al azar de cosas que no se
parecen entre sí (un anal desensibilizante junto a uno de sabores y un neutro de
500 ml) en vez de lo que buscaba. Una pregunta convierte esos 5 huecos en 5
opciones que sí le sirven.

Las ramas se filtran contra el inventario real (`catalog.facetas_disponibles`).
Ofrecer "de silicona" cuando no queda ninguno es PEOR que no preguntar: el
cliente elige esa rama, la consulta exacta da 0 filas, y `_ESCALERA_RELAJACION`
suelta el atributo devolviéndole justo lo que acaba de descartar.

Módulo puro, como `facetas.py`: no sabe de base de datos ni de HTTP.
"""
from __future__ import annotations

# Con 1 producto no es una rama, es un producto: el cliente elige "de silicona"
# esperando variedad y recibe una sola foto.
MIN_POR_RAMA = 2
# Más de 4 opciones en un mensaje de WhatsApp no se leen, se saltan.
MAX_RAMAS = 4
# Con una sola rama viva no hay nada que preguntar: se lista y ya.
MIN_RAMAS = 2

# tipo → ramas del menú, en orden de preferencia.
# Cada rama es (clave de recuento, grupo donde se cuenta, etiqueta para el cliente).
# La clave tiene que ser, además, algo que `facetas.interpretar_mensaje` sepa
# leer cuando el cliente la responda — hay un test que lo verifica.
_MENUS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "lubricante": (
        ("neutro", "atributos", "*neutro* (sin sabor ni efecto)"),
        ("sabor", "atributos", "con *sabores*"),
        ("anal", "zonas", "*anal desensibilizante*"),
        ("calor", "atributos", "con *efecto calor*"),
        ("frio", "atributos", "con *efecto frío*"),
        ("silicona", "atributos", "de *silicona*"),
        ("hibrido", "atributos", "*híbrido*"),
    ),
    "vibrador": (
        ("clitoris", "zonas", "para el *clítoris*"),
        ("vaginal", "zonas", "*vaginal / punto G*"),
        ("anal", "zonas", "*anal o próstata*"),
        ("pene", "zonas", "para *el pene*"),
    ),
    "dildo": (
        ("realista", "atributos", "*realista* (textura piel)"),
        ("ventosa", "atributos", "con *ventosa*"),
        ("vidrio", "atributos", "de *vidrio*"),
        ("doble", "atributos", "*doble*"),
    ),
    "lenceria": (
        ("mujer", "generos", "para *ella*"),
        ("hombre", "generos", "para *él*"),
    ),
}

_PREAMBULOS = {
    "lubricante": "¡Claro que sí! Tenemos varios 😊 Para mostrarte el ideal, cuéntame: ¿lo buscas ",
    "vibrador": "¡Buena elección! Para recomendarte el ideal, cuéntame: ¿lo buscas ",
    "dildo": "¡Buena elección! Para mostrarte lo ideal, cuéntame: ¿lo buscas ",
    "lenceria": "¡Claro que sí! Cuéntame, ¿la buscas ",
}


def construir(tipo: str | None, disponibles: dict) -> str | None:
    """Texto de la pregunta, o None si no hay nada útil que preguntar.

    `disponibles` es lo que devuelve `catalog.facetas_disponibles`:
    {"atributos": {...}, "zonas": {...}, "generos": {...}}.

    Devolver None es el caso seguro y frecuente: el tipo no tiene menú, el
    inventario no da para dos ramas, o no se pudieron contar las facetas. Quien
    llama simplemente lista como siempre.
    """
    menu = _MENUS.get(tipo or "")
    if not menu:
        return None

    vivas = [etiqueta for clave, grupo, etiqueta in menu
             if disponibles.get(grupo, {}).get(clave, 0) >= MIN_POR_RAMA]
    if len(vivas) < MIN_RAMAS:
        return None

    vivas = vivas[:MAX_RAMAS]
    # Con dos ramas la coma sobra ("para ella, o para él" no es castellano).
    if len(vivas) == 2:
        opciones = f"{vivas[0]} o {vivas[1]}"
    else:
        opciones = ", ".join(vivas[:-1]) + f", o {vivas[-1]}"
    return f"{_PREAMBULOS[tipo]}{opciones}? 😊"
