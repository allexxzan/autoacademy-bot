import os
import asyncio
import asyncpg
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")

# ──────────── Основная функция: создание таблицы tokens ────────────
async def create_tokens_table():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            user_id BIGINT,
            invite_link TEXT NOT NULL,
            expires TIMESTAMPTZ NOT NULL,
            subscription_ends TIMESTAMPTZ,  -- поле для даты окончания подписки
            used BOOLEAN DEFAULT FALSE
        )
    """)
    await conn.close()
    print("✅ Таблица tokens успешно создана (или уже существует).")


# ──────────── Вспомогательная функция (один раз вручную, если нужно) ────────────
# async def add_subscription_ends_column():
#     conn = await asyncpg.connect(DATABASE_URL)
#     try:
#         await conn.execute("""
#             ALTER TABLE tokens
#             ADD COLUMN IF NOT EXISTS subscription_ends TIMESTAMPTZ;
#         """)
#         print("✅ Колонка subscription_ends успешно добавлена.")
#     except Exception as e:
#         print(f"❌ Ошибка при добавлении колонки: {e}")
#     finally:
#         await conn.close()


if __name__ == "__main__":
    asyncio.run(create_tokens_table())
    # asyncio.run(add_subscription_ends_column())  # ← Раскомментируй вручную, если когда-нибудь понадобится
