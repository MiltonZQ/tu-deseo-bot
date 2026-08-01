# Despliegue de los fixes de clasificación de categorías — Plan

> **Para agentes:** SUB-SKILL REQUERIDA: usa `superpowers:subagent-driven-development` (recomendado) o `superpowers:executing-plans` para ejecutar tarea por tarea. Los pasos usan checkbox (`- [ ]`) para seguimiento.

**Goal:** Llevar a producción tres correcciones ya implementadas y testeadas localmente (categorías secuestradas por la descripción, categoría pegada entre turnos, productos al azar), con una puerta de revisión antes del build y una vía de rollback probada.

**Architecture:** Commit y push directo a `main`. El proyecto está en fase de **prototipo** (un solo desarrollador, sin usuarios reales), así que un Pull Request no aporta puerta de revisión: no hay segundo revisor y el fix hay que aplicarlo igual. La red de seguridad real es que la imagen anterior sigue publicada en GHCR y el rollback es cambiar un tag (Tarea 6), lo cual no depende del PR. El push dispara `.github/workflows/build-and-publish.yml`, que publica `ghcr.io/miltonzq/tu-deseo-bot` con tags `latest`, `sha-<commit>` y `<YYYYMMDD>`. El despliegue en Coolify lo ejecuta el usuario (no hay MCP de Coolify configurado en este proyecto).

> Si este repo pasa a tener usuarios reales, revisar esta decisión: ahí sí conviene la rama + PR, porque el push a `main` publica `latest` sin ninguna puerta intermedia.

**Tech Stack:** Python 3.12 / FastAPI, PostgreSQL (asyncpg), Qdrant (búsqueda semántica), Redis, yCloud (WhatsApp), GitHub Actions → GHCR, Coolify.

## Global Constraints

- Código, comentarios y mensajes de commit **en español** (convención del repo, ver `AGENTS.md`).
- **Nunca** commitear `.env`, `.mcp.json` ni `execution/`. Ejecutar un escaneo de secretos antes de cada `git add` (obligatorio por `AGENTS.md`).
- No ejecutar acciones en Coolify: no hay acceso desde este entorno. Los pasos de despliegue los realiza el usuario y el agente solo verifica el resultado por HTTP/logs.
- Commit base actual (punto de rollback): `815e8d83a6510c2016cae6ce01c481b33de270fe`.
- Rama por defecto: `main`. Remote: `https://github.com/MiltonZQ/tu-deseo-bot.git`.
- No hay `pytest` instalado en `.venv` ni `pip` disponible. Los tests se ejecutan con el runner manual documentado en la Tarea 1.

---

## Estado de partida

Los cambios **ya están escritos** en el working tree; este plan NO los reimplementa, los publica. Archivos modificados:

| Archivo | Qué cambió |
|---|---|
| `app/catalog.py` | `_categoria_normalizada` evalúa el nombre primero (nueva función auxiliar `_aplicar_reglas_categoria`); el motor de memoria detecta cambio de tema con `cat_desde_mensaje` en vez de la lista fija `sustantivo_cambio_tema`; el Intento E-bis no relaja la categoría si hay un subtipo concreto (`subtipo_hard`). |
| `app/main.py` | `_handle_message` solo fuerza fotos si el texto promete productos (`promete_productos`); `_enviar_fotos_productos` aísla el `try/except` por foto y loguea nombre/id/URL del fallo. |
| `tests/test_calificacion_categorias.py` | `test_bug16_motor_memoria_existe_en_clasificador` actualizado al nuevo mecanismo. |
| `tests/test_categoria_pegada_y_fotos.py` | **Nuevo.** 14 tests que ejecutan el código real contra un pool de DB falso. |

Riesgo principal a vigilar: el cambio de clasificación mueve **32 de 246 productos (13%)** de categoría. Es la corrección buscada, pero es el cambio con mayor radio de impacto del lote.

---

### Task 1: Higiene previa y verificación local

**Files:**
- Revisar: `app/catalog.py`, `app/main.py`, `tests/test_categoria_pegada_y_fotos.py`
- Modificar: `.gitignore`

**Interfaces:**
- Consume: nada (primera tarea).
- Produce: working tree limpio de artefactos, suite verde (147 tests, 1 fallo preexistente conocido), `.claude/` ignorado.

