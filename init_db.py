import asyncpg
import asyncio
import os
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

CREATE_TABLE_QUERY = """
CREATE TABLE IF NOT EXISTS students (
    username TEXT PRIMARY KEY,
    invite_link TEXT,
    invite_created_at TIMESTAMP,
    invite_sent_at TIMESTAMP,
    activated_at TIMESTAMP,
    valid_until TIMESTAMP,
    kick_at TIMESTAMP,
    join_date TIMESTAMP
);
"""

async def init():
    # Подключаемся к базе и создаём таблицу
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute(CREATE_TABLE_QUERY)
    await conn.close()
    print("✅ Таблица 'students' успешно создана или уже существует.")

if __name__ == "__main__":
    asyncio.run(init())
