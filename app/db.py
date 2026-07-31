"""Acceso a Postgres: pool, schema idempotente y consultas.

Esquema fusionado de Demo chat Agentico (bot conversacional) + belux-panel
(panel/abonos) + tablas nuevas de e-commerce para Tu Deseo (productos, pedidos).
"""
import json

import asyncpg
from app import config

_pool: asyncpg.Pool | None = None

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS leads (
    id BIGSERIAL PRIMARY KEY,
    wa_id TEXT UNIQUE NOT NULL,
    nombre TEXT,
    negocio TEXT,
    qualification_status TEXT NOT NULL DEFAULT 'en_progreso'
        CHECK (qualification_status IN ('en_progreso', 'calificado', 'descalificado')),
    disqualify_reason TEXT,
    action_link_sent BOOLEAN NOT NULL DEFAULT FALSE,
    bot_paused BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(qualification_status, created_at DESC);

CREATE TABLE IF NOT EXISTS pending_follow_ups (
    id BIGSERIAL PRIMARY KEY,
    wa_id TEXT UNIQUE NOT NULL,
    send_after TIMESTAMPTZ NOT NULL,
    sent BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_follow_ups_due
    ON pending_follow_ups(send_after) WHERE sent = FALSE;

CREATE TABLE IF NOT EXISTS conversations (
    id BIGSERIAL PRIMARY KEY,
    wa_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user','assistant')),
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conv_wa_id_ts ON conversations(wa_id, created_at DESC);

CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY,
    processed_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS escalations (
    id BIGSERIAL PRIMARY KEY,
    wa_id TEXT NOT NULL,
    customer_name TEXT,
    city TEXT,
    product TEXT,
    purchase_date TEXT,
    issue_summary TEXT,
    reason TEXT NOT NULL,
    reason_detail TEXT,
    media_count INT DEFAULT 0,
    last_media_type TEXT,
    conversation_excerpt TEXT,
    status TEXT NOT NULL DEFAULT 'pendiente'
        CHECK (status IN ('pendiente','en_proceso','resuelto')),
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_esc_status_ts
    ON escalations(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_esc_wa_status
    ON escalations(wa_id, status);

-- ── E-commerce: catálogo y pedidos (Tu Deseo) ──
CREATE TABLE IF NOT EXISTS productos (
    id BIGSERIAL PRIMARY KEY,
    nombre TEXT NOT NULL,
    descripcion TEXT,
    categoria TEXT,
    precio INT NOT NULL DEFAULT 0,
    sku_pos TEXT,
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_productos_categoria ON productos(categoria) WHERE activo = TRUE;
CREATE INDEX IF NOT EXISTS idx_productos_sku ON productos(sku_pos);

CREATE TABLE IF NOT EXISTS pedidos (
    id BIGSERIAL PRIMARY KEY,
    wa_id TEXT NOT NULL,
    nombre_cliente TEXT,
    direccion_envio TEXT,
    ciudad TEXT,
    telefono_contacto TEXT,
    estado TEXT NOT NULL DEFAULT 'pendiente'
        CHECK (estado IN ('pendiente','pagado','aprobado','despachado','entregado','cancelado','devuelto')),
    total INT NOT NULL DEFAULT 0,
    creado_por TEXT NOT NULL DEFAULT 'bot',
    notas TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_pedidos_estado ON pedidos(estado, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pedidos_wa_id ON pedidos(wa_id, created_at DESC);

CREATE TABLE IF NOT EXISTS pedidos_items (
    id BIGSERIAL PRIMARY KEY,
    pedido_id BIGINT NOT NULL REFERENCES pedidos(id) ON DELETE CASCADE,
    producto_id BIGINT REFERENCES productos(id) ON DELETE SET NULL,
    nombre_snapshot TEXT NOT NULL,
    cantidad INT NOT NULL DEFAULT 1,
    precio_unitario INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_items_pedido ON pedidos_items(pedido_id);

-- ── Abonos / comprobantes (porteado de belux-panel + Lavadero) ──
CREATE TABLE IF NOT EXISTS abonos (
    id BIGSERIAL PRIMARY KEY,
    telefono TEXT,
    pedido_id BIGINT REFERENCES pedidos(id) ON DELETE SET NULL,
    servicio TEXT,
    monto INT,
    monto_esperado INT,
    monto_declarado INT,
    estado TEXT NOT NULL DEFAULT 'pendiente'
        CHECK (estado IN ('pendiente','verificado_bot','verificado_manual','no_valido')),
    metodo TEXT,
    banco TEXT,
    referencia TEXT,
    fecha_comprobante DATE,
    url_comprobante TEXT,
    verificado_por TEXT,
    intento_n INT NOT NULL DEFAULT 1,
    fecha TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_abonos_estado ON abonos(estado, fecha DESC);
CREATE INDEX IF NOT EXISTS idx_abonos_telefono ON abonos(telefono, fecha DESC);

-- Vista ligera de "último mensaje" por contacto (compat con panel belux)
CREATE TABLE IF NOT EXISTS conversaciones (
    telefono TEXT PRIMARY KEY,
    ultimo_mensaje TEXT,
    ultimo_mensaje_at TIMESTAMPTZ DEFAULT NOW(),
    total_mensajes INT DEFAULT 1
);

-- Pausa global por contacto (compat con panel belux)
CREATE TABLE IF NOT EXISTS bot_pausado (
    telefono TEXT PRIMARY KEY,
    pausado BOOLEAN DEFAULT TRUE,
    fecha_pausado TIMESTAMPTZ DEFAULT NOW(),
    fecha_reanudado TIMESTAMPTZ,
    motivo TEXT
);

-- ── Memoria comprimida de conversaciones largas (resumen consolidado) ──
-- Un registro por wa_id. Se regenera cuando el historial supera SUMMARY_THRESHOLD
-- mensajes, condensando las interacciones previas en ~5 líneas que se reinyectan
-- en el prompt para que el bot "recuerde" más allá de HISTORY_WINDOW.
CREATE TABLE IF NOT EXISTS conversation_summaries (
    wa_id TEXT PRIMARY KEY,
    summary TEXT NOT NULL,
    message_count INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- ── Estado de conversación (pipeline determinístico) ──
-- Persiste qué busca el cliente y qué se le ha mostrado, para romper el bucle de
-- preguntas en el SISTEMA (no en la memoria del LLM). Un registro por wa_id.
CREATE TABLE IF NOT EXISTS conversation_state (
    wa_id TEXT PRIMARY KEY,
    categoria_busqueda TEXT,        -- 'anillos', 'vibradores', ... (intención)
    categoria_funcional TEXT,       -- 'anillos-y-fundas', ... (mapeo a categoría interna)
    genero TEXT,                    -- 'hombre'|'mujer'|'pareja'|'anal'
    calificado BOOLEAN NOT NULL DEFAULT FALSE,
    productos_mostrados BIGINT[] NOT NULL DEFAULT '{}',  -- ids ya enviados (no repetir)
    updated_at TIMESTAMPTZ DEFAULT now()
);
"""


async def init_pool() -> None:
    global _pool
    _pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=5)


async def close_pool() -> None:
    if _pool:
        await _pool.close()


async def run_migrations() -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            ALTER TABLE pedidos DROP CONSTRAINT IF EXISTS pedidos_estado_check;
            ALTER TABLE pedidos ADD CONSTRAINT pedidos_estado_check
                CHECK (estado IN ('pendiente','pagado','aprobado','despachado','entregado','cancelado','devuelto'));
            """
        )
        await conn.execute(
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS action_link_sent BOOLEAN NOT NULL DEFAULT FALSE"
        )
        await conn.execute(
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS bot_paused BOOLEAN NOT NULL DEFAULT FALSE"
        )
        await conn.execute(
            "ALTER TABLE productos ADD COLUMN IF NOT EXISTS woo_id BIGINT UNIQUE"
        )
        await conn.execute(
            "ALTER TABLE productos ADD COLUMN IF NOT EXISTS precio_regular INT"
        )
        await conn.execute(
            "ALTER TABLE productos ADD COLUMN IF NOT EXISTS precio_oferta INT"
        )
        await conn.execute(
            "ALTER TABLE productos ADD COLUMN IF NOT EXISTS stock_status TEXT DEFAULT 'instock'"
        )
        await conn.execute(
            "ALTER TABLE productos ADD COLUMN IF NOT EXISTS imagen_url TEXT"
        )
        await conn.execute(
            "ALTER TABLE productos ADD COLUMN IF NOT EXISTS galeria_urls TEXT"
        )
        await conn.execute(
            "ALTER TABLE productos ADD COLUMN IF NOT EXISTS permalink TEXT"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_productos_woo_id ON productos(woo_id)"
        )


async def seed_catalogo_if_empty(csv_path) -> int:
    """Carga el catálogo desde un CSV a la tabla productos, solo si está vacía.

    CSV esperado (con header): nombre, descripcion, categoria, precio, sku_pos, activo.
    Idempotente: si ya hay productos, no hace nada. Devuelve el número de insertados.
    """
    import csv as _csv
    from pathlib import Path

    p = Path(csv_path)
    if not p.exists():
        return 0

    async with _pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM productos")
        if count and count > 0:
            return 0  # ya poblada

        rows = []
        with p.open(newline="", encoding="utf-8") as f:
            reader = _csv.DictReader(f)
            for r in reader:
                nombre = (r.get("nombre") or "").strip()
                try:
                    precio = int(str(r.get("precio", "0")).replace(".", "").replace(",", "").replace("$", "").strip() or "0")
                except ValueError:
                    precio = 0
                if not nombre:
                    continue
                rows.append((
                    nombre[:200],
                    (r.get("descripcion") or "").strip() or None,
                    (r.get("categoria") or "").strip() or None,
                    precio,
                    (r.get("sku_pos") or "").strip() or None,
                    str(r.get("activo", "true")).strip().lower() in ("true", "1", "si", "yes"),
                ))

        if not rows:
            return 0
        await conn.executemany(
            """
            INSERT INTO productos (nombre, descripcion, categoria, precio, sku_pos, activo)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT DO NOTHING
            """,
            rows,
        )
        return len(rows)


async def seed_catalogo_from_md(md_path) -> int:
    """Carga/sincroniza el catálogo completo desde catalogo.md en la tabla productos.

    Idempotente: inserta o actualiza por `id` los 246 productos del catálogo local
    con su ID exacto, nombre, precio y descripción, garantizando que siempre estén
    disponibles y activos.
    """
    import re
    from pathlib import Path

    p = Path(md_path)
    if not p.exists():
        return 0

    text = p.read_text(encoding="utf-8")
    pattern = re.compile(r"-\s*\*\*(.*?)\*\*\s*—\s*\$(.*?)\s*—\s*(.*?)\s*#(\d+)")

    rows = []
    for line in text.splitlines():
        line = line.strip()
        m = pattern.match(line)
        if m:
            nombre, precio_str, desc, pid = m.groups()
            try:
                precio = int(precio_str.replace(".", "").replace(",", "").strip())
            except ValueError:
                precio = 0
            rows.append((
                int(pid),
                nombre.strip(),
                desc.strip(),
                precio,
                "instock",
                f"https://tu-deseo.autozb.com/img/p_{pid}.jpg",
                True,
            ))

    if not rows:
        return 0

    async with _pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO productos (id, nombre, descripcion, precio, stock_status, imagen_url, activo)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (id) DO UPDATE SET
                nombre = EXCLUDED.nombre,
                descripcion = EXCLUDED.descripcion,
                precio = EXCLUDED.precio,
                stock_status = EXCLUDED.stock_status,
                activo = TRUE,
                imagen_url = COALESCE(NULLIF(productos.imagen_url, ''), EXCLUDED.imagen_url)
            """,
            rows,
        )
        return len(rows)



async def purge_old(ttl_days: int) -> int:
    async with _pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM conversations WHERE created_at < now() - make_interval(days => $1)",
            int(ttl_days),
        )
        return int(result.split()[-1]) if result else 0


async def was_processed(message_id: str) -> bool:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM processed_messages WHERE message_id = $1", message_id
        )
        return row is not None


async def mark_processed(message_id: str) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO processed_messages(message_id) VALUES($1) "
            "ON CONFLICT DO NOTHING",
            message_id,
        )


async def get_history(wa_id: str, limit: int) -> list[dict]:
    """Devuelve los últimos `limit` mensajes en orden cronológico ascendente."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role, content FROM conversations "
            "WHERE wa_id = $1 ORDER BY created_at DESC LIMIT $2",
            wa_id, limit,
        )
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


async def list_conversation_threads(limit: int = 100) -> list[dict]:
    """Lista conversaciones agrupadas por wa_id con ultimo mensaje y metadata de lead."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            WITH last_messages AS (
                SELECT DISTINCT ON (wa_id)
                    wa_id, role, content, created_at
                FROM conversations
                ORDER BY wa_id, created_at DESC
            ),
            counts AS (
                SELECT wa_id, COUNT(*) AS message_count
                FROM conversations
                GROUP BY wa_id
            )
            SELECT
                lm.wa_id,
                lm.role AS last_role,
                lm.content AS last_content,
                lm.created_at AS last_message_at,
                counts.message_count,
                leads.nombre,
                leads.negocio,
                leads.qualification_status,
                leads.bot_paused
            FROM last_messages lm
            JOIN counts ON counts.wa_id = lm.wa_id
            LEFT JOIN leads ON leads.wa_id = lm.wa_id
            ORDER BY lm.created_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]


async def list_conversation_messages(wa_id: str, limit: int = 100) -> list[dict]:
    """Devuelve los mensajes de una conversacion en orden cronologico."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content, created_at
            FROM conversations
            WHERE wa_id = $1
            ORDER BY created_at DESC
            LIMIT $2
            """,
            wa_id,
            limit,
        )
    return [dict(r) for r in reversed(rows)]


async def save_message(wa_id: str, role: str, content: str) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO conversations(wa_id, role, content) VALUES($1, $2, $3)",
            wa_id, role, content,
        )
        # Mantener la vista ligera (compat panel)
        await conn.execute(
            """
            INSERT INTO conversaciones (telefono, ultimo_mensaje, ultimo_mensaje_at, total_mensajes)
            VALUES ($1, $2, now(), 1)
            ON CONFLICT (telefono) DO UPDATE SET
                ultimo_mensaje = EXCLUDED.ultimo_mensaje,
                ultimo_mensaje_at = now(),
                total_mensajes = conversaciones.total_mensajes + 1
            """,
            wa_id, content[:500],
        )


# ── Memoria comprimida (resumen consolidado de conversaciones largas) ──

async def get_summary(wa_id: str) -> dict | None:
    """Devuelve {summary, message_count} del último resumen consolidado, o None."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT summary, message_count FROM conversation_summaries WHERE wa_id = $1",
            wa_id,
        )
    return dict(row) if row else None


async def save_summary(wa_id: str, summary: str, message_count: int) -> None:
    """Persiste (o actualiza) el resumen consolidado de la conversación."""
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO conversation_summaries (wa_id, summary, message_count, updated_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (wa_id) DO UPDATE SET
                summary = EXCLUDED.summary,
                message_count = EXCLUDED.message_count,
                updated_at = now()
            """,
            wa_id, summary, message_count,
        )


# ── Estado de conversación (pipeline determinístico) ──

async def get_conversation_state(wa_id: str) -> dict | None:
    """Devuelve el estado de conversación del cliente, o None si no existe.

    Campos: categoria_busqueda, categoria_funcional, genero, calificado,
    productos_mostrados (lista de int).
    """
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT categoria_busqueda, categoria_funcional, genero, calificado,
                   productos_mostrados
            FROM conversation_state WHERE wa_id = $1
            """,
            wa_id,
        )
    if not row:
        return None
    return {
        "categoria_busqueda": row["categoria_busqueda"],
        "categoria_funcional": row["categoria_funcional"],
        "genero": row["genero"],
        "calificado": row["calificado"],
        "productos_mostrados": list(row["productos_mostrados"] or []),
    }


async def upsert_conversation_state(
    wa_id: str,
    categoria_busqueda: str | None = None,
    categoria_funcional: str | None = None,
    genero: str | None = None,
    calificado: bool | None = None,
    add_productos_mostrados: list[int] | None = None,
    reset: bool = False,
) -> None:
    """Crea o actualiza el estado de conversación de un cliente.

    - Los campos categoria_busqueda/categoria_funcional/genero/calificado se
      actualizan con el valor provisto (None los deja igual salvo reset=True).
    - add_productos_mostrados añade IDs a los ya mostrados (sin duplicados).
    - reset=True reinicia estado (ej: cliente cambia radicalmente de tema); en
      ese caso los campos pasan a su default y productos_mostrados se vacía.
    """
    async with _pool.acquire() as conn:
        if reset:
            await conn.execute(
                """
                INSERT INTO conversation_state (wa_id, calificado, productos_mostrados, updated_at)
                VALUES ($1, FALSE, '{}', now())
                ON CONFLICT (wa_id) DO UPDATE SET
                    categoria_busqueda = NULL,
                    categoria_funcional = NULL,
                    genero = NULL,
                    calificado = FALSE,
                    productos_mostrados = '{}',
                    updated_at = now()
                """,
                wa_id,
            )
            return

        # Construir SET dinámico solo con los campos provistos.
        sets: list[str] = []
        params: list = []
        idx = 1
        if categoria_busqueda is not None:
            sets.append(f"categoria_busqueda = ${idx}")
            params.append(categoria_busqueda)
            idx += 1
        if categoria_funcional is not None:
            sets.append(f"categoria_funcional = ${idx}")
            params.append(categoria_funcional)
            idx += 1
        if genero is not None:
            sets.append(f"genero = ${idx}")
            params.append(genero)
            idx += 1
        if calificado is not None:
            sets.append(f"calificado = ${idx}")
            params.append(calificado)
            idx += 1
        if add_productos_mostrados:
            # Añadir a los existentes con UNION (sin duplicados).
            # OJO: usar ${idx} (un solo $) como placeholder de asyncpg. Escribir
            # $$ activa el 'dollar-quoted string' de Postgres y rompe el SQL con
            # 'unterminated dollar-quoted string', lo que hace que NUNCA se persistan
            # los productos mostrados (causa raíz del bug de repetir fotos en 'ver más').
            # IMPORTANTE: calificar la columna como conversation_state.productos_mostrados
            # para evitar AmbiguousColumnError (en ON CONFLICT, Postgres no sabe si
            # refiere a la fila existente o a EXCLUDED). Sin esto, la persistencia falla
            # y productos_mostrados nunca se guarda → se repiten fotos.
            sets.append(
                f"productos_mostrados = ARRAY(SELECT DISTINCT unnest(conversation_state.productos_mostrados || ${idx}::bigint[]))"
            )
            params.append(list(add_productos_mostrados))
            idx += 1
        sets.append("updated_at = now()")

        await conn.execute(
            f"""
            INSERT INTO conversation_state (wa_id, calificado, productos_mostrados, updated_at)
            VALUES ($1, FALSE, '{{}}', now())
            ON CONFLICT (wa_id) DO UPDATE SET {', '.join(sets)}
            """,
            wa_id, *params,
        )


# ── Escalaciones ──

async def find_pending_escalation(wa_id: str) -> dict | None:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM escalations WHERE wa_id=$1 AND status='pendiente' "
            "ORDER BY created_at DESC LIMIT 1",
            wa_id,
        )
    return dict(row) if row else None


async def create_escalation(data: dict) -> int:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO escalations(
                wa_id, customer_name, city, product, purchase_date,
                issue_summary, reason, reason_detail, media_count,
                last_media_type, conversation_excerpt
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            RETURNING id
            """,
            data.get("wa_id"),
            data.get("customer_name"),
            data.get("city"),
            data.get("product"),
            data.get("purchase_date"),
            data.get("issue_summary"),
            data["reason"],
            data.get("reason_detail"),
            data.get("media_count", 0),
            data.get("last_media_type"),
            data.get("conversation_excerpt"),
        )
    return row["id"]


async def bump_escalation(escalation_id: int, data: dict) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE escalations SET
                customer_name = COALESCE($2, customer_name),
                city = COALESCE($3, city),
                product = COALESCE($4, product),
                purchase_date = COALESCE($5, purchase_date),
                issue_summary = COALESCE($6, issue_summary),
                reason = COALESCE($7, reason),
                reason_detail = COALESCE($8, reason_detail),
                media_count = media_count + $9,
                last_media_type = COALESCE($10, last_media_type),
                conversation_excerpt = COALESCE($11, conversation_excerpt),
                updated_at = now()
            WHERE id = $1
            """,
            escalation_id,
            data.get("customer_name"),
            data.get("city"),
            data.get("product"),
            data.get("purchase_date"),
            data.get("issue_summary"),
            data.get("reason"),
            data.get("reason_detail"),
            int(data.get("media_count_delta", 0)),
            data.get("last_media_type"),
            data.get("conversation_excerpt"),
        )


async def list_escalations(status: str | None = None, limit: int = 200) -> list[dict]:
    query = "SELECT * FROM escalations"
    args: list = []
    if status:
        query += " WHERE status = $1"
        args.append(status)
    query += " ORDER BY created_at DESC LIMIT $%d" % (len(args) + 1)
    args.append(limit)
    async with _pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
    return [dict(r) for r in rows]


async def get_escalation(escalation_id: int) -> dict | None:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM escalations WHERE id=$1", escalation_id)
    return dict(row) if row else None


async def update_escalation_status(
    escalation_id: int, status: str, notes: str | None = None
) -> None:
    async with _pool.acquire() as conn:
        if status == "resuelto":
            await conn.execute(
                "UPDATE escalations SET status=$2, notes=COALESCE($3, notes), "
                "resolved_at=now(), updated_at=now() WHERE id=$1",
                escalation_id, status, notes,
            )
        else:
            await conn.execute(
                "UPDATE escalations SET status=$2, notes=COALESCE($3, notes), "
                "updated_at=now() WHERE id=$1",
                escalation_id, status, notes,
            )


async def escalation_counts() -> dict:
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT status, COUNT(*) AS n FROM escalations GROUP BY status"
        )
    return {r["status"]: r["n"] for r in rows}


async def delete_escalation(escalation_id: int) -> bool:
    """Elimina una escalación por ID."""
    async with _pool.acquire() as conn:
        res = await conn.execute("DELETE FROM escalations WHERE id = $1", escalation_id)
        return res.endswith("1") or res.endswith("ROW")


async def clear_all_escalations() -> int:
    """Elimina todos los escalados de la base de datos."""
    async with _pool.acquire() as conn:
        res = await conn.execute("DELETE FROM escalations")
        return int(res.split()[-1]) if res else 0



# ── Bot pause/resume ──

async def is_bot_paused(wa_id: str) -> bool:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT bot_paused FROM leads WHERE wa_id = $1", wa_id
        )
    return bool(row["bot_paused"]) if row else False


async def set_bot_paused(wa_id: str, paused: bool, motivo: str | None = None) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO leads(wa_id, bot_paused)
            VALUES($1, $2)
            ON CONFLICT (wa_id) DO UPDATE SET bot_paused = $2, updated_at = now()
            """,
            wa_id, paused,
        )
        # Mantener tabla de pausa (compat panel belux)
        await conn.execute(
            """
            INSERT INTO bot_pausado (telefono, pausado, fecha_pausado, motivo)
            VALUES ($1, $2, now(), $3)
            ON CONFLICT (telefono) DO UPDATE SET
                pausado = $2,
                fecha_pausado = CASE WHEN $2 THEN now() ELSE bot_pausado.fecha_pausado END,
                fecha_reanudado = CASE WHEN NOT $2 THEN now() ELSE bot_pausado.fecha_reanudado END,
                motivo = COALESCE($3, bot_pausado.motivo)
            """,
            wa_id, paused, motivo,
        )


# ── Leads / CRM ──

async def upsert_lead(wa_id: str, **kwargs) -> None:
    fields = {k: v for k, v in kwargs.items()
              if k in ("nombre", "negocio", "qualification_status",
                       "disqualify_reason", "action_link_sent", "bot_paused")}
    if not fields:
        return
    set_clause = ", ".join(
        f"{col} = ${i + 2}" for i, col in enumerate(fields)
    )
    values = list(fields.values())
    async with _pool.acquire() as conn:
        await conn.execute(
            f"""
            INSERT INTO leads(wa_id, {", ".join(fields)})
            VALUES($1, {", ".join(f"${i+2}" for i in range(len(fields)))})
            ON CONFLICT (wa_id) DO UPDATE SET {set_clause}, updated_at = now()
            """,
            wa_id, *values,
        )


async def get_lead(wa_id: str) -> dict | None:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM leads WHERE wa_id = $1", wa_id)
    return dict(row) if row else None


async def list_leads(status: str | None = None, limit: int = 200) -> list[dict]:
    query = "SELECT * FROM leads"
    args: list = []
    if status:
        query += " WHERE qualification_status = $1"
        args.append(status)
    query += f" ORDER BY created_at DESC LIMIT ${len(args) + 1}"
    args.append(limit)
    async with _pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
    return [dict(r) for r in rows]


async def update_lead_status(
    wa_id: str,
    status: str,
    disqualify_reason: str | None = None,
) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE leads SET
                qualification_status = $2,
                disqualify_reason = CASE
                    WHEN $2 = 'descalificado' THEN COALESCE($3, disqualify_reason)
                    ELSE NULL
                END,
                updated_at = now()
            WHERE wa_id = $1
            """,
            wa_id,
            status,
            disqualify_reason,
        )


async def crm_counts() -> dict:
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT qualification_status, COUNT(*) AS n FROM leads GROUP BY qualification_status"
        )
    return {r["qualification_status"]: r["n"] for r in rows}


async def qualify_leads_with_action_link(action_url: str | None = None) -> int:
    async with _pool.acquire() as conn:
        if action_url:
            result = await conn.execute(
                """
                UPDATE leads SET
                    qualification_status = 'calificado',
                    action_link_sent = TRUE,
                    disqualify_reason = NULL,
                    updated_at = now()
                WHERE qualification_status <> 'calificado'
                  AND (
                    action_link_sent = TRUE
                    OR EXISTS (
                        SELECT 1 FROM conversations
                        WHERE conversations.wa_id = leads.wa_id
                          AND conversations.role = 'assistant'
                          AND conversations.content ILIKE '%' || $1 || '%'
                    )
                  )
                """,
                action_url,
            )
        else:
            result = await conn.execute(
                """
                UPDATE leads SET
                    qualification_status = 'calificado',
                    action_link_sent = TRUE,
                    disqualify_reason = NULL,
                    updated_at = now()
                WHERE qualification_status <> 'calificado'
                  AND action_link_sent = TRUE
                """
            )
    return int(result.split()[-1]) if result else 0


# ── Follow-ups ──

async def upsert_follow_up(wa_id: str, delay_minutes: int = 10) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pending_follow_ups(wa_id, send_after)
            VALUES($1, now() + make_interval(mins => $2))
            ON CONFLICT (wa_id) DO UPDATE SET
                send_after = now() + make_interval(mins => $2),
                sent = FALSE
            """,
            wa_id, delay_minutes,
        )


async def cancel_follow_up(wa_id: str) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM pending_follow_ups WHERE wa_id = $1 AND sent = FALSE",
            wa_id,
        )


async def get_due_follow_ups() -> list[dict]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, wa_id FROM pending_follow_ups "
            "WHERE sent = FALSE AND send_after <= now()"
        )
    return [dict(r) for r in rows]


async def mark_follow_up_sent(follow_up_id: int) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE pending_follow_ups SET sent = TRUE WHERE id = $1",
            follow_up_id,
        )


async def mark_all_follow_ups_sent() -> int:
    async with _pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE pending_follow_ups SET sent = TRUE WHERE sent = FALSE"
        )
    return int(result.split()[-1]) if result else 0


async def clear_contact_data(wa_ids: list[str]) -> dict[str, int]:
    """Borra estado conversacional y comercial de una lista de contactos."""
    async with _pool.acquire() as conn:
        results = {
            "conversations": await conn.execute(
                "DELETE FROM conversations WHERE wa_id = ANY($1::text[])",
                wa_ids,
            ),
            "leads": await conn.execute(
                "DELETE FROM leads WHERE wa_id = ANY($1::text[])",
                wa_ids,
            ),
            "escalations": await conn.execute(
                "DELETE FROM escalations WHERE wa_id = ANY($1::text[])",
                wa_ids,
            ),
            "pending_follow_ups": await conn.execute(
                "DELETE FROM pending_follow_ups WHERE wa_id = ANY($1::text[])",
                wa_ids,
            ),
        }
    return {
        key: int(value.split()[-1]) if value else 0
        for key, value in results.items()
    }


async def clear_all_conversations() -> dict[str, int]:
    """Elimina todo el historial conversacional y estados de la base de datos para todos los números."""
    async with _pool.acquire() as conn:
        results = {
            "conversations": await conn.execute("DELETE FROM conversations"),
            "conversaciones": await conn.execute("DELETE FROM conversaciones"),
            "conversation_summaries": await conn.execute("DELETE FROM conversation_summaries"),
            "conversation_state": await conn.execute("DELETE FROM conversation_state"),
            "processed_messages": await conn.execute("DELETE FROM processed_messages"),
            "pending_follow_ups": await conn.execute("DELETE FROM pending_follow_ups"),
            "escalations": await conn.execute("DELETE FROM escalations"),
        }
    return {
        key: int(value.split()[-1]) if value else 0
        for key, value in results.items()
    }


# ═══════════════════════════════════════════════════════════════════════
# Panel de gestión (porteado/adaptado de belux-panel + Tu Deseo)
# ═══════════════════════════════════════════════════════════════════════

async def admin_metrics() -> dict:
    """Métricas ligeras para el dashboard."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                (SELECT COUNT(DISTINCT wa_id) FROM conversations) AS conversations,
                (SELECT COUNT(*) FROM conversations) AS messages,
                (SELECT COUNT(*) FROM leads) AS leads,
                (SELECT COUNT(*) FROM leads WHERE qualification_status = 'calificado') AS qualified,
                (SELECT COUNT(*) FROM escalations WHERE status = 'pendiente') AS pending_escalations,
                (SELECT COUNT(*) FROM pedidos WHERE created_at::date = CURRENT_DATE) AS pedidos_hoy,
                (SELECT COUNT(*) FROM pedidos
                   WHERE created_at >= date_trunc('week', CURRENT_DATE)) AS pedidos_semana,
                (SELECT COUNT(*) FROM abonos WHERE estado = 'pendiente') AS abonos_pendientes
            """
        )
    return dict(row)


async def pedidos_por_dia(days: int = 7) -> list[dict]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT created_at::date AS dia,
                   COUNT(*) FILTER (WHERE estado NOT IN ('cancelado', 'devuelto')) AS pedidos,
                   COUNT(*) FILTER (WHERE estado IN ('cancelado', 'devuelto')) AS cancelados
            FROM pedidos
            WHERE created_at::date >= CURRENT_DATE - $1::int
            GROUP BY created_at::date
            ORDER BY dia
            """,
            days,
        )
    return [{"dia": str(r["dia"]), "pedidos": r["pedidos"], "cancelados": r["cancelados"]} for r in rows]


async def productos_top(days: int = 30, limit: int = 10) -> list[dict]:
    """Productos más solicitados por número de pedidos en el período."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pi.nombre_snapshot AS producto,
                   SUM(pi.cantidad) AS total
            FROM pedidos_items pi
            JOIN pedidos p ON p.id = pi.pedido_id
            WHERE p.created_at::date >= CURRENT_DATE - $1::int
              AND p.estado NOT IN ('cancelado', 'devuelto')
            GROUP BY pi.nombre_snapshot
            ORDER BY total DESC
            LIMIT $2
            """,
            days, limit,
        )
    return [{"producto": r["producto"], "total": r["total"]} for r in rows]


async def picos_demanda(days: int = 14) -> list[dict]:
    """Distribución de mensajes por hora del día (para detectar picos)."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT EXTRACT(HOUR FROM created_at AT TIME ZONE $1)::int AS hora,
                   COUNT(*) AS mensajes
            FROM conversations
            WHERE created_at::date >= CURRENT_DATE - $2::int
            GROUP BY hora
            ORDER BY hora
            """,
            config.BOT_TIMEZONE, days,
        )
    return [{"hora": r["hora"], "mensajes": r["mensajes"]} for r in rows]


