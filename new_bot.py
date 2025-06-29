import os
import uuid
import pytz
import asyncpg
import logging
import datetime
from dotenv import load_dotenv

from telegram import Update, ChatInviteLink
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, JobQueue
from telegram.error import Forbidden

# ──────────── НАСТРОЙКИ ────────────
load_dotenv()

print("Скрипт начал выполнение!")

DATABASE_URL = os.getenv("DATABASE_URL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 5744533263
CHANNEL_ID = -1002673430364
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

approved_usernames = {
    "pankrat00", "milena_lifestyle1", "simonaee", "majjjya", "Alexart123",
    "nirta_66", "fekaloud", "nastyushkiiins", "anakrasln", "srgv_v",
    "ashkinarylit", "autoacadem10", "avirmary", "katei1"
}

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)


# ──────────── Подключение к базе ────────────
async def get_db_pool():
    return await asyncpg.create_pool(DATABASE_URL)


# ──────────── Команда /start ────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    now = datetime.datetime.utcnow()
    args = context.args

    if not username:
        await update.message.reply_text("У тебя не указан username. Добавь его в настройках Telegram.")
        return

    if username not in approved_usernames:
        await update.message.reply_text("Ты не в списке учеников АвтоАкадемии. Доступ запрещён.")
        return

    async with context.application.bot_data["db"].acquire() as conn:
        if args:
            token = args[0]
            row = await conn.fetchrow("SELECT * FROM tokens WHERE token = $1", token)
            if not row or row["username"] != username:
                await update.message.reply_text("❌ Неверный или чужой токен.")
                return
            if row["expires"] < now:
                await update.message.reply_text("⏰ Токен истёк.")
                return
            if row["used"]:
                await update.message.reply_text("⚠️ Этот токен уже использован.")
                return

            await conn.execute("UPDATE tokens SET used = TRUE, user_id = $1 WHERE token = $2", user.id, token)
            await update.message.reply_text("✅ Ты успешно вошёл. Добро пожаловать!")
            return

        row = await conn.fetchrow("""
            SELECT * FROM tokens
            WHERE username = $1 AND used = FALSE AND expires > $2
            LIMIT 1
        """, username, now)

        if row:
            expires_msk = row["expires"].replace(tzinfo=pytz.utc).astimezone(MOSCOW_TZ)
            await update.message.reply_text(
                f"🔑 У тебя уже есть активная ссылка:\n{row['invite_link']}\n"
                f"Срок действия: до {expires_msk.strftime('%Y-%m-%d %H:%M:%S %Z')}"
            )
            return

        token = uuid.uuid4().hex[:8]
        expires = now + datetime.timedelta(hours=1)
        subscription_ends = now + datetime.timedelta(minutes=10)
        invite: ChatInviteLink = await context.bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            expire_date=expires,
            member_limit=1
        )

        await conn.execute("""
            INSERT INTO tokens (token, username, user_id, invite_link, expires, subscription_ends, used)
            VALUES ($1, $2, $3, $4, $5, $6, FALSE)
        """, token, username, user.id, invite.invite_link, expires, subscription_ends)

        expires_msk = expires.replace(tzinfo=pytz.utc).astimezone(MOSCOW_TZ)
        await update.message.reply_text(
            f"✅ Привет, {user.first_name}!\n"
            f"Вот твоя ссылка для входа в канал:\n{invite.invite_link}\n"
            f"Срок действия: до {expires_msk.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )


# ──────────── Команда /stats ────────────
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Доступ запрещён.")
        return

    async with context.application.bot_data["db"].acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM tokens")
        unused = await conn.fetchval("SELECT COUNT(*) FROM tokens WHERE used = FALSE")
        await update.message.reply_text(f"Всего токенов: {total}\nНеиспользованных: {unused}")


# ──────────── Команда /remove ────────────
async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Доступ запрещён.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /remove username")
        return

    username = context.args[0].lstrip("@")
    async with context.application.bot_data["db"].acquire() as conn:
        await conn.execute("DELETE FROM tokens WHERE username = $1", username)
        await update.message.reply_text(f"Токены для @{username} удалены.")


# ──────────── Команда /reissue ────────────
async def reissue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Доступ запрещён.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /reissue username")
        return

    username = context.args[0].lstrip("@")
    if username not in approved_usernames:
        await update.message.reply_text("Пользователь не найден в списке учеников.")
        return

    now = datetime.datetime.utcnow()
    expires = now + datetime.timedelta(hours=1)
    subscription_ends = now + datetime.timedelta(minutes=10)
    token = uuid.uuid4().hex[:8]
    invite = await context.bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        expire_date=expires,
        member_limit=1
    )

    async with context.application.bot_data["db"].acquire() as conn:
        await conn.execute("DELETE FROM tokens WHERE username = $1", username)
        await conn.execute("""
            INSERT INTO tokens (token, username, user_id, invite_link, expires, subscription_ends, used)
            VALUES ($1, $2, $3, $4, $5, $6, FALSE)
        """, token, username, 0, invite.invite_link, expires, subscription_ends)

    expires_msk = expires.replace(tzinfo=pytz.utc).astimezone(MOSCOW_TZ)
    await update.message.reply_text(
        f"✅ Новый токен для @{username}:\n"
        f"{invite.invite_link}\n"
        f"Срок действия: до {expires_msk.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )


# ──────────── Удаление просроченных и предупреждение ────────────
async def kick_expired_members(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.datetime.utcnow()

    async with context.application.bot_data["db"].acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM tokens
            WHERE used = TRUE AND subscription_ends IS NOT NULL
        """)

        for row in rows:
            user_id = row["user_id"]
            subscription_ends = row["subscription_ends"]

            if not subscription_ends:
                continue

            time_left = (subscription_ends - now).total_seconds()

            # ⚠ Предупреждение за минуту
            if 50 <= time_left <= 70:
                try:
                    await context.bot.send_message(user_id, "⏳ Осталась 1 минута до окончания подписки!")
                except Exception as e:
                    logging.warning(f"⚠️ Не удалось отправить предупреждение {user_id}: {e}")

            # 🧨 Удаление
            if time_left <= 0:
                try:
                    await context.bot.ban_chat_member(CHANNEL_ID, user_id)
                    await context.bot.unban_chat_member(CHANNEL_ID, user_id)
                    await context.bot.send_message(user_id, "⏰ Твоя подписка завершена, доступ к каналу закрыт.")
                    logging.info(f"Пользователь {user_id} удалён.")
                except Exception as e:
                    logging.warning(f"❌ Ошибка удаления {user_id}: {e}")

                await conn.execute("DELETE FROM tokens WHERE user_id = $1", user_id)

# ──────────── При запуске ────────────
async def on_startup(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        logging.info("🚀 Бот запущен.")
        pool = await get_db_pool()
        app.bot_data["db"] = pool
        app.job_queue.run_repeating(kick_expired_members, interval=30, first=5)
    except Exception as e:
        logging.error(f"❌ Ошибка при запуске: {e}")
        raise


# ──────────── Команда /test ────────────
async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот работает!")


# ──────────── MAIN ────────────
if __name__ == "__main__":
    print("🟢 Скрипт начал выполнение!")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .job_queue(JobQueue())
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("remove", remove_user))
    app.add_handler(CommandHandler("reissue", reissue))
    app.add_handler(CommandHandler("test", test))

    app.post_init = on_startup
    app.run_polling()
