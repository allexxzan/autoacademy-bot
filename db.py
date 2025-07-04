import asyncpg
import os
import datetime
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(DATABASE_URL)

    # --- Получить студента ---
    async def get_student(self, username: str):
        query = "SELECT * FROM students WHERE username = $1"
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(query, username)

    # --- Добавить студента ---
    async def add_student(self, username: str):
        query = """
        INSERT INTO students (username)
        VALUES ($1)
        ON CONFLICT (username) DO NOTHING
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, username)

    # --- Удалить студента ---
    async def delete_student(self, username: str):
        query = "DELETE FROM students WHERE username = $1"
        async with self.pool.acquire() as conn:
            await conn.execute(query, username)

    # --- Сбросить ссылку (ручной запрос от админа) ---
    async def reset_link(self, username: str):
        query = """
        UPDATE students
        SET invite_link = NULL,
            invite_created_at = NULL,
            invite_sent_at = NULL
        WHERE username = $1
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, username)

    # --- Зафиксировать отправку ссылки ---
    async def record_invite_sent(self, username: str, invite_link: str, sent_at: datetime.datetime):
        query = """
        UPDATE students
        SET invite_link = $2,
            invite_created_at = $3,
            invite_sent_at = $3
        WHERE username = $1
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, username, invite_link, sent_at)

    # --- Активировать подписку ---
    async def activate_subscription(self, username: str, activated_at: datetime.datetime, valid_until: datetime.datetime):
        query = """
        UPDATE students
        SET activated_at = $2,
            valid_until = $3,
            join_date = $2
        WHERE username = $1
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, username, activated_at, valid_until)

    # --- Получить список истекших подписок ---
    async def get_expired_students(self, now: datetime.datetime):
        query = """
        SELECT username
        FROM students
        WHERE valid_until IS NOT NULL
          AND valid_until <= $1
          AND kick_at IS NULL
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, now)
            return [dict(r) for r in rows]

    # --- Пометить, что пользователь кикнут ---
    async def mark_kicked(self, username: str, kicked_at: datetime.datetime):
        query = "UPDATE students SET kick_at = $2 WHERE username = $1"
        async with self.pool.acquire() as conn:
            await conn.execute(query, username, kicked_at)

    # --- Получить статистику ---
    async def get_stats(self):
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM students")
            now = datetime.datetime.utcnow()
            active = await conn.fetchval("SELECT COUNT(*) FROM students WHERE valid_until > $1", now)
            expired = await conn.fetchval("SELECT COUNT(*) FROM students WHERE valid_until <= $1", now)
            return total, active, expired

    # --- Получить всех студентов (для отладки) ---
    async def get_all_students(self):
        query = "SELECT * FROM students"
        async with self.pool.acquire() as conn:
            return await conn.fetch(query)
