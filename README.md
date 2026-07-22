# Tu Deseo — Bot de WhatsApp + Panel de Gestión

Bot conversacional y panel de administración para **Tu Deseo — Sex Shop & Bienestar Sexual**.
Asesoría de productos (catálogo de ~300 referencias), captura de pedidos, validación de
comprobantes de pago (Nequi/Daviplata/Bancolombia) y pasarela Bold.

## Stack

- **Backend:** Python + FastAPI
- **Base de datos:** PostgreSQL 16
- **Canal:** WhatsApp Cloud API vía **yCloud** (Coexistence Partner oficial de Meta)
- **IA:** OpenAI (`gpt-5.2-chat-latest` conversación, `gpt-4o` comprobantes, `whisper-1` audios)
- **Despliegue:** Docker + Coolify

## Estructura

```
tu-deseo-bot/
├── app/
│   ├── main.py            # Webhook yCloud/Meta, flujo de mensajes
│   ├── whatsapp_client.py # Envío y extracción de mensajes
│   ├── signature.py       # Verificación de firma del webhook
│   ├── openai_client.py   # Chat + transcripción Whisper + token counter
│   ├── escalations.py     # Detección y registro de escalados a humano
│   ├── leads.py           # CRM y cualificación de leads
│   ├── follow_ups.py      # Re-engagement programado
│   ├── payments.py        # Validación de comprobantes con GPT-4o vision  (Fase 3)
│   ├── catalog.py         # Gestión/sync de productos SIDDE POS           (Fase 1)
│   ├── admin.py           # Panel web responsive (Dashboard, Pedidos, Abonos…)
│   ├── db.py              # Esquema y consultas PostgreSQL
│   └── config.py          # Variables de entorno y prompts
├── prompts/
│   ├── system.md          # Tono y reglas del bot
│   └── knowledge/         # Catálogo y FAQ
├── Dockerfile
├── docker-compose.yml     # PostgreSQL + app para desarrollo local
├── requirements.txt
└── .env.example
```

## Puesta en marcha (local)

```bash
cp .env.example .env
# Edita .env con tus claves reales (al menos YCLOUD_*, OPENAI_API_KEY, ADMIN_*, SESSION_SECRET)
docker compose up --build
```

- App: http://localhost:8000
- Health: http://localhost:8000/health
- Panel: http://localhost:8000/admin/login

## Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Estado del servicio |
| GET | `/webhook` | Handshake yCloud/Meta |
| POST | `/webhook` | Recepción de mensajes |
| POST | `/reload` | Recarga prompts en caliente (header `X-Reload-Token`) |
| POST | `/maintenance/reset-contact` | Borra memoria de un contacto |
| GET | `/admin/*` | Panel de gestión |

## Configurar webhook en yCloud

- **Callback URL:** `https://TU_DOMINIO/webhook`
- **Verify token / Webhook secret:** el valor de `YCLOUD_WEBHOOK_SECRET`
- **Evento:** `whatsapp.inbound.message`

## Seguridad

Antes de cualquier commit:

```bash
rg -n "sk-|ghp_|EAA|token_real|password_real|secret_real|api_key_real" .
```

No deben existir secretos reales. `.env`, `.mcp.json` y `execution/` están en `.gitignore`.
