# AGENTS.md — Tu Deseo Bot

Asistente de IA trabajando en un bot de WhatsApp + panel para **Tu Deseo** (sex shop).
Mantén el código en español, limpio y consistente con los módulos existentes.

## Contexto del proyecto

- **Cliente:** Tu Deseo — Sex Shop & Bienestar Sexual (Sebastián).
- **Catálogo:** ~300 referencias (se sincroniza desde SIDDE POS o Excel/PDF del cliente).
- **Pagos:** transferencias Nequi/Daviplata/Bancolombia (comprobante por imagen, validado con
  GPT-4o vision) + pasarela Bold (Semana 2).
- **Tono del bot:** empático, educativo, derriba tabúes, sugiere productos complementarios.

## Reglas antes de modificar código

- Lee el módulo objetivo antes de editarlo. Mucha lógica (webhook, firma, escalados, follow-ups,
  CRM, transcripción) ya está implementada y **debe reutilizarse**.
- No imprimas secretos completos en la conversación.
- El esquema de DB vive en `app/db.py` (`SCHEMA_SQL` + `run_migrations`), idempotente.
- Los prompts están en `prompts/system.md` + `prompts/knowledge/*.md`; se recargan en caliente
  con `POST /reload` (header `X-Reload-Token: <RELOAD_TOKEN>`).

## Despliegue (Coolify)

Cuando el usuario pida desplegar:

1. Usa el MCP de Coolify si está configurado.
2. Crea/usar un repo privado de GitHub.
3. Provisiona PostgreSQL y configura `DATABASE_URL`.
4. Publica la app con `WEBHOOK_DOMAIN`.
5. Verifica `GET /health` y el handshake de yCloud en `GET /webhook`.
6. Entrega las instrucciones para pegar Callback URL y secret en yCloud.

## Seguridad

- Nunca commitees `.env`, `.mcp.json`, ni `execution/`.
- Antes de `git add` / `git commit` / `git push`, ejecuta una búsqueda de secretos.
