import asyncpg
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

CREATE_TABLE_QUERY = """
CREATE TABLE IF NOT EXISTS students (
    username TEXT PRIMARY KEY,
    full_name TEXT,
    user_id BIGINT,
    invite_link TEXT,
    invite_created_at TIMESTAMPTZ,
    invite_sent_at TIMESTAMPTZ,
    activated_at TIMESTAMPTZ,
    valid_until TIMESTAMPTZ,
    kick_at TIMESTAMPTZ,
    join_date TIMESTAMPTZ
);
"""

async def init():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute(CREATE_TABLE_QUERY)
    await conn.close()
    print("✅ Таблица 'students' успешно создана или уже существует.")

if __name__ == "__main__":
    asyncio.run(init())
