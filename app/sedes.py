"""Data de sedes físicas de Tu Deseo + detección de menciones en mensajes.

Cuando el bot menciona una sede en su respuesta, el hook de main.py usa
`detectar_sede(texto)` para enviar el link de Google Maps (ubicación exacta)
automáticamente por WhatsApp.
"""
from __future__ import annotations


# Sede -> {dir, link}. El link es el de Google Maps (con kgmid/daddr) que
# resuelve la ubicación EXACTA del negocio al tocarlo.
SEDES: dict[str, dict] = {
    'Unicentro': {"dir": 'Cra 15 # 119 56, Bogotá', "link": 'https://www.google.com/maps/dir//Tu+Deseo+Sex+Shop+Unicentro,+Ak+15+%23119+-+56,+Bogot%C3%A1/@4.6482975,-74.107807,11z/data=!4m8!4m7!1m0!1m5!1m1!1s0x8e3f9b300e29a321:0xea9cb26b10737dc2!2m2!1d-74.042773!2d4.699921'},
    'Frente Unicentro': {"dir": 'Cra 15 # 124 17 local 117, Bogotá', "link": 'https://www.google.com/maps?sca_esv=cb0b2358a0071c17&hl=es&um=1&ie=UTF-8&fb=1&gl=co&sa=X&geocode=KdmJhd5bmz-OMffdZJjabxrX&daddr=Ak+15+%23124+-17+Loc+117,+Bogot%C3%A1&kgmid=/g/11wr1c_vzs'},
    'Cedritos': {"dir": 'Cra 19 # 138 44, Bogotá', "link": 'https://www.google.com/maps?sca_esv=cb0b2358a0071c17&hl=es&um=1&ie=UTF-8&fb=1&gl=co&sa=X&geocode=KZWFS9CVhT-OMXF4ZXA6Th-s&daddr=Cra+19+%23138-44,+Bogot%C3%A1&kgmid=/g/11y3_jfhly'},
    'Engativá': {"dir": 'Transversal 93a # 80c 6, Bogotá', "link": 'https://www.google.com/maps?hl=es&um=1&ie=UTF-8&fb=1&gl=co&sa=X&geocode=KcPmFKozmz-OMZRBXfGS8OYn&daddr=Tv.+93a+%2380c+06,+Bogot%C3%A1&kgmid=/g/11rd_bvps8'},
    'Chapinero': {"dir": 'Cra 11 # 71 40 segundo piso, Bogotá', "link": 'https://www.google.com/maps?hl=es&um=1&ie=UTF-8&fb=1&gl=co&sa=X&geocode=KQFM9Cpfmz-OMR_PQxmC9ObW&daddr=Cra.+11+%2371-40,+Bogot%C3%A1&kgmid=/g/11p5l87p42'},
    'Ferias': {"dir": 'Cll 72 # 70 44, Bogotá', "link": 'https://www.google.com/maps?hl=es&um=1&ie=UTF-8&fb=1&gl=co&sa=X&geocode=KZl_MfY1mz-OMWSwHt0BSihY&daddr=Cl+72+%2370-44,+Bogot%C3%A1&kgmid=/g/11x0tkxd2d'},
    'Usaquén': {"dir": 'Cra 15 #119-80, Bogotá', "link": 'https://www.google.com/maps?hl=es&um=1&ie=UTF-8&fb=1&gl=co&sa=X&geocode=KemQ1wVGmz-OMUOUOVTaHzSK&daddr=Ak+15+%23119-80,+Bogot%C3%A1&kgmid=/g/11w9ydcm_b'},
    'Colina': {"dir": 'Cll 130 #58 20 Local 134 Plaza Aventura, Suba, Bogotá', "link": 'https://www.google.com/maps?um=1&ie=UTF-8&fb=1&gl=co&sa=X&geocode=KccUdseuhT-OMcjvu7N1xbyK&daddr=Centro+Comercial+Plaza+Aventura,+Cl.+130+%2358+20+Local+134,+Suba,+Bogot%C3%A1,+Cundinamarca&kgmid=/g/11s7ym7c8n'},
    'C.C Panamá': {"dir": 'Dg.182 #20-91 Local 103E, Usaquén, Bogotá', "link": 'https://www.google.com/maps?um=1&ie=UTF-8&fb=1&gl=co&sa=X&geocode=KS1OdtgChT-OMQ-qTMeBJvA5&daddr=Dg.+182+%2320-91,+Usaqu%C3%A9n,+Bogot%C3%A1,+Cundinamarca&kgmid=/g/11spm8xk6g'},
}


def detectar_sede(texto: str) -> str | None:
    """Detecta si un texto menciona una sede por nombre (o variante).

    Devuelve el nombre canónico de la sede, o None si no menciona ninguna.
    Búsqueda case-insensitive, tolerante a acentos y palabras parciales.
    """
    if not texto:
        return None
    import unicodedata
    t = unicodedata.normalize("NFKD", texto.lower()).encode("ascii", "ignore").decode()
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


def get_info(nombre: str) -> dict | None:
    """Devuelve {dir, link} de una sede por su nombre canónico."""
    return SEDES.get(nombre)
