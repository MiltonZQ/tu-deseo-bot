import asyncio
import os
import sys

def load_env_file(filepath=".env"):
    if not os.path.exists(filepath):
        return
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and k not in os.environ:
                    os.environ[k] = v

load_env_file()

import asyncpg


async def clear_history():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL no encontrada en el entorno.")
        return

    connection_urls = [db_url]
    if "@db:" in db_url:
        connection_urls.append(db_url.replace("@db:", "@localhost:"))

    conn = None
    for url in connection_urls:
        try:
            conn = await asyncpg.connect(url)
            print(f"Conectado a la BD.")
            break
        except Exception as e:
            print(f"Intento de conexión a la BD falló: {e}")

    if not conn:
        print("No se pudo conectar a la base de datos.")
        return

    try:
        tables = [
            "conversations",
            "conversaciones",
            "conversation_summaries",
            "conversation_state",
            "processed_messages",
            "pending_follow_ups",
            "escalations",
        ]

        print("\n--- Conteo previo ---")
        for table in tables:
            try:
                count = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")
                print(f"Tabla '{table}': {count} registros")
            except Exception as e:
                print(f"Tabla '{table}': error al contar ({e})")

        print("\nLimpiando historial de conversación...")
        async with conn.transaction():
            for table in tables:
                try:
                    res = await conn.execute(f"DELETE FROM {table}")
                    print(f"Borrado de '{table}': {res}")
                except Exception as e:
                    print(f"Error borrando '{table}': {e}")

        print("\n--- Conteo posterior ---")
        for table in tables:
            try:
                count = await conn.fetchval(f"SELECT COUNT(*) FROM {table}")
                print(f"Tabla '{table}': {count} registros")
            except Exception as e:
                print(f"Tabla '{table}': error al contar ({e})")

        print("\n¡Historial de conversación eliminado correctamente!")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(clear_history())
