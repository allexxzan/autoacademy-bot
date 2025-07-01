import os
import uuid
import pytz
import asyncpg
import logging
import datetime
from dotenv import load_dotenv
from telegram import Update, ChatInviteLink
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, JobQueue,
    ChatMemberHandler
)
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest

# ───────────── НАСТРОЙКИ ─────────────
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = 5744533263
CHANNEL_ID = -1002673430364
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

approved_usernames = {
    "pankrat00", "milena_lifestyle1", "simonaee", "majjjya", "Alexart123",
    "nirta_66", "fekaloud", "nastyushkiiins", "anakrasln", "srgv_v",
    "ashkinarylit", "autoacadem10", "avirmary", "katei1", "artchis01"
}

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ───────────── БАЗА ─────────────
async def get_db_pool():
    return await asyncpg.create_pool(DATABASE_URL)

# ───────────── /START ─────────────
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

        prev_token = await conn.fetchrow("""
            SELECT * FROM tokens
            WHERE username = $1
            LIMIT 1
        """, username)

        if prev_token:
            await update.message.reply_text("⚠️ Ссылка уже была выдана ранее. Повторная выдача невозможна.\nОбратитесь к администратору.")
            return

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
        logging.info(f"Выдан доступ @{username} (ID: {user.id}) до {subscription_ends}")

# ───────────── /АВТО-КИК ─────────────
async def kick_expired_members(context: ContextTypes.DEFAULT_TYPE):
    logging.info("🔔 Запуск проверки истекших подписок")
    now_utc = datetime.datetime.utcnow().replace(tzinfo=pytz.utc)

    async with context.application.bot_data["db"].acquire() as conn:
        # Обновление user_id вручную (если 0)
        await conn.fetch("""
            UPDATE tokens
            SET user_id = (
                SELECT user_id FROM (
                    VALUES ('ashkinarylit', 773948478), ('katei1', 123456789)
                ) AS fix(username, user_id)
                WHERE fix.username = tokens.username
                LIMIT 1
            )
            WHERE user_id = 0
        """)

        rows = await conn.fetch("""
            SELECT * FROM tokens
            WHERE used = TRUE AND subscription_ends IS NOT NULL AND user_id != 0
        """)

        for row in rows:
            user_id = row["user_id"]
            username = row["username"]
            sub_ends = row["subscription_ends"].replace(tzinfo=pytz.utc)

            try:
                member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
                is_in_chat = member.status in ['member', 'restricted']
            except BadRequest as e:
                is_in_chat = False if "user not found" in str(e).lower() else True

            time_left = (sub_ends - now_utc).total_seconds()

            if 0 < time_left <= 60 and is_in_chat:
                try:
                    await context.bot.send_message(user_id, "⚠️ Ваша подписка истекает через 1 минуту!")
                    logging.info(f"📢 Предупреждение отправлено @{username}")
                except Exception as e:
                    logging.warning(f"⚠️ Не удалось уведомить @{username}: {e}")

            if time_left <= 0 and is_in_chat:
                try:
                    await context.bot.ban_chat_member(CHANNEL_ID, user_id, until_date=int(now_utc.timestamp()) + 30)
                    await conn.execute("UPDATE tokens SET used = FALSE WHERE user_id = $1", user_id)
                    logging.info(f"🚫 @{username} удалён по окончании подписки")
                except Exception as e:
                    logging.error(f"❌ Ошибка кика @{username}: {e}")

        # ───────────── УДАЛЕНИЕ ЧУЖАКОВ ─────────────
        logging.info("🔍 Проверка на нелегальных участников")

        try:
            admins = await context.bot.get_chat_administrators(CHANNEL_ID)
            admin_ids = {admin.user.id for admin in admins}
        except Exception as e:
            logging.error(f"❌ Не удалось получить список админов: {e}")
            return

        EXCEPTION_IDS = {ADMIN_ID, 123456789, 987654321}
        EXCEPTIONS = admin_ids.union(EXCEPTION_IDS)

        known_users = await conn.fetch("""
            SELECT user_id FROM tokens
            WHERE used = TRUE AND subscription_ends > $1 AND user_id IS NOT NULL
        """, now_utc)
        known_ids = {row['user_id'] for row in known_users}
        allowed_ids = known_ids.union(EXCEPTIONS)

        all_known = await conn.fetch("SELECT user_id FROM tokens WHERE user_id IS NOT NULL")
        for row in all_known:
            user_id = row["user_id"]
            if user_id in allowed_ids:
                continue

            try:
                member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
                if member.status in ['member', 'restricted']:
                    await context.bot.ban_chat_member(CHANNEL_ID, user_id, until_date=int(now_utc.timestamp()) + 30)
                    logging.info(f"🛑 Удалён чужак ID {user_id}")
                    await context.bot.send_message(ADMIN_ID, f"⚠️ В канал вступил и был удалён неизвестный ID: {user_id}")
            except Exception as e:
                logging.warning(f"⚠️ Не удалось обработать участника ID {user_id}: {e}")