- [ ] **Step 1: Revisar el diff completo antes de tocar nada**

```bash
git diff
git status --short
```

Esperado: modificados `app/catalog.py`, `app/main.py`, `tests/test_calificacion_categorias.py`; sin trackear `.claude/` y `tests/test_categoria_pegada_y_fotos.py`. Si aparece cualquier otro archivo, detenerse y averiguar por qué antes de continuar.

- [ ] **Step 2: Escaneo de secretos (obligatorio por AGENTS.md)**

```bash
git diff | grep -inE "(api[_-]?key|secret|token|password|passwd|bearer|sk-[a-z0-9]{20})" || echo "SIN SECRETOS EN EL DIFF"
grep -rn "YCLOUD_API_KEY\|OPENAI_API_KEY\|DATABASE_URL" app/catalog.py app/main.py tests/test_categoria_pegada_y_fotos.py || echo "SIN SECRETOS EN LOS ARCHIVOS"
```

Esperado: ambas líneas imprimen el mensaje "SIN SECRETOS…". Si algo aparece, **parar** y limpiarlo antes de seguir.

- [ ] **Step 3: Ignorar `.claude/` para que no entre en el commit**

`.claude/` son skills locales del entorno de trabajo, no código del bot. Meterlas en este commit ensucia un diff que se va a revisar. Añadir al final de `.gitignore`, después del bloque `# IDE`:

```gitignore
.claude/
```

- [ ] **Step 4: Confirmar que `.claude/` ya no aparece como archivo sin trackear**

```bash
git status --short
```

Esperado: ya NO aparece `?? .claude/`. Sigue apareciendo `?? tests/test_categoria_pegada_y_fotos.py` (ese sí va al commit).

- [ ] **Step 5: Ejecutar la suite completa**

No hay `pytest` en el entorno; este runner importa cada módulo de tests y llama a las funciones `test_*`:

```bash
PYTHONPATH=. .venv/bin/python -c "
import importlib
fallos=[]; total=0
for mod in ('tests.test_calificacion_categorias','tests.test_filtrado_subtipos',
            'tests.test_pipeline_robustez','tests.test_categoria_pegada_y_fotos'):
    m=importlib.import_module(mod)
    for n in dir(m):
        if n.startswith('test_') and callable(getattr(m,n)):
            total+=1
            try: getattr(m,n)()
            except Exception: fallos.append(f'{mod.split(\".\")[-1]}::{n}')
print('total:',total,'fallos:',len(fallos))
[print(' -',f) for f in fallos]
" 2>&1 | grep -v "Clasificador LLM\|cache de palabras\|candidatos tras todos"
```

Esperado, exactamente:

```
total: 147 fallos: 1
 - test_filtrado_subtipos::test_funda_pene_no_mezcla_masturbadores
```

Ese único fallo es **preexistente** (ya fallaba en `815e8d8`, verificado con `git stash`): el test afirma claves que nunca existieron en el código (`"funda para el pene"` en `_INTENCION_A_CATEGORIA_FUNCIONAL`, `"funda"` en `_SUBTIPO_KEYWORDS`). Se aborda en la Tarea 6, fuera de este despliegue. Si aparece **cualquier otro** fallo, parar: es una regresión introducida.

- [ ] **Step 6: Verificar que los módulos compilan**

```bash
.venv/bin/python -c "import ast; [ast.parse(open(f).read()) for f in ['app/catalog.py','app/main.py']]; print('sintaxis OK')"
```

Esperado: `sintaxis OK`.

---

### Task 2: Commit en rama y Pull Request

**Files:**
- Commit: `app/catalog.py`, `app/main.py`, `tests/test_calificacion_categorias.py`, `tests/test_categoria_pegada_y_fotos.py`, `.gitignore`

**Interfaces:**
- Consume: working tree verificado de la Tarea 1.
- Produce: rama `fix/clasificacion-categorias-pegadas` en `origin` y un PR abierto. **No** dispara el build (el workflow solo corre en push a `main`).

- [ ] **Step 1: Crear la rama**

```bash
git checkout -b fix/clasificacion-categorias-pegadas
```

Esperado: `Switched to a new branch 'fix/clasificacion-categorias-pegadas'`.

- [ ] **Step 2: Añadir exactamente los archivos previstos**

