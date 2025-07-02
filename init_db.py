import os
import asyncio
import asyncpg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

async def create_tokens_table():
    try:
        async with asyncpg.connect(DATABASE_URL) as conn:
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
    except Exception as e:
        print(f"❌ Ошибка при создании таблицы tokens: {e}")

# Можно добавить функции для других таблиц, например:
# async def create_students_table():
#     ...

if __name__ == "__main__":
    asyncio.run(create_tokens_table())
