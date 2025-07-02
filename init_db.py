import os
import asyncio
import asyncpg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

async def create_tokens_table():
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tokens (
                    token TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    user_id BIGINT,
                    invite_link TEXT NOT NULL,
                    expires TIMESTAMPTZ NOT NULL,
                    subscription_ends TIMESTAMPTZ,
                    used BOOLEAN DEFAULT FALSE
                )
            """)
            print("✅ Таблица tokens успешно создана (или уже существует).")
        finally:
            await conn.close()
    except Exception as e:
        print(f"❌ Ошибка при создании таблицы tokens: {e}")

if __name__ == "__main__":
    asyncio.run(create_tokens_table())