```bash
git add app/catalog.py app/main.py tests/test_calificacion_categorias.py tests/test_categoria_pegada_y_fotos.py .gitignore
git status --short
```

Esperado: cinco archivos en estado `M`/`A`. Nada más en el índice.

- [ ] **Step 3: Commit**

```bash
git commit -m "$(cat <<'EOF'
fix: 3 causas raíz de recomendaciones equivocadas (categoría, memoria, relajación)

1. La DESCRIPCIÓN secuestraba la categoría del producto.
   _categoria_normalizada mezclaba nombre + descripción + categoría origen en un
   solo texto, y una palabra de la descripción disparaba una regla anterior:
   - "Bomba Automática Bender Optimus Pro" → su descripción dice "sistema
     eléctrico de succión" → quedaba como succionador y no salía en "bombas".
   - "Succionador Nyla Fuscia" → "Compatible con lubricantes a base de agua" →
     quedaba como lubricante y era el único succionador que no se mostraba.
   Ahora el nombre manda y la descripción solo se consulta si el nombre no da
   señal. Corrige 32 de 246 productos del catálogo (13%).

2. La categoría del primer turno se quedaba pegada toda la conversación.
   El motor de memoria solo aceptaba cambio de tema con una lista fija de 15
   sustantivos que omitía lubricante, látigo, plug, arnés, kit, masturbador y
   disfraz. El cliente pedía látigos y lubricantes y seguía recibiendo bombas.
   Ahora manda la categoría que salga del mensaje actual; se conserva la
   excepción de "anal/agua/silicona/sabores" como filtro dentro de lubricantes.

3. Salían productos al azar por dos vías:
   - El Intento E-bis relajaba la categoría por género ignorando el subtipo
     pedido (látigos → anillos vibradores). Ya no relaja con subtipo concreto.
   - _handle_message forzaba las fotos de los candidatos aunque la respuesta no
     ofreciera productos, así que el mensaje de escalado salía con fotos
     pegadas detrás. Ahora solo se fuerzan si el texto promete productos.

Además: el try/except de _enviar_fotos_productos envolvía el bucle entero, así
que la primera imagen que fallara cancelaba en silencio todas las restantes.
Ahora se aísla por foto y se loguea nombre, id y URL del fallo.

Tests: 14 nuevos en tests/test_categoria_pegada_y_fotos.py que ejecutan el
código real contra un pool de DB falso. Suite: 147 tests, 1 fallo preexistente
(test_funda_pene_no_mezcla_masturbadores, ya rojo en 815e8d8).

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Push de la rama**

```bash
git push -u origin fix/clasificacion-categorias-pegadas
```

Esperado: la rama se crea en `origin`. **Verificar que NO se disparó ningún workflow** (el trigger es solo `main`):

```bash
gh run list --limit 3
```

Esperado: no hay ninguna ejecución nueva para esta rama.

- [ ] **Step 5: Abrir el PR**

```bash
gh pr create --base main --title "fix: 3 causas raíz de recomendaciones equivocadas del bot" --body "$(cat <<'EOF'
## Qué corrige

Cuatro fallos reportados en la conversación de prueba del 2026-08-01, que resultaron ser **tres causas raíz**:

1. **La descripción secuestraba la categoría.** `_categoria_normalizada` mezclaba nombre + descripción + categoría origen. "Bomba Bender Optimus Pro" caía en `succionadores` por la frase "sistema eléctrico de succión" de su descripción; "Succionador Nyla" caía en `lubricantes-y-cuidado` por "Compatible con lubricantes a base de agua". Eran exactamente los dos productos que el cliente reportó que nunca aparecían. **Afecta 32 de 246 productos (13%).**
2. **La categoría se quedaba pegada entre turnos.** El motor de memoria detectaba cambio de tema con una lista fija de 15 sustantivos que no incluía lubricante, látigo, plug, arnés, kit ni masturbador. El cliente preguntaba por látigos y por lubricantes de sabores, y seguía recibiendo bombas para el pene del primer turno.
3. **Productos al azar.** El Intento E-bis relajaba la categoría por género ignorando el subtipo pedido, y `_handle_message` forzaba fotos aunque la respuesta no ofreciera productos (por eso el mensaje "Déjame confirmar con el equipo…" salía con dos anillos pegados detrás).

