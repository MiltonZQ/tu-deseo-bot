"""Carga de variables de entorno y prompts desde disco.

Configuracion para Tu Deseo — Sex Shop & Bienestar Sexual.
Basado en la plantilla Demo chat Agentico (sin Cal.com) + vars de pago.
"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dotenv import load_dotenv

load_dotenv()

# ── WhatsApp (proveedor: yCloud por defecto, Meta soportado) ──
WHATSAPP_API_TOKEN = os.getenv("WHATSAPP_API_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
WHATSAPP_PROVIDER = os.getenv("WHATSAPP_PROVIDER", "ycloud").strip().lower()
YCLOUD_API_BASE_URL = os.getenv("YCLOUD_API_BASE_URL", "https://api.ycloud.com")
YCLOUD_API_KEY = os.getenv("YCLOUD_API_KEY", "")
YCLOUD_WHATSAPP_FROM = os.getenv("YCLOUD_WHATSAPP_FROM", "")
YCLOUD_WEBHOOK_SECRET = os.getenv("YCLOUD_WEBHOOK_SECRET", "")

# ── IA / Modelos ──
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "openai/gpt-4.1-mini")  # chat conversacional GPT-4.1 mini (vía OpenRouter) — no-thinking, rápido (1.4s), responde completo
# Modelo de respaldo: si el principal falla (deprecation, caída del provider),
# el bot reintenta automáticamente con este para no quedarse sin responder.
OPENAI_MODEL_FALLBACK = os.getenv("OPENAI_MODEL_FALLBACK", "openai/gpt-4o-mini").strip() or None
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip() or None
OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o")  # comprobantes
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "whisper-1")  # notas de voz
# Effort del razonamiento para modelos que lo soportan (GLM-5.x, etc.).
# "low" = control de costo y latencia; valores: low/medium/high o vacío para omitir.
MODEL_REASONING_EFFORT = os.getenv("MODEL_REASONING_EFFORT", "low").strip().lower() or None
# Excluir el razonamiento (chain-of-thought) de la respuesta para modelos "thinking"
# como GPT-5.2 o Gemini 3.x. El pipeline determinístico ya hace el razonamiento en
# Python, así que aquí solo se necesita la redacción final.
MODEL_REASONING_EXCLUDE = os.getenv("MODEL_REASONING_EXCLUDE", "false").strip().lower() in {
    "1", "true", "yes", "on"
}
# Máximo de tokens para la respuesta del LLM. Generoso para que el bot pueda redactar
# saludo + 5 marcadores [FOTO:ID] + CTA sin cortarse, pero acotado para control de costo.
MAX_REPLY_TOKENS = int(os.getenv("MAX_REPLY_TOKENS", "2000"))

# ── Base de datos ──
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ── Identidad y comportamiento del bot ──
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Tu Deseo")
BUSINESS_TAGLINE = os.getenv("BUSINESS_TAGLINE", "Sex Shop & Bienestar Sexual")
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", "America/Bogota")

HISTORY_WINDOW = int(os.getenv("HISTORY_WINDOW", "30"))
MAX_PROMPT_TOKENS = int(os.getenv("MAX_PROMPT_TOKENS", "20000"))
MAX_USER_MESSAGE_CHARS = int(os.getenv("MAX_USER_MESSAGE_CHARS", "4000"))
HISTORY_TTL_DAYS = int(os.getenv("HISTORY_TTL_DAYS", "30"))
# Cuándo consolidar el historial en un resumen persistido (memoria comprimida).
# Se regenera al superar este nº de mensajes, reinyectándose luego en el prompt.
# IMPORTANTE: debe ser SIEMPRE menor que HISTORY_WINDOW para que se dispare:
# total_msgs = len(history) + 2 <= HISTORY_WINDOW + 2, y el resumen se crea al
# superar este umbral. Con HISTORY_WINDOW=30, SUMMARY_THRESHOLD=18 garantiza
# que la memoria de largo plazo se regenere periódicamente.
SUMMARY_THRESHOLD = int(os.getenv("SUMMARY_THRESHOLD", "18"))

# ── Operaciones ──
RELOAD_TOKEN = os.getenv("RELOAD_TOKEN", "")
ENABLE_FOLLOW_UPS = os.getenv("ENABLE_FOLLOW_UPS", "false").strip().lower() in {
    "1", "true", "yes", "on"
}
FOLLOW_UP_MINUTES = int(os.getenv("FOLLOW_UP_MINUTES", "10"))

# ── Panel admin ──
ADMIN_USER = os.getenv("ADMIN_USER", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
TITLE = os.getenv("TITLE", "Tu Deseo — Panel")

# ── Pagos / Comprobantes (porteado de lógica Lavadero/Belux) ──
# Bancos colombianos aceptados para comprobantes
ACCEPTED_BANKS = os.getenv(
    "ACCEPTED_BANKS",
    "Nequi, Bancolombia, Daviplata, Bre-B, PSE"
)
# Cuentas destino del negocio (para validar destinatario en el comprobante)
PAYMENT_ACCOUNT_BANCOLOMBIA = os.getenv("PAYMENT_ACCOUNT_BANCOLOMBIA", "")
PAYMENT_ACCOUNT_NEQUI = os.getenv("PAYMENT_ACCOUNT_NEQUI", "")
PAYMENT_ACCOUNT_DAVIPLATA = os.getenv("PAYMENT_ACCOUNT_DAVIPLATA", "")
PAYMENT_BRE_B_HANDLE = os.getenv("PAYMENT_BRE_B_HANDLE", "")
# Máx intentos de comprobante inválido antes de escalar a humano
PAYMENT_MAX_ATTEMPTS = int(os.getenv("PAYMENT_MAX_ATTEMPTS", "2"))
# Ventana de conteo de intentos (horas)
PAYMENT_ATTEMPTS_WINDOW_HOURS = int(os.getenv("PAYMENT_ATTEMPTS_WINDOW_HOURS", "24"))

# ── Pasarela Bold (Semana 2) ──
BOLD_API_KEY = os.getenv("BOLD_API_KEY", "")
BOLD_API_BASE_URL = os.getenv("BOLD_API_BASE_URL", "https://api.bold.co")
BOLD_WEBHOOK_SECRET = os.getenv("BOLD_WEBHOOK_SECRET", "")
BOLD_INTEGRATION_ENABLED = os.getenv("BOLD_INTEGRATION_ENABLED", "false").strip().lower() in {
    "1", "true", "yes", "on"
}

# ── SIDDE POS (sincronización catálogo) ──
SIDDE_POS_API_URL = os.getenv("SIDDE_POS_API_URL", "")
SIDDE_POS_API_KEY = os.getenv("SIDDE_POS_API_KEY", "")
SIDDE_POS_ENABLED = os.getenv("SIDDE_POS_ENABLED", "false").strip().lower() in {
    "1", "true", "yes", "on"
}

# ── WooCommerce (sincronización tienda web e imágenes) ──
WOOCOMMERCE_URL = os.getenv("WOOCOMMERCE_URL", "")
WOOCOMMERCE_CONSUMER_KEY = os.getenv("WOOCOMMERCE_CONSUMER_KEY", "")
WOOCOMMERCE_CONSUMER_SECRET = os.getenv("WOOCOMMERCE_CONSUMER_SECRET", "")
WOOCOMMERCE_WEBHOOK_SECRET = os.getenv("WOOCOMMERCE_WEBHOOK_SECRET", "")
WOOCOMMERCE_SYNC_ENABLED = os.getenv("WOOCOMMERCE_SYNC_ENABLED", "false").strip().lower() in {
    "1", "true", "yes", "on"
}

# ── URL opcional para leads calificados (checkout, landing) ──
QUALIFIED_CTA_URL = os.getenv("QUALIFIED_CTA_URL", "")

PROMPTS_DIR = Path(os.getenv("PROMPTS_DIR", "prompts"))

SYSTEM_PROMPT: str = ""

FALLBACK_PROMPT = (
    "Eres el asistente de ventas de Tu Deseo, sex shop y bienestar sexual. "
    "Responde en español, de forma empática, educativa y cercana. Se breve."
)


def load_prompts() -> str:
    """Lee prompts/system.md + prompts/knowledge/*.md y construye el system prompt.

    NOTA: catalogo.md se EXCLUYE deliberadamente del prompt estático (~27k tokens).
    El catálogo se inyecta por turno solo con los 4-5 candidatos que el sistema
    recupera de la DB (pipeline determinístico), reduciendo costo, latencia y
    las alucinaciones de IDs que generaba el prompt gigante.
    """
    global SYSTEM_PROMPT

    system_file = PROMPTS_DIR / "system.md"
    if system_file.exists():
        base = system_file.read_text(encoding="utf-8").strip()
    else:
        base = FALLBACK_PROMPT
    base = base.replace("{{BUSINESS_NAME}}", BUSINESS_NAME)

    knowledge_dir = PROMPTS_DIR / "knowledge"
    extras: list[str] = []
    if knowledge_dir.is_dir():
        for md in sorted(knowledge_dir.glob("*.md")):
            if md.name == "catalogo.md":
                continue  # catálogo gestionado por turno vía candidatos (pipeline)
            content = md.read_text(encoding="utf-8").strip()
            if content:
                extras.append(f"--- {md.name} ---\n{content}")
        for txt in sorted(knowledge_dir.glob("*.txt")):
            content = txt.read_text(encoding="utf-8").strip()
            if content:
                extras.append(f"--- {txt.name} ---\n{content}")

    SYSTEM_PROMPT = base + ("\n\n" + "\n\n".join(extras) if extras else "")
    return SYSTEM_PROMPT


def validate() -> list[str]:
    """Devuelve lista de errores de configuracion (vacia = OK)."""
    missing = []
    common_keys = (
        "OPENAI_API_KEY",
        "DATABASE_URL",
        "ADMIN_USER",
        "ADMIN_PASSWORD",
        "SESSION_SECRET",
    )
    if WHATSAPP_PROVIDER == "ycloud":
        provider_keys = ("YCLOUD_API_KEY", "YCLOUD_WHATSAPP_FROM")
    else:
        provider_keys = ("WHATSAPP_API_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "VERIFY_TOKEN")
    for key in provider_keys + common_keys:
        if not globals().get(key):
            missing.append(key)
    return missing


def payment_accounts_configured() -> bool:
    """True si hay al menos una cuenta destino configurada para validar comprobantes."""
    return any([
        PAYMENT_ACCOUNT_BANCOLOMBIA,
        PAYMENT_ACCOUNT_NEQUI,
        PAYMENT_ACCOUNT_DAVIPLATA,
        PAYMENT_BRE_B_HANDLE,
    ])


def bot_zoneinfo() -> ZoneInfo:
    try:
        return ZoneInfo(BOT_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")
