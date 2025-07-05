import asyncpg
import os
from dotenv import load_dotenv
import datetime

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

class Database:
    def __init__(self):
        self.pool = None

    async def connect(self):
        if self.pool is None:
            self.pool = await asyncpg.create_pool(DATABASE_URL)
    
    async def close(self):
        if self.pool:
            await self.pool.close()

    # Добавить студента, если нет — вставить, если есть — обновить join_date
    async def add_student(self, username: str):
        await self.connect()
        async with self.pool.acquire() as conn:
            now = datetime.datetime.utcnow()
            query = """
                INSERT INTO students (username, join_date)
                VALUES ($1, $2)
                ON CONFLICT (username) DO UPDATE SET join_date = EXCLUDED.join_date
            """
            await conn.execute(query, username, now)

    # Удалить студента по username
    async def delete_student(self, username: str):
        await self.connect()
        async with self.pool.acquire() as conn:
            query = "DELETE FROM students WHERE username = $1"
            await conn.execute(query, username)

    # Получить студента по username
    async def get_student(self, username: str):
        await self.connect()
        async with self.pool.acquire() as conn:
            query = "SELECT * FROM students WHERE username = $1"
            return await conn.fetchrow(query, username)

    # Обновить invite_link, invite_created_at, invite_sent_at для студента
    async def update_invite_link(self, username: str, invite_link: str):
        await self.connect()
        async with self.pool.acquire() as conn:
            now = datetime.datetime.utcnow()
            query = """
                UPDATE students 
                SET invite_link = $2, invite_created_at = $3, invite_sent_at = $3
                WHERE username = $1
            """
            await conn.execute(query, username, invite_link, now)

    # Обновить activated_at, valid_until
    async def activate_student(self, username: str, valid_until: datetime.datetime):
        await self.connect()
        async with self.pool.acquire() as conn:
            now = datetime.datetime.utcnow()
            query = """
                UPDATE students
                SET activated_at = $2, valid_until = $3
                WHERE username = $1
            """
            await conn.execute(query, username, now, valid_until)

    # Получить всех просроченных (valid_until < now)
    async def get_expired_students(self):
        await self.connect()
        async with self.pool.acquire() as conn:
            now = datetime.datetime.utcnow()
            query = "SELECT * FROM students WHERE valid_until < $1"
            return await conn.fetch(query, now)

    # Обновить kick_at — время, когда кикнули
    async def update_kick_time(self, username: str):
        await self.connect()
        async with self.pool.acquire() as conn:
            now = datetime.datetime.utcnow()
            query = "UPDATE students SET kick_at = $2 WHERE username = $1"
            await conn.execute(query, username, now)