Extra: el `try/except` de `_enviar_fotos_productos` envolvía el bucle entero — la primera foto que fallara cancelaba en silencio las restantes. Ahora se aísla por foto y se loguea el fallo con nombre, id y URL.

## Verificación

- 14 tests nuevos que **ejecutan el código real** (pool de DB falso), cubriendo cada bug y las no-regresiones del motor de memoria y del Intento E-bis.
- Suite completa: **147 tests, 1 fallo preexistente** (`test_funda_pene_no_mezcla_masturbadores`, ya rojo en 815e8d8 — afirma claves que nunca existieron en el código).

## Al desplegar

- Qdrant guarda `categoria_funcional` en el momento de indexar, así que el índice queda desactualizado hasta que `sync_qdrant_from_db()` corre al arrancar la app. Se corrige solo con el redeploy, pero tarda.
- El fallo de fotos del Lovense Lush **no está confirmado**: el log nuevo existe para diagnosticarlo en la próxima ocurrencia.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Esperado: imprime la URL del PR. Guardarla.

---

### Task 3: Merge a `main` y verificación del build

**Files:** ninguno (operación de repo/CI).

**Interfaces:**
- Consume: PR abierto de la Tarea 2.
- Produce: `main` actualizado, workflow verde, imagen `ghcr.io/miltonzq/tu-deseo-bot` publicada con tags `latest`, `sha-<commit>` y `<YYYYMMDD>`.

> **Puerta de decisión.** Este es el punto sin retorno barato: el merge dispara el build y publica `latest`. Si Coolify tiene auto-deploy por webhook, **producción cambia aquí**. Confirmar con el usuario antes de ejecutar el Step 1.

- [ ] **Step 1: Revisar el diff del PR una última vez**

```bash
gh pr diff
```

Esperado: solo los cinco archivos previstos. Sin `.env`, sin `.claude/`, sin `__pycache__`.

- [ ] **Step 2: Merge a `main`**

```bash
gh pr merge --squash --delete-branch
git checkout main && git pull
```

Esperado: PR mergeado, rama local de vuelta en `main` con el commit nuevo.

- [ ] **Step 3: Seguir el build hasta que termine**

```bash
gh run watch $(gh run list --workflow=build-and-publish.yml --limit 1 --json databaseId --jq '.[0].databaseId')
```

Esperado: `build-and-push` en verde. Si falla, leer el log con `gh run view --log-failed` y **no** desplegar.

- [ ] **Step 4: Anotar el SHA de la imagen nueva y el de rollback**

```bash
echo "IMAGEN NUEVA:   ghcr.io/miltonzq/tu-deseo-bot:sha-$(git rev-parse HEAD)"
echo "ROLLBACK A:     ghcr.io/miltonzq/tu-deseo-bot:sha-815e8d83a6510c2016cae6ce01c481b33de270fe"
```

Guardar ambas líneas: la segunda es el plan de rollback de la Tarea 5.

---

### Task 4: Despliegue en Coolify y verificación de arranque

**Files:** ninguno (operación de infraestructura, la ejecuta el usuario).

**Interfaces:**
- Consume: imagen publicada de la Tarea 3.
- Produce: app corriendo con el código nuevo y Qdrant reindexado.

- [ ] **Step 1: Desplegar (usuario)**

En Coolify: abrir el recurso del bot y pulsar **Redeploy** para que traiga la imagen `latest` recién publicada. Si el recurso está fijado a un tag concreto, cambiarlo al `sha-…` anotado en la Tarea 3, Step 4.

- [ ] **Step 2: Verificar que la app arrancó**

```bash
curl -s https://<WEBHOOK_DOMAIN>/health
```

Esperado: `{"status":"ok","business":"..."}`. Sustituir `<WEBHOOK_DOMAIN>` por el dominio real configurado en Coolify.

- [ ] **Step 3: Confirmar que Qdrant terminó de reindexar**

En los logs del contenedor (Coolify → Logs), buscar la línea:

```
Indexados N productos en Qdrant desde DB (filtrados con imagen)
```

Esperado: aparece con `N` > 200. **Hasta que salga esa línea, la búsqueda semántica sigue devolviendo las categorías viejas** y los smoke tests de la Tarea 5 darán falsos negativos. Es la razón por la que este paso va antes de probar.

- [ ] **Step 4: Verificar el handshake del webhook**

```bash
curl -s https://<WEBHOOK_DOMAIN>/webhook
```