# ───────────── ВСТУПЛЕНИЕ В КАНАЛ ─────────────
async def handle_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_member = update.my_chat_member
    user = chat_member.new_chat_member.user

    if chat_member.new_chat_member.status == ChatMemberStatus.MEMBER:
        user_id = user.id
        username = user.username or f"ID_{user_id}"

        async with context.application.bot_data["db"].acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tokens WHERE user_id = $1 AND used = TRUE", user_id
            )

            if not row and user_id not in {ADMIN_ID, 123456789}:
                try:
                    await context.bot.send_message(
                        ADMIN_ID,
                        f"⚠️ В канал вступил неизвестный пользователь: @{username} (ID: {user_id})"
                    )
                except Exception as e:
                    logging.error(f"❌ Не удалось уведомить админа: {e}")

                try:
                    await context.bot.ban_chat_member(CHANNEL_ID, user_id, until_date=int(datetime.datetime.utcnow().timestamp()) + 30)
                    logging.info(f"🛑 Кикнут чужак @{username}")
                except Exception as e:
                    logging.error(f"❌ Не удалось кикнуть @{username}: {e}")

# ───────────── /SENDLINK ─────────────
async def sendlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔️ Доступ запрещён.")
        return

    if not context.args:
        await update.message.reply_text("Используй: /sendlink @username")
        return

    username = context.args[0].lstrip("@")

    async with context.application.bot_data["db"].acquire() as conn:
        if username not in approved_usernames:
            await update.message.reply_text("❌ Пользователь не найден в списке учеников.")
            return

        # Получаем user_id из последнего токена, даже просроченного
        row = await conn.fetchrow("""
            SELECT user_id FROM tokens 
            WHERE username = $1 AND user_id IS NOT NULL AND user_id != 0
            ORDER BY id DESC LIMIT 1
        """, username)

        user_id = row["user_id"] if row else None

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
            await update.message.reply_text("⚠️ Не удалось создать ссылку.")
            return

        # Записываем новый токен с user_id, если известен
        await conn.execute("""
            INSERT INTO tokens (token, username, user_id, invite_link, expires, subscription_ends, used)
            VALUES ($1, $2, $3, $4, $5, $6, TRUE)
        """, token, username, user_id or 0, invite.invite_link, expires, subscription_ends)

        ends_msk = subscription_ends.replace(tzinfo=pytz.utc).astimezone(MOSCOW_TZ)

        if user_id:
            try:
                await context.bot.send_message(user_id, f"👋 Привет! Твоя новая ссылка для входа: {invite.invite_link}")
                await update.message.reply_text(f"✅ Ссылка отправлена пользователю @{username} в личку.")
            except Exception as e:
                logging.warning(f"❌ Не удалось отправить сообщение @{username}: {e}")
                await update.message.reply_text(
                    f"⚠️ Не удалось отправить ссылку в личку. Возможно, пользователь не писал боту."
                )
        else:
            await update.message.reply_text(
                f"⚠️ Пользователь @{username} ещё не писал боту. Передай ссылку вручную:\n{invite.invite_link}"
            )

        logging.info(f"🔁 Повторно выдана ссылка @{username} (user_id: {user_id}) до {ends_msk}")

# ───────────── /STATS ─────────────
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

# ───────────── СТАРТ БОТА ─────────────
async def on_startup(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        logging.info("🚀 Бот запущен.")

        pool = await get_db_pool()
        app.bot_data["db"] = pool
        logging.info("✅ Подключение к базе данных установлено")

        app.job_queue.run_repeating(kick_expired_members, interval=300, first=10)
        logging.info("⏳ Запущена проверка подписок (каждые 5 минут)")
    except Exception as e:
        logging.error(f"❌ Ошибка запуска: {e}")
        raise

# ───────────── ДОБАВЛЯЕМ КОМАНДЫ ─────────────

# 💡 Сначала определяем функции:
async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот работает!")

async def force_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await kick_expired_members(context)
    await update.message.reply_text("✅ Автокик запущен вручную")

# ───────────── MAIN ─────────────
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
    app.add_handler(CommandHandler("sendlink", sendlink))
    app.add_handler(CommandHandler("test", test))
    app.add_handler(CommandHandler("force_kick", force_kick))
    app.add_handler(ChatMemberHandler(handle_chat_member, chat_member_types=ChatMemberHandler.MY_CHAT_MEMBER))

    app.post_init = on_startup

    app.run_polling(
        close_loop=False,
        stop_signals=None,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )
