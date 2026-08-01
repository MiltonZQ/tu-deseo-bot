# Despliegue de los fixes de clasificación de categorías — Plan

> **Para agentes:** SUB-SKILL REQUERIDA: usa `superpowers:subagent-driven-development` (recomendado) o `superpowers:executing-plans` para ejecutar tarea por tarea. Los pasos usan checkbox (`- [ ]`) para seguimiento.

**Goal:** Llevar al servidor tres correcciones ya implementadas y testeadas localmente (categorías secuestradas por la descripción, categoría pegada entre turnos, productos al azar), verificando el arranque y con una vía de rollback documentada.

**Architecture:** Commit y push directo a `main`. El proyecto está en fase de **prototipo** (un solo desarrollador, sin usuarios reales), así que un Pull Request no aporta puerta de revisión: no hay segundo revisor y el fix hay que aplicarlo igual. La red de seguridad real es que la imagen anterior sigue publicada en GHCR y el rollback es cambiar un tag (Tarea 6), lo cual no depende del PR. El push dispara `.github/workflows/build-and-publish.yml`, que publica la imagen en GHCR, **pero esa imagen no es lo que corre en el servidor**: la app de Coolify construye desde el repo con su propio Dockerfile (`build_pack: dockerfile`, `git_commit_sha: HEAD`). El despliegue se lanza por la API de Coolify con las credenciales de `.env` (`COOLIFY_URL`, `COOLIFY_TOKEN`).

> Si este repo pasa a tener usuarios reales, revisar esta decisión: ahí sí conviene la rama + PR, porque el push a `main` publica `latest` sin ninguna puerta intermedia.

**Tech Stack:** Python 3.12 / FastAPI, PostgreSQL (asyncpg), Qdrant (búsqueda semántica), Redis, yCloud (WhatsApp), GitHub Actions → GHCR, Coolify.

## Global Constraints

- Código, comentarios y mensajes de commit **en español** (convención del repo, ver `AGENTS.md`).
- **Nunca** commitear `.env`, `.mcp.json` ni `execution/`. Ejecutar un escaneo de secretos antes de cada `git add` (obligatorio por `AGENTS.md`).
- Coolify se opera por su API REST con las credenciales de `.env` (`COOLIFY_URL`, `COOLIFY_TOKEN`). Helper: `scripts/coolify.py`. App uuid: `jkscan3y83awkmuuxzt8i3wc`. Dominio: `https://tu-deseo.autozb.com`.
- El `COOLIFY_TOKEN` contiene un `|`: **no** cargar `.env` con `source`/`set -a` (el shell lo interpreta como pipe y deja la variable vacía). Usar `scripts/coolify.py`, que lo parsea correctamente.
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

### Task 2: Commit y push a `main`

**Files:**
- Commit: `app/catalog.py`, `app/main.py`, `tests/test_calificacion_categorias.py`, `tests/test_categoria_pegada_y_fotos.py`, `.gitignore`

**Interfaces:**
- Consume: working tree verificado de la Tarea 1.
- Produce: commit en `main` empujado a `origin`. Dispara el workflow de GHCR (que publica una imagen que este despliegue no usa) y deja `main` listo para que Coolify construya desde `HEAD`.

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
git push origin main
```

Esperado: `815e8d8..625a64d  main -> main`.

- [ ] **Step 5: Confirmar que el build de GHCR quedó verde**

El workflow no bloquea el despliegue (Coolify construye por su cuenta), pero un build rojo delata un problema en el `Dockerfile` que también rompería el despliegue:

```bash
gh run watch $(gh run list --workflow=build-and-publish.yml --limit 1 --json databaseId --jq '.[0].databaseId') --exit-status
```

Esperado: `build-and-push` en verde. La única anotación esperable es la deprecación de Node.js 20 en las actions, que no afecta al build.

---

### Task 3: Despliegue en Coolify por API

**Files:**
- Usar: `scripts/coolify.py`

**Interfaces:**
- Consume: `main` actualizado (Tarea 2).
- Produce: contenedor corriendo el commit nuevo, `status: running:healthy`.

- [ ] **Step 1: Confirmar qué app es y desde dónde construye**

```bash
.venv/bin/python scripts/coolify.py GET /api/v1/applications/jkscan3y83awkmuuxzt8i3wc
```

Esperado: `git_repository: MiltonZQ/tu-deseo-bot`, `git_branch: main`, `git_commit_sha: HEAD`, `build_pack: dockerfile`. Ese `HEAD` es lo que hace que el despliegue tome el último commit de `main` automáticamente.

- [ ] **Step 2: Lanzar el despliegue**

```bash
.venv/bin/python scripts/coolify.py POST "/api/v1/deploy?uuid=jkscan3y83awkmuuxzt8i3wc&force=false"
```

Esperado: `HTTP 200` con un `deployment_uuid`. Anotarlo.

- [ ] **Step 3: Esperar a que termine y verificar que construyó el commit correcto**

```bash
.venv/bin/python scripts/coolify.py GET /api/v1/deployments/<deployment_uuid>
```

Repetir cada ~15 s hasta `status: finished` (tarda ~2 min). **Verificar que el campo `commit` coincide con `git rev-parse HEAD`**: si no coincide, Coolify construyó otra cosa y hay que averiguar por qué antes de seguir.

---

### Task 4: Verificación de arranque y reindexado de Qdrant

**Files:** ninguno.

**Interfaces:**
- Consume: despliegue `finished` de la Tarea 3.
- Produce: app sana y Qdrant reindexado con las categorías corregidas.

- [ ] **Step 1: Verificar que la app arrancó**

```bash
curl -s https://tu-deseo.autozb.com/health
curl -s https://tu-deseo.autozb.com/webhook
.venv/bin/python scripts/coolify.py GET /api/v1/applications/jkscan3y83awkmuuxzt8i3wc
```

Esperado: `{"status":"ok","business":"Tu Deseo"}`, `{"status":"ok","provider":"ycloud"}` y `status: running:healthy`.

- [ ] **Step 2: Esperar a que Qdrant termine de reindexar**

```bash
.venv/bin/python scripts/coolify.py GET "/api/v1/applications/jkscan3y83awkmuuxzt8i3wc/logs?lines=400"
```

Buscar la línea final:

```
Indexados 246 productos en Qdrant desde DB (filtrados con imagen)
```

Tarda ~4 min: va en lotes de 30 (`Indexados 30 puntos Qdrant en …`), 9 lotes en total. **Hasta que aparezca esa línea, la búsqueda semántica mezcla categorías viejas y nuevas** y los smoke tests de la Tarea 5 darían falsos negativos. Es la razón por la que este paso va antes de probar.

Ruido esperado y NO relacionado con este cambio: dos `WARNING … Excepcion upsert Qdrant` por lote, de las dos primeras URLs de Qdrant que no resuelven antes de que funcione la tercera (`http://qdrant-…:6333`). Es preexistente.