Esperado: `{"status":"ok","provider":"ycloud"}`.

---

### Task 5: Smoke test de los cuatro fallos reportados

**Files:** ninguno (pruebas manuales por WhatsApp).

**Interfaces:**
- Consume: app desplegada y Qdrant reindexado (Tarea 4).
- Produce: confirmación en producción de cada fix, o el disparador del rollback de la Tarea 6.

- [ ] **Step 1: Limpiar el estado del contacto de prueba**

Crítico y fácil de olvidar: el contacto de prueba tiene `conversation_state.categoria_funcional = "bombas-pene"` guardado del bug anterior. Sin limpiarlo, el primer turno arranca contaminado y la prueba no mide nada.

```bash
curl -s -X POST "https://<WEBHOOK_DOMAIN>/maintenance/reset-contact?wa_id=<NUMERO_DE_PRUEBA>" \
  -H "X-Reload-Token: <RELOAD_TOKEN>"
```

Esperado: `{"cleared_wa_ids":["<NUMERO>"],"deleted":...}`. El `RELOAD_TOKEN` está en las variables de entorno de Coolify — no imprimirlo en la conversación.

- [ ] **Step 2: Fallo 1 — las dos bombas**

Enviar por WhatsApp: `tienen bombas para el pene`

Esperado: la lista incluye **Bomba Para El Pene Hefesto** ($180.000) **y Bomba Automática para Pene Recargable Bender Optimus Pro** ($340.000). Antes solo salía el Hefesto.

- [ ] **Step 3: Fallo 2 — el succionador que faltaba**

Enviar: `quiero ver succionadores`

Esperado: entre las opciones aparece **Succionador Con Ondas Y Vibracion Nyla Fuscia** ($120.000). Era el único succionador que nunca se mostraba.

- [ ] **Step 4: Fallo 3 — la categoría ya no se queda pegada**

Enviar los cuatro mensajes **en este orden, esperando la respuesta de cada uno**:

1. `tienen bombas para el pene` → muestra bombas.
2. `me gustaria ver si tinen latigos` → mensaje de escalado (no hay látigos sueltos en catálogo, solo dentro del Kit Bondage Fiore). **Verificar que NO llega ninguna foto detrás.** Antes llegaban dos anillos vibradores al azar.
3. `puedo ver que lubricantes manejan` → pregunta de calificación de lubricantes, **sin foto de la bomba pegada**.
4. `sabores` → muestra **lubricantes de sabores** (Blix cereza/chocolate/fresa/maracuyá/sandía, Sen menta). Antes llegaban anillos vibradores.

- [ ] **Step 5: Fallo 4 — fotos del Lovense Lush**

Enviar: `tienen lovense lush`

Esperado: el texto lista los 3 Lush y **llegan las 3 fotos**. Este fix no está confirmado, así que revisar los logs del contenedor en cualquier caso:

```
Fotos a <wa_id>: N enviadas, M fallidas, de K candidatos
```

Si `M > 0`, buscar la línea `Error enviando foto de '<nombre>' (id=… url=…)`: ahí está la URL exacta que WhatsApp rechaza, que es el dato que faltaba para diagnosticarlo.

- [ ] **Step 6: Registrar el resultado**

Anotar cuáles de los cinco pasos pasaron. Si falla el 2, 3 o 4 → ir a la Tarea 6 (rollback). Si solo falla el 5 (fotos del Lush) → **no** hacer rollback: es un fallo preexistente no resuelto por este lote, y ahora queda diagnosticable.

---

### Task 6: Rollback (solo si la Tarea 5 falla)

**Files:** ninguno (operación de infraestructura + repo).

**Interfaces:**
- Consume: SHA de rollback anotado en la Tarea 3, Step 4.
- Produce: producción de vuelta en el comportamiento de `815e8d8`.

- [ ] **Step 1: Rollback inmediato de producción (usuario, < 2 min)**

En Coolify, cambiar la imagen del recurso a:

```
ghcr.io/miltonzq/tu-deseo-bot:sha-815e8d83a6510c2016cae6ce01c481b33de270fe
```

y redesplegar. Esta imagen ya está publicada en GHCR: no depende de ningún build nuevo, por eso es la vía rápida.

- [ ] **Step 2: Verificar que volvió**

