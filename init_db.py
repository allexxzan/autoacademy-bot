import os
import asyncio
import asyncpg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

async def add_subscription_ends_column():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("""
            ALTER TABLE tokens
            ADD COLUMN IF NOT EXISTS subscription_ends TIMESTAMPTZ;
        """)
        print("✅ Колонка subscription_ends успешно добавлена.")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(add_subscription_ends_column())