---

### Task 5: Smoke test de los cuatro fallos reportados

**Files:** ninguno (pruebas manuales por WhatsApp).

**Interfaces:**
- Consume: app desplegada y Qdrant reindexado (Tarea 4).
- Produce: confirmación en producción de cada fix, o el disparador del rollback de la Tarea 6.

- [ ] **Step 1: Limpiar el estado del contacto de prueba**

Crítico y fácil de olvidar: el contacto de prueba tiene `conversation_state.categoria_funcional = "bombas-pene"` guardado del bug anterior. Sin limpiarlo, el primer turno arranca contaminado y la prueba no mide nada.

```bash
curl -s -X POST "https://tu-deseo.autozb.com/maintenance/reset-contact?wa_id=<NUMERO_DE_PRUEBA>" \
  -H "X-Reload-Token: <RELOAD_TOKEN>"
```

Esperado: `{"cleared_wa_ids":["<NUMERO>"],"deleted":...}`. El `RELOAD_TOKEN` está en `.env` y en las variables de entorno de Coolify — no imprimirlo en la conversación.

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

> **Importante — cómo despliega este proyecto realmente.** La app de Coolify (`uuid jkscan3y83awkmuuxzt8i3wc`) está configurada con `build_pack: dockerfile`, `git_repository: MiltonZQ/tu-deseo-bot`, `git_branch: main` y `git_commit_sha: HEAD`: **construye desde el repo en cada despliegue y NO usa la imagen de GHCR**. Por lo tanto el rollback NO es cambiar un tag de imagen — es revertir el commit en `main` y redesplegar. La imagen de GHCR que publica el workflow queda como artefacto, pero no es lo que corre en el servidor.

- [ ] **Step 1: Revertir el commit en `main`**

```bash
git checkout main && git pull
git revert --no-edit 625a64d
git push
```

Como Coolify construye desde `HEAD` de `main`, esto deja el repo en el estado bueno antes de redesplegar.

- [ ] **Step 2: Redesplegar**

```bash
.venv/bin/python scripts/coolify.py POST "/api/v1/deploy?uuid=jkscan3y83awkmuuxzt8i3wc&force=false"
```

(o el botón **Redeploy** en la UI de Coolify). Tarda ~2 min en construir; seguir el estado con `GET /api/v1/deployments/<deployment_uuid>` hasta `finished`.

- [ ] **Step 3: Verificar que volvió**

```bash
curl -s https://tu-deseo.autozb.com/health
```

Esperado: `{"status":"ok","business":"Tu Deseo"}`. Ojo: tras cada arranque Qdrant vuelve a reindexar los 246 productos (~4 min en lotes de 30); hasta que aparezca `Indexados 246 productos en Qdrant desde DB` la búsqueda semántica usa el índice a medias.

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

Rollback: revertir el commit en `main` y redesplegar (Tarea 6) — no sirve cambiar el tag de la imagen, porque Coolify construye desde el repo.

Las tres consideraciones planteadas quedan cubiertas así:

| Consideración | Dónde se maneja |
|---|---|
| Reindexado lento de Qdrant | Tarea 4, Step 3: se espera la línea de log antes de probar, para que los smoke tests no den falsos negativos. |
| Bug del Lush sin confirmar | Tarea 5, Step 5: se prueba y se leen los logs nuevos; **explícitamente no** dispara rollback, porque es un fallo preexistente. |
| 1 test rojo preexistente | Tarea 1, Step 5: se documenta como esperado, con la evidencia de que ya fallaba en `815e8d8`. Se arregla aparte en la Tarea 7. |