```bash
curl -s https://<WEBHOOK_DOMAIN>/health
```

Esperado: `{"status":"ok",...}`.

- [ ] **Step 3: Revertir el commit en `main`**

```bash
git checkout main && git pull
git revert --no-edit HEAD
git push
```

Esto dispara un build nuevo que deja `latest` apuntando al código viejo, para que un redeploy accidental no reintroduzca el fallo.

- [ ] **Step 4: Reproducir el fallo localmente antes de reintentar**

Volver a la skill `systematic-debugging` con el síntoma concreto observado en la Tarea 5. **No** intentar un segundo fix sin haber reproducido primero.

---

### Task 7: Seguimiento (después del despliegue, no bloquea)

**Files:**
- Modificar: `tests/test_filtrado_subtipos.py:144-161`
- Modificar: `app/catalog.py` (`_INTENCION_A_CATEGORIA_FUNCIONAL`)

**Interfaces:**
- Consume: despliegue estable confirmado.
- Produce: suite 100% verde.

- [ ] **Step 1: Decidir qué es correcto en `test_funda_pene_no_mezcla_masturbadores`**

El test afirma dos cosas que nunca existieron en el código:

```python
assert _INTENCION_A_CATEGORIA_FUNCIONAL.get("funda para el pene") == "fundas-pene"
assert subtipo in ("funda", "fundas")   # "funda" no está en _SUBTIPO_KEYWORDS
```

El comportamiento real ya es correcto por coincidencia de subcadena (`_intencion_desde_texto` encuentra `"funda"` dentro de `"funda para el pene"`). Hay dos salidas y hay que elegir una con el usuario: (a) añadir la clave `"funda para el pene": "fundas-pene"` al mapa para que el test valga, o (b) corregir el test para que afirme el comportamiento real. La opción (b) es la de menor riesgo: no cambia nada en producción.

- [ ] **Step 2: Aplicar la opción elegida y correr la suite**

```bash
PYTHONPATH=. .venv/bin/python -c "
import importlib
f=0;t=0
for mod in ('tests.test_calificacion_categorias','tests.test_filtrado_subtipos',
            'tests.test_pipeline_robustez','tests.test_categoria_pegada_y_fotos'):
    m=importlib.import_module(mod)
    for n in dir(m):
        if n.startswith('test_') and callable(getattr(m,n)):
            t+=1
            try: getattr(m,n)()
            except Exception: f+=1; print('FALLA',n)
print('total:',t,'fallos:',f)
" 2>&1 | grep -v "Clasificador LLM\|cache de palabras\|candidatos tras todos"
```

Esperado: `total: 147 fallos: 0`.

- [ ] **Step 3: Instalar pytest en el entorno para no depender del runner manual**

`.venv` no tiene `pip` ni `pytest`, por eso toda la suite se corre con un one-liner. Vale la pena arreglarlo:

```bash
.venv/bin/python -m ensurepip --upgrade && .venv/bin/python -m pip install -q pytest
PYTHONPATH=. .venv/bin/python -m pytest tests/ -q
```

Esperado: `147 passed`.

- [ ] **Step 4: Commit**

```bash
git add tests/test_filtrado_subtipos.py
git commit -m "test: corregir aserción de fundas que afirmaba claves inexistentes

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Resumen de la decisión

**Push directo a `main`.** El proyecto está en prototipo: no hay usuarios reales ni un segundo revisor, y el fix hay que aplicarlo de todas formas. Un PR sería ceremonia sin puerta real. Lo que sí importa es que el rollback esté listo y probado — la imagen anterior (`sha-815e8d83…`) sigue publicada en GHCR y volver a ella es cambiar un tag en Coolify (Tarea 6).

Las tres consideraciones planteadas quedan cubiertas así:

| Consideración | Dónde se maneja |
|---|---|
| Reindexado lento de Qdrant | Tarea 4, Step 3: se espera la línea de log antes de probar, para que los smoke tests no den falsos negativos. |
| Bug del Lush sin confirmar | Tarea 5, Step 5: se prueba y se leen los logs nuevos; **explícitamente no** dispara rollback, porque es un fallo preexistente. |
| 1 test rojo preexistente | Tarea 1, Step 5: se documenta como esperado, con la evidencia de que ya fallaba en `815e8d8`. Se arregla aparte en la Tarea 7. |
