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

# ─────────────── НАСТРОЙКИ ───────────────
load_dotenv()
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

# ─────────────── БАЗА ───────────────
async def get_db_pool():
    return await asyncpg.create_pool(DATABASE_URL)

# ─────────────── /START ───────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    now = datetime.datetime.utcnow()

    if not username:
        await update.message.reply_text("У тебя не указан username. Добавь его в настройках Telegram.")
        return

    if username not in approved_usernames:
        await update.message.reply_text("Ты не в списке учеников АвтоАкадемии. Доступ запрещён.")
        return

    async with context.application.bot_data["db"].acquire() as conn:
        # 1. Есть ли активная подписка?
        row = await conn.fetchrow("""
            SELECT * FROM tokens
            WHERE username = $1 AND used = TRUE AND subscription_ends > $2
            LIMIT 1
        """, username, now)

        if row:
            ends_msk = row["subscription_ends"].replace(tzinfo=pytz.utc).astimezone(MOSCOW_TZ)
            await update.message.reply_text(
                f"🔐 У тебя уже есть доступ до {ends_msk.strftime('%Y-%m-%d %H:%M:%S %Z')}.\n"
                f"Если есть вопросы — обратись к администратору."
            )
            return

        # 2. Проверяем, выдавалась ли уже ссылка
        prev_token = await conn.fetchrow("""
            SELECT * FROM tokens
            WHERE username = $1
            LIMIT 1
        """, username)

        if prev_token:
            await update.message.reply_text("⚠️ Ссылка уже была выдана ранее. Повторная выдача невозможна.\nОбратитесь к администратору.")
            return

        # 3. Генерация новой ссылки (первая попытка)
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
            await update.message.reply_text("Ошибка при создании ссылки. Попробуйте позже.")
            return

        await conn.execute("""
            INSERT INTO tokens (token, username, user_id, invite_link, expires, subscription_ends, used)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE)
        """, token, username, user.id, invite.invite_link, expires, subscription_ends)

        ends_msk = subscription_ends.replace(tzinfo=pytz.utc).astimezone(MOSCOW_TZ)
        await update.message.reply_text(
            f"✅ Добро пожаловать, {user.first_name}!\n"
            f"Ссылка для входа: {invite.invite_link}\n"
            f"Подписка активна до: {ends_msk.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )
        logging.info(f"Выдан доступ @{username} до {subscription_ends}")

# ─────────────── УВЕДОМЛЕНИЕ И АВТОКИК ───────────────
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

            # ⚠️ Предупреждение ЗА МИНУТУ, только если человек ВСТУПИЛ в канал
            if 50 <= time_left <= 70:
                try:
                    member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
                    if member.status in ['member', 'administrator']:
                        await context.bot.send_message(user_id, "⏳ Осталась 1 минута до окончания подписки!")
                except Exception as e:
                    logging.warning(f"Не удалось отправить предупреждение {user_id}: {e}")

            # ⛔️ Подписка истекла — кикаем, если он в канале
            if time_left <= 0:
                try:
                    member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
                    if member.status in ['member', 'administrator']:
                        await context.bot.ban_chat_member(CHANNEL_ID, user_id)
                        await context.bot.unban_chat_member(CHANNEL_ID, user_id)
                        await context.bot.send_message(user_id, "⏰ Твоя подписка завершена, доступ к каналу закрыт.")
                        logging.info(f"Пользователь {user_id} удалён из канала.")
                except Forbidden:
                    logging.warning(f"Нет прав на удаление пользователя {user_id}")
                except Exception as e:
                    logging.error(f"Ошибка при удалении пользователя {user_id}: {e}")

# ─────────────── /REISSUE ───────────────
async def reissue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Доступ запрещён.")
        return

    if not context.args:
        await update.message.reply_text("Используй: /reissue username")
        return

    username = context.args[0].lstrip("@")
    if username not in approved_usernames:
        await update.message.reply_text("❌ Пользователь не найден в списке учеников.")
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
        logging.error(f"Ошибка генерации ссылки: {e}")
        await update.message.reply_text("⚠️ Ошибка создания ссылки.")
        return

    async with context.application.bot_data["db"].acquire() as conn:
        await conn.execute("DELETE FROM tokens WHERE username = $1", username)
        await conn.execute("""
            INSERT INTO tokens (token, username, user_id, invite_link, expires, subscription_ends, used)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE)
        """, token, username, 0, invite.invite_link, expires, subscription_ends)

    ends_msk = subscription_ends.replace(tzinfo=pytz.utc).astimezone(MOSCOW_TZ)
    await update.message.reply_text(
        f"✅ Новый токен для @{username}:\n{invite.invite_link}\nПодписка до: {ends_msk.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )

# ─────────────── /STATS ───────────────
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Доступ запрещён.")
        return

    async with context.application.bot_data["db"].acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM tokens")
        used = await conn.fetchval("SELECT COUNT(*) FROM tokens WHERE used = TRUE")
        unused = await conn.fetchval("SELECT COUNT(*) FROM tokens WHERE used = FALSE")
        await update.message.reply_text(
            f"📊 Всего токенов: {total}\n"
            f"✅ Использованных: {used}\n"
            f"🕸 Неиспользованных: {unused}"
        )       

# ─────────────── При запуске ───────────────
async def on_startup(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        logging.info("🚀 Бот запущен.")

        # Создаём пул соединений с базой и кладём его в bot_data
        pool = await get_db_pool()
        app.bot_data["db"] = pool

        # Планируем автокик каждые 30 секунд
        app.job_queue.run_repeating(kick_expired_members, interval=30, first=5)
    except Exception as e:
        logging.error(f"❌ Ошибка при запуске: {e}")
        raise


# ─────────────── /test ───────────────
async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот работает!")


# ─────────────── MAIN ───────────────
if __name__ == "__main__":
    print("🟢 Скрипт начал выполнение!")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .job_queue(JobQueue())
        .build()
    )

    # Добавляем команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("reissue", reissue))
    app.add_handler(CommandHandler("test", test))

    app.post_init = on_startup
    app.run_polling()