async def list_pedidos(estado: str | None = None, limit: int = 50) -> list[dict]:
    query = """
        SELECT id, wa_id, nombre_cliente, direccion_envio, ciudad,
               telefono_contacto, estado, total, creado_por,
               to_char(created_at AT TIME ZONE $1, 'DD/MM/YYYY HH24:MI') AS fecha
        FROM pedidos
    """
    args: list = [config.BOT_TIMEZONE]
    if estado:
        query += " WHERE estado = $2"
        args.append(estado)
    query += f" ORDER BY created_at DESC LIMIT ${len(args) + 1}"
    args.append(limit)
    async with _pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
    return [dict(r) for r in rows]


async def update_pedido_estado(pedido_id: int, estado: str) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE pedidos SET estado = $2, updated_at = now() WHERE id = $1",
            pedido_id, estado,
        )


async def delete_pedido(pedido_id: int) -> bool:
    async with _pool.acquire() as conn:
        res = await conn.execute("DELETE FROM pedidos WHERE id = $1", pedido_id)
        return res.endswith("1") or res.endswith("ROW")


async def get_pedido(pedido_id: int) -> dict | None:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM pedidos WHERE id = $1", pedido_id)
    return dict(row) if row else None


async def create_pedido(data: dict) -> int:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO pedidos (wa_id, nombre_cliente, direccion_envio, ciudad,
                telefono_contacto, estado, total, creado_por, notas)
            VALUES ($1, $2, $3, $4, $5, COALESCE($6,'pendiente'), $7, $8, $9)
            RETURNING id
            """,
            data.get("wa_id"), data.get("nombre_cliente"), data.get("direccion_envio"),
            data.get("ciudad"), data.get("telefono_contacto"), data.get("estado"),
            int(data.get("total", 0)), data.get("creado_por", "asesor"),
            data.get("notas"),
        )
    return row["id"] if row else 0


async def add_pedido_item(
    pedido_id: int,
    producto_id: int | None,
    nombre_snapshot: str,
    cantidad: int = 1,
    precio_unitario: int = 0,
) -> None:
    """Inserta una línea de producto en un pedido."""
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO pedidos_items (pedido_id, producto_id, nombre_snapshot, cantidad, precio_unitario)
            VALUES ($1, $2, $3, $4, $5)
            """,
            pedido_id, producto_id, nombre_snapshot, int(cantidad), int(precio_unitario),
        )


