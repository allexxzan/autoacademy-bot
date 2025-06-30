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
DATABASE_URL = os.getenv("DATABASE_URL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 5744533263
CHANNEL_ID = -1002673430364
MOSCOW_TZ = pytz.timezone("Europe/Moscow")

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

approved_usernames = {
    "pankrat00", "milena_lifestyle1", "simonaee", "majjjya", "Alexart123",
    "nirta_66", "fekaloud", "nastyushkiiins", "anakrasln", "srgv_v",
    "ashkinarylit", "autoacadem10", "avirmary", "katei1"
}

# ──────────── ПОДКЛЮЧЕНИЕ К БД ────────────
async def get_db_pool():
    return await asyncpg.create_pool(DATABASE_URL)


# ──────────── /start ────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    now = datetime.datetime.utcnow()

    if not username:
        await update.message.reply_text("Добавь username в настройках Telegram.")
        return

    if username not in approved_usernames:
        await update.message.reply_text("Ты не в списке учеников АвтоАкадемии. Доступ запрещён.")
        return

    async with context.application.bot_data["db"].acquire() as conn:
        # Проверка активной подписки
        active_sub = await conn.fetchrow("""
            SELECT * FROM tokens
            WHERE username = $1 AND used = TRUE AND subscription_ends > $2
            ORDER BY subscription_ends DESC
            LIMIT 1
        """, username, now)

        if active_sub:
            sub_msk = active_sub["subscription_ends"].replace(tzinfo=pytz.utc).astimezone(MOSCOW_TZ)
            await update.message.reply_text(
                f"ℹ️ У тебя уже есть активная подписка до {sub_msk.strftime('%Y-%m-%d %H:%M:%S %Z')}.\n"
                f"Если возникли трудности — обратись к админу."
            )
            return

        # Проверка неиспользованного токена
        unused = await conn.fetchrow("""
            SELECT * FROM tokens
            WHERE username = $1 AND used = FALSE AND expires > $2
            LIMIT 1
        """, username, now)

        if unused:
            expires_msk = unused["expires"].replace(tzinfo=pytz.utc).astimezone(MOSCOW_TZ)
            await update.message.reply_text(
                f"🔑 У тебя есть неиспользованная ссылка:\n{unused['invite_link']}\n"
                f"Действует до: {expires_msk.strftime('%Y-%m-%d %H:%M:%S %Z')}"
            )
            return

        # Генерация новой ссылки
        token = uuid.uuid4().hex[:8]
        expires = now + datetime.timedelta(hours=1)
        subscription_ends = now + datetime.timedelta(minutes=10)

        try:
            invite: ChatInviteLink = await context.bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                expire_date=expires,
                member_limit=1
            )
        except Exception as e:
            logging.error(f"Ошибка создания ссылки: {e}")
            await update.message.reply_text("⚠️ Ошибка создания ссылки. Попробуйте позже.")
            return

        await conn.execute("""
            INSERT INTO tokens (token, username, user_id, invite_link, expires, subscription_ends, used)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE)
        """, token, username, user.id, invite.invite_link, expires, subscription_ends)

        sub_msk = subscription_ends.replace(tzinfo=pytz.utc).astimezone(MOSCOW_TZ)
        await update.message.reply_text(
            f"✅ Добро пожаловать, {user.first_name}!\n"
            f"Ссылка: {invite.invite_link}\n"
            f"Подписка до: {sub_msk.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )


# ──────────── kick_expired_members ────────────
async def kick_expired_members(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.datetime.utcnow()

    async with context.application.bot_data["db"].acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM tokens
            WHERE used = TRUE AND subscription_ends IS NOT NULL
        """)

        for row in rows:
            user_id = row["user_id"]
            ends = row["subscription_ends"]
            time_left = (ends - now).total_seconds()

            # За минуту до окончания
            if 50 <= time_left <= 70:
                try:
                    await context.bot.send_message(user_id, "⏳ Осталась 1 минута до окончания подписки.")
                except Exception as e:
                    logging.warning(f"⚠️ Не удалось отправить предупреждение: {e}")

            # Удаление, если срок вышел
            if time_left <= 0:
                try:
                    member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
                    if member.status in ["member", "administrator"]:
                        await context.bot.ban_chat_member(CHANNEL_ID, user_id)
                        await context.bot.unban_chat_member(CHANNEL_ID, user_id)
                        await context.bot.send_message(user_id, "⏰ Подписка завершена. Доступ к каналу закрыт.")
                        logging.info(f"Пользователь {user_id} удалён.")
                    await conn.execute("DELETE FROM tokens WHERE user_id = $1", user_id)
                except Forbidden:
                    logging.warning(f"Нет прав на удаление пользователя {user_id}")
                except Exception as e:
                    logging.error(f"❌ Ошибка удаления {user_id}: {e}")


# ──────────── Админские команды ────────────
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    async with context.application.bot_data["db"].acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM tokens")
        unused = await conn.fetchval("SELECT COUNT(*) FROM tokens WHERE used = FALSE")
        await update.message.reply_text(f"Всего токенов: {total}\nНеиспользованных: {unused}")


async def reissue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.args:
        await update.message.reply_text("Использование: /reissue username")
        return

    username = context.args[0].lstrip("@")
    if username not in approved_usernames:
        await update.message.reply_text("❌ Пользователь не в базе.")
        return

    now = datetime.datetime.utcnow()
    expires = now + datetime.timedelta(hours=1)
    subscription_ends = now + datetime.timedelta(minutes=10)
    token = uuid.uuid4().hex[:8]

    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            expire_date=expires,
            member_limit=1
        )
    except Exception as e:
        logging.error(f"Ошибка создания ссылки: {e}")
        await update.message.reply_text("Ошибка при создании ссылки.")
        return

    async with context.application.bot_data["db"].acquire() as conn:
        await conn.execute("DELETE FROM tokens WHERE username = $1", username)
        await conn.execute("""
            INSERT INTO tokens (token, username, user_id, invite_link, expires, subscription_ends, used)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE)
        """, token, username, 0, invite.invite_link, expires, subscription_ends)

    await update.message.reply_text(f"✅ Новая ссылка: {invite.invite_link}")


async def on_startup(app):
    pool = await get_db_pool()
    app.bot_data["db"] = pool
    app.job_queue.run_repeating(kick_expired_members, interval=30, first=5)
    logging.info("🚀 Бот запущен и готов к работе.")


# ──────────── MAIN ────────────
if __name__ == "__main__":
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .job_queue(JobQueue())
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reissue", reissue))

    app.post_init = on_startup
    app.run_polling()
