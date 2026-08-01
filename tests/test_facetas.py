"""Clasificación de productos por facetas.

Reemplaza la lista ordenada de palabras que decidía una única "categoría
funcional" por atributos independientes. El caso que lo motivó: la categoría
`anal` metía en el mismo cajón enemas de limpieza, plugs sin vibración, plugs
vibradores, arneses strap-on y masajeadores de próstata. Un cliente que pedía
"vibrador anal" recibía enemas, porque nada distinguía un irrigador de higiene
de un Lovense Hush 2.

Con facetas, esa distinción es explícita y consultable:
    enema        → tipo=enema,    zona=anal, vibra=False
    plug simple  → tipo=plug,     zona=anal, vibra=False
    Hush 2       → tipo=plug,     zona=anal, vibra=True
    Edge 2       → tipo=vibrador, zona=anal, vibra=True
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

for _m in ("asyncpg", "httpx", "openai", "qdrant_client", "redis", "redis.asyncio",
           "tiktoken", "PIL", "PIL.Image"):
    _mod = types.ModuleType(_m)
    _mod.__getattr__ = lambda _n: type("_Any", (), {"__init__": lambda *a, **k: None})  # type: ignore[attr-defined]
    sys.modules.setdefault(_m, _mod)

from app import facetas  # noqa: E402


# (nombre, descripcion, tipo, zona, vibra) — textos reales del catálogo
CASOS = [
    # El caso del reporte: los tres viven hoy en la categoría "anal"
    ("Enema para limpieza Anal Lito",
     "Irrigador para higiene íntima anal con cánula",
     "enema", "anal", False),
    ("Enema para limpieza Anal Jervis Optimus",
     "Ducha anal para limpieza previa",
     "enema", "anal", False),
    ("Plug Anal Basix Pipedreame Negro Siliconado",
     "Plug anal de silicona para principiantes",
     "plug", "anal", False),
    ("Plug Lovense Hush 2",
     "Plug anal vibrador con control por aplicación",
     "plug", "anal", True),
    ("Satisfyer Plug Ilicious 1 Negro con App",
     "Plug anal con vibración y control por app",
     "plug", "anal", True),
    ("Lovense Masajeador Prostático Edge 2",
     "Masajeador de próstata con vibración y control por app",
     "vibrador", "anal", True),
    ("Bolas Anales Pimpo",
     "Bolas anales de silicona graduadas",
     "bolas", "anal", False),
    # Arneses: se usan sobre la pareja, no son plugs
    ("Arnes Fetish Fantasy Series Classix Strap-On",
     "Arnés ajustable con dildo para penetración",
     "arnes", "vaginal", False),
    # Los dos vibradores que se enviaron como "para el pene"
    ("Majestic Vibrador tipo Hitachi Ursula",
     "Vibrador en tono rosa, obra maestra de la estimulación profunda",
     "vibrador", "clitoris", True),
    ("Vibrador Doble Tifany Camtoyz",
     "Sistema de doble estimulación pulsante con varios modos de vibración",
     "vibrador", "vaginal", True),
    # Los dos productos que no aparecían por culpa de su descripción
    ("Bomba Automática para Pene Recargable Bender Optimus Pro",
     "Bomba automática para pene con sistema eléctrico de succión y válvula",
     "bomba", "pene", False),
    ("Succionador Con Ondas Y Vibracion Nyla Fuscia",
     "Succionador de clítoris con ondas de presión. Compatible con lubricantes a base de agua",
     "succionador", "clitoris", True),
    # Resto de tipos
    ("Anillo Vibrador Candil para el Pene",
     "Anillo vibrador de silicona para el pene",
     "anillo", "pene", True),
    ("Funda para el Pene Ultrarealista Cobra Camtoyz",
     "Funda extensora ultrarrealista",
     "funda", "pene", False),
    ("Huevo Masturbador Goggy",
     "Masturbador masculino desechable",
     "masturbador", "pene", False),
    ("Lubricante BliX Sensación Caliente Sabor Cereza 30 ML",
     "Lubricante íntimo con sabor a cereza y sensación caliente",
     "lubricante", "ninguna", False),
    ("Dildo Ultra Realista Burgo Camtoyz",
     "Dildo realista con ventosa",
     "dildo", "vaginal", False),
    ("Kit Bondage Fiore",
     "Amarres de manos y pies, mordaza, collar, látigo y antifaz",
     "bondage", "ninguna", False),
    ("Juego De Mesa SexPlay Kamasutra",
     "Juego de mesa para parejas",
     "juego", "ninguna", False),
]


def test_tipo_zona_y_vibra_por_reglas():
    fallos = []
    for nombre, desc, tipo, zona, vibra in CASOS:
        f = facetas.clasificar_por_reglas(nombre, desc, "")
        if f.tipo != tipo:
            fallos.append(f"{nombre[:45]}: tipo {f.tipo!r} != {tipo!r}")
        if f.zona != zona:
            fallos.append(f"{nombre[:45]}: zona {f.zona!r} != {zona!r}")
        if f.vibra is not vibra:
            fallos.append(f"{nombre[:45]}: vibra {f.vibra!r} != {vibra!r}")
    assert not fallos, "\n  " + "\n  ".join(fallos)


def test_enema_y_plug_vibrador_son_distinguibles():
    """El corazón del bug: ambos son 'anal' pero no son lo mismo."""
    enema = facetas.clasificar_por_reglas("Enema para limpieza Anal Lito", "Irrigador", "")
    hush = facetas.clasificar_por_reglas("Plug Lovense Hush 2", "Plug anal vibrador con app", "")
    assert enema.tipo != hush.tipo
    assert enema.vibra is False and hush.vibra is True


def test_control_por_app_se_detecta():
    f = facetas.clasificar_por_reglas("Satisfyer Plug Ilicious 1 Negro con App",
                                      "Control por aplicación", "")
    assert f.control == "app"
    f2 = facetas.clasificar_por_reglas("Vibrador Asmel Camtoyz Control Remoto",
                                       "Con control remoto inalámbrico", "")
    assert f2.control == "remoto"
    f3 = facetas.clasificar_por_reglas("Plug Anal Rómulo CamToyz", "Plug de silicona", "")
    assert f3.control in ("manual", "ninguno")


def test_atributos_relevantes():
    f = facetas.clasificar_por_reglas("Dildo Ultra Realista Burgo Camtoyz",
                                      "Dildo realista con ventosa", "")
    assert "realista" in f.atributos and "ventosa" in f.atributos
    f2 = facetas.clasificar_por_reglas("Lubricante BliX Sabor Cereza",
                                       "Lubricante con sabor a cereza", "")
    assert "sabor" in f2.atributos


def test_la_confianza_baja_cuando_las_reglas_no_deciden():
    """Sin señal, no se inventa un tipo: queda para que lo resuelva el LLM."""
    f = facetas.clasificar_por_reglas("Producto Misterioso XYZ", "", "")
    assert f.tipo is None
    assert f.confianza == 0.0


def test_el_nombre_manda_sobre_la_descripcion():
    """Regresión: la descripción secuestraba la clasificación."""
    f = facetas.clasificar_por_reglas(
        "Succionador Con Ondas Y Vibracion Nyla Fuscia",
        "Compatible con lubricantes a base de agua", "")
    assert f.tipo == "succionador", "la palabra 'lubricantes' de la descripción no manda"


def test_no_hay_coincidencias_dentro_de_otras_palabras():
    """Regresión: 'funda' dentro de 'profunda', 'pene' dentro de 'penetración'."""
    f = facetas.clasificar_por_reglas("Vibrador Ursula", "Estimulación profunda", "")
    assert f.tipo != "funda"
    f2 = facetas.clasificar_por_reglas("Arnes Strap-On", "Para doble penetracion", "")
    assert f2.zona != "pene"
