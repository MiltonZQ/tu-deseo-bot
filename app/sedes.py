"""Data de sedes físicas de Tu Deseo + detección de menciones en mensajes.

Cuando el bot menciona una sede en su respuesta, el hook de main.py usa
`detectar_sede(texto)` para enviar la ubicación automáticamente por WhatsApp.
"""
from __future__ import annotations


# Sedes con coordenadas (lat, lng). Nombre -> datos.
SEDES: dict[str, dict] = {
    'Unicentro': {"lat": 4.6482975, "lng": -74.107807, "dir": 'Cra 15 # 119 56, Bogotá'},
    'Frente Unicentro': {"lat": 4.6011126, "lng": -74.0827716, "dir": 'Cra 15 # 124 17 local 117, Bogotá'},
    'Cedritos': {"lat": 4.6533817, "lng": -74.0836331, "dir": 'Cra 19 # 138 44, Bogotá'},
    'Engativá': {"lat": 4.6901387, "lng": -74.1173778, "dir": 'Transversal 93a # 80c 6, Bogotá'},
    'Chapinero': {"lat": 4.6674931, "lng": -74.0563171, "dir": 'Cra 11 # 71 40 segundo piso, Bogotá'},
    'Ferias': {"lat": 4.6518184, "lng": -74.0510026, "dir": 'Cll 72 # 70 44, Bogotá'},
    'Usaquén': {"lat": 4.6011126, "lng": -74.0827716, "dir": 'Cra 15 #119-80, Bogotá'},
    'Colina': {"lat": 4.720158, "lng": -74.0683547, "dir": 'Cll 130 #58 20 Local 134 Plaza Aventura, Suba, Bogotá'},
    'C.C Panamá': {"lat": 4.758657, "lng": -74.0436105, "dir": 'Dg.182 #20-91 Local 103E, Usaquén, Bogotá'},
}


def detectar_sede(texto: str) -> str | None:
    """Detecta si un texto menciona una sede por nombre (o variante).

    Devuelve el nombre canónico de la sede, o None si no menciona ninguna.
    Búsqueda case-insensitive, tolerante a acentos y palabras parciales.
    """
    if not texto:
        return None
    t = texto.lower()
    # Quitar acentos para matching tolerante
    import unicodedata
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    # Apodos/variantes -> nombre canónico
    variantes = {
        "unicentro": "Unicentro",
        "frente unicentro": "Frente Unicentro",
        "cedritos": "Cedritos",
        "engativa": "Engativá",
        "chapinero": "Chapinero",
        "ferias": "Ferias",
        "las ferias": "Ferias",
        "usaquen": "Usaquén",
        "colina": "Colina",
        "plaza aventura": "Colina",
        "panama": "C.C Panamá",
        "c.c panama": "C.C Panamá",
        "cc panama": "C.C Panamá",
        "centro comercial panama": "C.C Panamá",
    }
    for clave, nombre in variantes.items():
        if clave in t:
            return nombre
    return None


def get_coords(nombre: str) -> dict | None:
    """Devuelve {lat, lng, dir} de una sede por su nombre canónico."""
    s = SEDES.get(nombre)
    if s:
        return {"lat": s["lat"], "lng": s["lng"], "dir": s["dir"]}
    return None