async def list_pedido_items(pedido_id: int) -> list[dict]:
    """Devuelve las líneas de producto de un pedido."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, producto_id, nombre_snapshot, cantidad, precio_unitario
            FROM pedidos_items
            WHERE pedido_id = $1
            ORDER BY id
            """,
            pedido_id,
        )
    return [dict(r) for r in rows]


async def get_pedido_pendiente(wa_id: str) -> dict | None:
    """Devuelve el pedido pendiente reciente de un contacto (o None), acotado a las últimas 24 horas."""
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, wa_id, nombre_cliente, direccion_envio, ciudad,
                   telefono_contacto, estado, total, creado_por, notas,
                   to_char(created_at AT TIME ZONE $2, 'DD/MM/YYYY HH24:MI') AS fecha
            FROM pedidos
            WHERE wa_id = $1 AND estado = 'pendiente' AND created_at >= NOW() - INTERVAL '24 hours'
            ORDER BY created_at DESC LIMIT 1
            """,
            wa_id, config.BOT_TIMEZONE,
        )
    return dict(row) if row else None


async def list_abonos_de_pedido(pedido_id: int) -> list[dict]:
    """Devuelve los abonos (comprobantes) asociados a un pedido."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, monto, monto_esperado, estado, metodo, banco, referencia,
                   url_comprobante, verificado_por, intento_n,
                   to_char(fecha AT TIME ZONE $2, 'DD/MM/YYYY HH24:MI') AS fecha
            FROM abonos
            WHERE pedido_id = $1
            ORDER BY fecha DESC
            """,
            pedido_id, config.BOT_TIMEZONE,
        )
    return [dict(r) for r in rows]


# ── Abonos (panel + lógica de comprobantes) ──

async def list_abonos(estado: str | None = None, limit: int = 50) -> list[dict]:
    query = """
        SELECT id, telefono, pedido_id, servicio, monto, monto_esperado, monto_declarado,
               estado, metodo, banco, referencia, fecha_comprobante, url_comprobante,
               verificado_por, intento_n,
               to_char(fecha AT TIME ZONE $1, 'DD/MM/YYYY HH24:MI') AS fecha
        FROM abonos
    """
    args: list = [config.BOT_TIMEZONE]
    if estado:
        query += " WHERE estado = $2"
        args.append(estado)
    query += f" ORDER BY fecha DESC LIMIT ${len(args) + 1}"
    args.append(limit)
    async with _pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
    return [dict(r) for r in rows]


async def update_abono_estado(abono_id: int, estado: str, verificado_por: str = "asesor") -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE abonos SET estado = $2, verificado_por = $3 WHERE id = $1",
            abono_id, estado, verificado_por,
        )


async def insert_abono(data: dict) -> int:
    """Crea un registro de abono (lo usa payments.py). Devuelve el id."""
    # fecha_comprobante viene como string "YYYY-MM-DD" desde el vision model, pero la
    # columna es DATE; asyncpg no convierte strings a date automáticamente.
    from datetime import date
    fecha_cruda = data.get("fecha_comprobante")
    fecha_date: date | None = None
    if fecha_cruda:
        try:
            fecha_date = date.fromisoformat(str(fecha_cruda).strip())
        except (ValueError, TypeError):
            fecha_date = None  # formato inválido → NULL (no rompe el insert)

    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO abonos (telefono, pedido_id, servicio, monto, monto_esperado,
                monto_declarado, estado, metodo, banco, referencia, fecha_comprobante,
                url_comprobante, verificado_por, intento_n)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            data.get("telefono"), data.get("pedido_id"), data.get("servicio"),
            data.get("monto"), data.get("monto_esperado"), data.get("monto_declarado"),
            data.get("estado", "pendiente"), data.get("metodo"), data.get("banco"),
            data.get("referencia"), fecha_date,
            data.get("url_comprobante"), data.get("verificado_por", "bot"),
            int(data.get("intento_n", 1)),
        )
    return row["id"] if row else 0


async def update_abono_pedido_id(abono_id: int, pedido_id: int) -> None:
    """Asocia un abono existente a un id de pedido."""
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE abonos SET pedido_id = $1 WHERE id = $2",
            pedido_id, abono_id,
        )


async def backfill_verified_abonos() -> int:
    """Crea pedidos retroactivos para abonos verificados sin pedido o cuyo pedido esté cancelado."""
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT a.id, a.telefono, a.monto, a.monto_declarado, a.monto_esperado, a.banco, a.referencia, a.pedido_id
            FROM abonos a
            LEFT JOIN pedidos p ON a.pedido_id = p.id
            WHERE a.estado IN ('verificado_bot', 'verificado_manual')
              AND (a.pedido_id IS NULL OR a.pedido_id = 0 OR p.estado = 'cancelado')
            """
        )
        if not rows:
            return 0
        
        count = 0
        for r in rows:
            abono_id = int(r["id"])
            wa_id = str(r["telefono"] or "")
            monto_val = int(r["monto_declarado"] or r["monto"] or r["monto_esperado"] or 0)
            if monto_val == 0:
                monto_val = 29800
            
            history_rows = await conn.fetch(
                "SELECT role, content FROM mensajes WHERE wa_id = $1 ORDER BY id DESC LIMIT 20",
                wa_id,
            )
            history = [{"role": h["role"], "content": h["content"]} for h in reversed(history_rows)] if history_rows else []
            
            from app import pedidos
            nombre = pedidos._extraer_nombre(history) if history else None
            ciudad = pedidos._extraer_ciudad(history) if history else None
            direccion = pedidos._extraer_direccion(history) if history else None
            telefono = pedidos._extraer_telefono(history, wa_id) if history else wa_id
            items, _ = await pedidos._resolver_productos_y_total(history) if history else ([], 0)

            pedido_id = await create_pedido({
                "wa_id": wa_id,
                "nombre_cliente": nombre,
                "direccion_envio": direccion,
                "ciudad": ciudad,
                "telefono_contacto": telefono,
                "estado": "pagado",
                "total": monto_val,
                "creado_por": "bot",
                "notas": f"Pedido pagado (verificado en abono #{abono_id}, Ref: {r.get('referencia') or 'S/N'})",
            })
            
            await conn.execute("UPDATE abonos SET pedido_id = $1, monto = $2, monto_declarado = $2 WHERE id = $3", pedido_id, monto_val, abono_id)
            
            for item in items:
                try:
                    await add_pedido_item(
                        pedido_id=pedido_id,
                        producto_id=item["producto_id"],
                        nombre_snapshot=item["nombre"],
                        cantidad=item["cantidad"],
                        precio_unitario=item["precio_unitario"],
                    )
                except Exception:
                    pass
            count += 1
            
        log.info("Backfill: %d abonos verificados retroactivos asociados a nuevos pedidos 'pagado'", count)
        return count


