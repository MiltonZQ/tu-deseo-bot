"""Corre los casos de `casos.json` contra un árbol del repo y compara versiones.

Responde una pregunta que la suite no puede responder: con el catálogo REAL y el
modelo REAL, ¿el bot contesta mejor o peor que antes de un cambio?

Dos capas por caso:

  * DECISIONES — `main._recuperar_candidatos`, el núcleo determinista: qué
    categoría clasifica, qué restricciones arma, qué productos devuelve y si
    escala. Es exacto y repetible; se compara por diferencia de conjuntos.
  * RESPUESTA — el texto que le llegaría al cliente, replicando la MISMA cadena
    de ramas de `main._handle_message`: hay turnos que redacta el sistema
    (lista de productos, pregunta de calificación, categoría agotada, handoff)
    y turnos que redacta el LLM (asesoría). Evaluar solo la rama del LLM daría
    una foto falsa, porque la mayoría de los turnos de producto ni la tocan.

Uso:
    python3 scripts/eval/run.py --arbol . --salida /tmp/actual.json
    python3 scripts/eval/run.py --arbol /tmp/base --salida /tmp/base.json
    python3 scripts/eval/run.py --comparar /tmp/base.json /tmp/actual.json

`--sin-llm` corre solo la capa de decisiones (gratis y determinista) y es el
humo que conviene pasar antes de gastar llamadas.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import sys
import types
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

_AQUI = Path(__file__).resolve().parent
_REPO = _AQUI.parents[1]
sys.path.insert(0, str(_AQUI))

import fake_db  # noqa: E402
import fake_openai  # noqa: E402
import snapshot_catalogo  # noqa: E402


# ── Montaje del árbol bajo evaluación ──

def montar(arbol: Path, env: dict, registro_llm: Path | None, sin_llm: bool):
    """Importa `app.main` desde `arbol` con las dos fronteras sustituidas.

    Se limpian los módulos `app.*` ya cargados para poder montar dos árboles
    distintos en procesos separados sin que el import cacheado los mezcle.
    """
    for nombre in [n for n in list(sys.modules) if n == "app" or n.startswith("app.")]:
        del sys.modules[nombre]
    sys.path.insert(0, str(arbol))
    sys.path.insert(0, str(arbol / "tests"))

    import stubs  # el del árbol evaluado  # noqa: E402
    stubs.stub_drivers()
    stubs.stub_web()
    main = stubs.importar_main()

    from app import db, openai_client
    pool = fake_db.instalar(db, _AQUI / "catalogo.json")
    cliente = None if sin_llm else fake_openai.instalar(openai_client, env, registro_llm)

    _silenciar_efectos(main)
    _encoder_aproximado(openai_client)
    return main, pool, cliente


def _encoder_aproximado(openai_client) -> None:
    """`tiktoken` está stubeado y su `encode` no existe.

    Se aproxima a 4 caracteres por token, que es la regla de bolsillo habitual
    para texto en español. Solo interviene en `fit_history`, y como la
    aproximación es la misma en los dos árboles, no sesga la comparación.
    """
    class _Encoder:
        def encode(self, texto):
            return [0] * (len(texto or "") // 4)

    openai_client._encoder = lambda: _Encoder()


def _silenciar_efectos(main) -> None:
    """Anula escrituras y envíos: la evaluación no toca WhatsApp ni la DB real.

    Se hace sobre los módulos que `main` importó, no sobre `main` mismo, para
    que también valga para el código que los llama por su cuenta.
    """
    from app import db, escalations, follow_ups, leads, whatsapp_client

    async def _nada(*a, **k):
        return None

    async def _falso(*a, **k):
        return False

    for modulo, funciones in (
        (db, ("save_message", "upsert_conversation_state", "set_bot_paused",
              "save_summary", "mark_processed", "clear_contact_data")),
        (whatsapp_client, ("send_text", "send_image", "send_images")),
        (escalations, ("registrar", "record_if_escalated")),
        (follow_ups, ("schedule", "cancel")),
    ):
        for nombre in funciones:
            if hasattr(modulo, nombre):
                setattr(modulo, nombre, _nada)
    db.get_conversation_state = _nada
    db.get_history = _nada
    db.get_lead = _nada
    db.get_summary = _nada
    db.is_bot_paused = _falso
    db.was_processed = _falso
    # `leads.process_reply` solo detecta datos de contacto en la respuesta; en la
    # evaluación devuelve el texto tal cual para no alterar lo que se compara.
    leads.process_reply = lambda wa_id, reply, history=None: _identidad(reply)


async def _identidad(valor):
    return valor


# ── Ejecución de un caso ──

async def responder(main, caso: dict, catalogo: list[dict], sin_llm: bool) -> dict:
    """Decisiones + texto final del ÚLTIMO turno del caso.

    Un caso puede traer `turnos` (varios mensajes del cliente encadenados). Los
    turnos previos se corren de verdad y su estado se arrastra igual que en
    producción, porque hay efectos que solo aparecen en el segundo turno: la
    primera respuesta a "algo para demorar" es una pregunta de calificación, y
    el atributo que el LLM detectó solo llega al SQL cuando el cliente contesta.
    Fijar ese estado a mano en el caso lo falsearía —cada versión persiste cosas
    distintas y es justo lo que se está midiendo.
    """
    historial = list(caso.get("historial") or [])
    estado = caso.get("estado")

    mensajes = list(caso.get("turnos") or [caso["mensaje"]])
    for previo in mensajes[:-1]:
        candidatos, info = await main._recuperar_candidatos(previo, historial, estado)
        texto, _ = await _redactar(main, caso, info, candidatos, historial, catalogo, sin_llm)
        historial = historial + [{"role": "user", "content": previo},
                                 {"role": "assistant", "content": texto or ""}]
        estado = _persistir_estado(estado, info, candidatos)

    mensaje = mensajes[-1]
    candidatos, info = await main._recuperar_candidatos(mensaje, historial, estado)

    decision = {
        "categoria_funcional": info.get("categoria_funcional"),
        "genero": info.get("genero"),
        "subtipo_detectado": info.get("subtipo_detectado"),
        "restricciones": info.get("restricciones") or {},
        "debe_mostrar": bool(info.get("debe_mostrar")),
        "escala": bool(info.get("sin_inventario") or info.get("sin_stock_subtipo")),
        "relajado": info.get("relajado"),
        "productos": [p["nombre"] for p in candidatos],
    }

    texto, rama = await _redactar(main, caso, info, candidatos, historial, catalogo, sin_llm)
    return {"decision": decision, "respuesta": texto, "rama": rama}


def _persistir_estado(estado: dict | None, info: dict, candidatos: list[dict]) -> dict:
    """Lo mismo que `db.upsert_conversation_state` guarda en `_handle_message`."""
    previo = dict(estado or {})
    mostrados = list(previo.get("productos_mostrados") or [])
    if info.get("debe_mostrar"):
        mostrados += [p["id"] for p in candidatos if p["id"] not in mostrados]
    return {
        "categoria_busqueda": info.get("intencion"),
        "categoria_funcional": info.get("categoria_funcional"),
        "genero": info.get("genero"),
        "calificado": bool(info.get("calificado")) or bool(mostrados),
        "productos_mostrados": mostrados,
        "restricciones": info.get("restricciones") or None,
        "texto_busqueda": info.get("texto_busqueda"),
        "preguntas_hechas": (previo.get("preguntas_hechas") or [])
        + ([(info.get("restricciones") or {}).get("tipo")]
           if info.get("pregunta_faceta") and (info.get("restricciones") or {}).get("tipo")
           else []),
    }


async def _redactar(main, caso, info, candidatos, historial, catalogo, sin_llm):
    """La misma prioridad de ramas que `main._handle_message`."""
    if info.get("sin_inventario") or info.get("sin_stock_subtipo"):
        return ("Déjame validar con el equipo si nos llegó algo nuevo que aún no tengo "
                "registrado 🙌 En un momentito se comunican contigo por aquí."), "handoff"
    if info.get("pide_numero_de_lista"):
        return main.PEDIR_NUMERO_DE_LISTA, "sistema:numero"
    if info.get("pregunta_faceta"):
        return info["pregunta_faceta"], "sistema:faceta"
    if info.get("categoria_agotada"):
        return main._texto_agotado(info), "sistema:agotado"
    pregunta = main._pregunta_de_calificacion(info)
    if pregunta:
        return pregunta, "sistema:calificacion"
    if info.get("debe_mostrar") and candidatos:
        return main._texto_desde_candidatos(candidatos, info, offset=0), "sistema:productos"

    if sin_llm:
        return None, "llm (omitido)"

    from app import catalog, openai_client
    producto_activo = None
    pista = caso.get("usar_producto_activo")
    if pista:
        coincide = next((p for p in catalogo if pista.lower() in (p["nombre"] or "").lower()), None)
        if not coincide:
            raise SystemExit(f"caso {caso['id']}: no hay producto que case con {pista!r}")
        producto_activo = await catalog.get_producto_by_id(coincide["id"])

    texto = await openai_client.complete(
        caso["mensaje"], historial,
        lead=None, summary=None,
        candidatos=candidatos if info.get("debe_mostrar") else [],
        estado={
            "categoria_busqueda": info.get("intencion"),
            "categoria_funcional": info.get("categoria_funcional"),
            "genero": info.get("genero"),
            "calificado": info.get("calificado"),
            "categoria_agotada": info.get("categoria_agotada", False),
            "sin_mas_opciones": info.get("sin_mas_opciones", False),
            "hay_mas": info.get("hay_mas", False),
            "total_en_categoria": info.get("total_en_categoria", 0),
            "productos_mostrados": [producto_activo["id"]] if producto_activo else [],
            "productos_con_precios": "",
        },
        debe_mostrar_fotos=bool(info.get("debe_mostrar")),
        **({"producto_activo": producto_activo} if _acepta_producto_activo(openai_client) else {}),
    )
    return texto, "llm"


def _acepta_producto_activo(openai_client) -> bool:
    """La versión base (14391dc) no tiene ese parámetro; pasarlo la reventaría."""
    import inspect
    return "producto_activo" in inspect.signature(openai_client.complete).parameters


# ── Comandos ──

def ejecutar(args) -> int:
    arbol = Path(args.arbol).resolve()
    env = snapshot_catalogo.cargar_env()
    registro = Path(args.registro_llm) if args.registro_llm else None
    if registro and registro.exists():
        registro.unlink()

    casos = json.loads((_AQUI / "casos.json").read_text())
    if args.caso:
        casos = [c for c in casos if c["id"] in args.caso]
        if not casos:
            raise SystemExit(f"ningún caso con id en {args.caso}")

    logging.disable(logging.CRITICAL)
    main, pool, cliente = montar(arbol, env, registro, args.sin_llm)
    catalogo = pool.filas

    resultados = {}
    for caso in casos:
        intentos = 1 if args.sin_llm else args.repeticiones
        corridas = []
        for _ in range(intentos):
            # El código bajo prueba escribe a stdout/stderr por su cuenta; se
            # captura para que el informe salga limpio.
            buf = io.StringIO()
            with redirect_stdout(buf), redirect_stderr(buf):
                corridas.append(asyncio.run(responder(main, caso, catalogo, args.sin_llm)))
        resultados[caso["id"]] = {
            "caso": caso,
            "decision": corridas[0]["decision"],
            "rama": corridas[0]["rama"],
            "respuestas": [c["respuesta"] for c in corridas],
        }
        d = corridas[0]["decision"]
        print(f"  {caso['id']:22} {corridas[0]['rama']:22} "
              f"n={len(d['productos']):2} escala={d['escala']}")

    salida = Path(args.salida)
    salida.write_text(json.dumps(
        {"arbol": str(arbol), "sin_llm": args.sin_llm, "resultados": resultados},
        ensure_ascii=False, indent=1))
    print(f"\nEscrito: {salida}"
          + (f"  ({cliente.llamadas} llamadas al modelo)" if cliente else ""))
    return 0


def _productos_esperados(caso: dict, nombres: list[str]) -> tuple[int, int]:
    """(aciertos, total) contra las pistas de `productos_correctos`."""
    pistas = caso.get("productos_correctos")
    if pistas is None:
        return (0, 0)
    if not pistas:  # se espera que NO devuelva nada
        return (0 if nombres else 1, 1)
    aciertos = sum(1 for n in nombres
                   if any(p.lower() in n.lower() for p in pistas))
    return (aciertos, len(nombres) or 1)


def _evaluar(caso: dict, dato: dict) -> tuple[str, str]:
    """(veredicto, detalle) de un caso contra lo que se esperaba de él.

    Cada caso declara solo lo que le importa: la categoría, si debe preguntar en
    vez de mostrar, si debe escalar, o qué productos son aceptables. Un caso sin
    expectativas queda en `revisar`, que es honesto: nadie ha dicho todavía qué
    es lo correcto ahí.
    """
    decision = dato["decision"]
    rama = dato["rama"]
    fallos = []

    esperada = caso.get("categoria_esperada")
    if esperada and decision["categoria_funcional"] != esperada:
        fallos.append(f"categoría {decision['categoria_funcional']!r} ≠ {esperada!r}")

    if caso.get("debe_preguntar") and "calificacion" not in rama:
        fallos.append(f"debía preguntar y fue a {rama}")

    if caso.get("debe_escalar") is not None:
        if decision["escala"] != caso["debe_escalar"]:
            fallos.append("escaló sin deber" if decision["escala"] else "no escaló")

    pistas = caso.get("productos_correctos")
    nombres = decision["productos"]
    if pistas == []:
        if nombres:
            fallos.append(f"no debía mostrar productos y mostró {len(nombres)}")
    elif pistas:
        malos = [n for n in nombres
                 if not any(p.lower() in n.lower() for p in pistas)]
        if malos:
            fallos.append(f"{len(malos)}/{len(nombres)} fuera de lo esperado: {malos[0][:40]!r}")

    if fallos:
        return "FALLA", "; ".join(fallos)
    tiene_criterio = any(caso.get(k) is not None for k in
                         ("categoria_esperada", "debe_preguntar", "debe_escalar",
                          "productos_correctos"))
    return ("ok", "") if tiene_criterio else ("revisar", caso.get("nota", ""))


def informe(args) -> int:
    """Puntúa una corrida contra las expectativas declaradas en los casos."""
    datos = json.loads(Path(args.informe).read_text())["resultados"]
    conteo = {"ok": 0, "FALLA": 0, "revisar": 0}
    por_grupo: dict[str, dict[str, int]] = {}
    fallas = []
    for cid, dato in datos.items():
        caso = dato["caso"]
        veredicto, detalle = _evaluar(caso, dato)
        conteo[veredicto] += 1
        grupo = caso.get("grupo", "?")
        por_grupo.setdefault(grupo, {"ok": 0, "FALLA": 0, "revisar": 0})[veredicto] += 1
        if veredicto == "FALLA":
            fallas.append((grupo, cid, caso["mensaje"], detalle))

    print(f"{'grupo':14} {'ok':>4} {'falla':>6} {'revisar':>8}")
    print("-" * 36)
    for grupo, c in sorted(por_grupo.items()):
        print(f"{grupo:14} {c['ok']:4} {c['FALLA']:6} {c['revisar']:8}")
    print("-" * 36)
    print(f"{'TOTAL':14} {conteo['ok']:4} {conteo['FALLA']:6} {conteo['revisar']:8}")

    if fallas:
        print(f"\n{len(fallas)} fallos:")
        for grupo, cid, mensaje, detalle in fallas:
            print(f"  [{grupo}] {cid}: {mensaje!r}\n      {detalle}")
    return 0


def comparar(args) -> int:
    base = json.loads(Path(args.comparar[0]).read_text())["resultados"]
    nuevo = json.loads(Path(args.comparar[1]).read_text())["resultados"]

    print(f"{'caso':22} {'grupo':10} {'base':>18} {'nuevo':>18}  veredicto")
    print("-" * 88)
    conteo = {"mejor": 0, "peor": 0, "igual": 0}
    for cid, dn in nuevo.items():
        db_ = base.get(cid)
        if not db_:
            continue
        caso = dn["caso"]
        an, tn = _productos_esperados(caso, dn["decision"]["productos"])
        ab, tb = _productos_esperados(caso, db_["decision"]["productos"])
        precision_n = an / tn if tn else None
        precision_b = ab / tb if tb else None

        veredicto = "igual"
        detalle = []
        if caso.get("debe_escalar") is not None:
            esperado = caso["debe_escalar"]
            ok_n = dn["decision"]["escala"] == esperado
            ok_b = db_["decision"]["escala"] == esperado
            if ok_n and not ok_b:
                veredicto, _ = "mejor", detalle.append("escalado corregido")
            elif ok_b and not ok_n:
                veredicto, _ = "peor", detalle.append("escalado roto")
        if precision_n is not None and precision_b is not None and veredicto == "igual":
            if precision_n > precision_b:
                veredicto = "mejor"
            elif precision_n < precision_b:
                veredicto = "peor"
        if veredicto == "igual" and dn["decision"]["productos"] != db_["decision"]["productos"]:
            detalle.append("mismos aciertos, distinto conjunto")

        conteo[veredicto] += 1
        fmt = lambda a, t, d: (f"{a}/{t} ac." if t else "—") + f" [{d['rama'].split(':')[0]}]"
        print(f"{cid:22} {caso.get('grupo',''):10} "
              f"{fmt(ab, tb, db_):>18} {fmt(an, tn, dn):>18}  {veredicto}"
              + (f"  ({', '.join(detalle)})" if detalle else ""))

    print("-" * 88)
    print(f"mejor: {conteo['mejor']}   igual: {conteo['igual']}   peor: {conteo['peor']}")
    return 0


def main_cli() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--arbol", help="raíz del repo a evaluar (por defecto, esta)")
    p.add_argument("--salida", help="JSON con los resultados")
    p.add_argument("--sin-llm", action="store_true",
                   help="solo la capa de decisiones: gratis, determinista")
    p.add_argument("--repeticiones", type=int, default=3,
                   help="corridas por caso (el modelo no es determinista)")
    p.add_argument("--caso", nargs="*", help="ids concretos a correr")
    p.add_argument("--registro-llm", help="JSONL con cada petición y respuesta")
    p.add_argument("--comparar", nargs=2, metavar=("BASE", "NUEVO"))
    p.add_argument("--informe", metavar="RESULTADOS",
                   help="puntúa una corrida contra lo que declara cada caso")
    args = p.parse_args()

    if args.informe:
        return informe(args)
    if args.comparar:
        return comparar(args)
    if not args.arbol or not args.salida:
        p.error("hacen falta --arbol y --salida (o --comparar)")
    return ejecutar(args)


if __name__ == "__main__":
    sys.exit(main_cli())