async def count_abonos_fallidos(telefono: str, hours: int) -> int:
    """Cuenta comprobantes inválidos recientes (para lógica de escalamiento)."""
    async with _pool.acquire() as conn:
        n = await conn.fetchval(
            """
            SELECT COUNT(*) FROM abonos
            WHERE telefono = $1 AND estado = 'no_valido'
              AND fecha > now() - make_interval(hours => $2)
            """,
            telefono, hours,
        )
    return int(n or 0)


# ── Pausados (compat panel belux) ──

async def list_pausados() -> list[dict]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT telefono, motivo, fecha_pausado
            FROM bot_pausado
            WHERE pausado = TRUE
            ORDER BY fecha_pausado DESC
            """
        )
    return [dict(r) for r in rows]


async def reanudar_bot(telefono: str) -> None:
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE bot_pausado SET pausado = FALSE, fecha_reanudado = now() WHERE telefono = $1",
            telefono,
        )
        # Marcar escalations pendientes del contacto como resueltas al reanudar el bot
        await conn.execute(
            "UPDATE escalations SET status = 'resuelto', resolved_at = now(), updated_at = now() "
            "WHERE wa_id = $1 AND status = 'pendiente'",
            telefono,
        )
    # además sincroniza leads.bot_paused
    await set_bot_paused(telefono, False)


# ── Recientes (compat panel belux) ──

async def list_recientes(limit: int = 30) -> list[dict]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT telefono, ultimo_mensaje, ultimo_mensaje_at, total_mensajes
            FROM conversaciones
            WHERE ultimo_mensaje_at > now() - interval '48 hours'
            ORDER BY ultimo_mensaje_at DESC
            LIMIT $1
            """,
            limit,
        )
    return [dict(r) for r in rows]
