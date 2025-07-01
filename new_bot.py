import os
import uuid
import pytz
import asyncpg
import logging
import datetime
from dotenv import load_dotenv
from telegram import Update, ChatInviteLink
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, JobQueue
from telegram.error import Forbidden, BadRequest

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
    "ashkinarylit", "autoacadem10", "avirmary", "katei1", "artchis01"
}

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

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
        logging.info(f"Выдан доступ @{username} (ID: {user.id}) до {subscription_ends}")


# ─────────────── /АВТО-КИК ───────────────
async def kick_expired_members(context: ContextTypes.DEFAULT_TYPE):
    logging.info("🔔 Запуск проверки истекших подписок")
    now_utc = datetime.datetime.utcnow().replace(tzinfo=pytz.utc)

    async with context.application.bot_data["db"].acquire() as conn:
        # Шаг 1: Обновим нулевые user_id
        fixed_users = await conn.fetch("""
            UPDATE tokens
            SET user_id = (
                SELECT user_id FROM (
                    VALUES ('ashkinarylit', 773948478), ('katei1', 123456789)
                ) AS fix(username, user_id)
                WHERE fix.username = tokens.username
                LIMIT 1
            )
            WHERE user_id = 0
            RETURNING username, user_id
        """)
        if fixed_users:
            logging.info(f"🔄 Обновлены user_id вручную: {fixed_users}")

        # Шаг 2: Проверка активных подписок
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
                if "user not found" in str(e).lower():
                    logging.info(f"👤 @{username} не найден в канале.")
                    is_in_chat = False
                else:
                    logging.error(f"❌ Ошибка get_chat_member @{username}: {e}")
                    continue

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
                    logging.info(f"🚫 @{username} удалён по истечении подписки")
                except Exception as e:
                    logging.error(f"❌ Ошибка удаления @{username}: {e}")

        # Шаг 3: Удаление "нелегальных" участников
        logging.info("🔍 Проверка на нелегальных участников")
        try:
            admins = await context.bot.get_chat_administrators(CHANNEL_ID)
            admin_ids = [admin.user.id for admin in admins]
        except Exception as e:
            logging.error(f"❌ Не удалось получить список админов: {e}")
            return

        # Здесь get_chat_administrators возвращает список, а не итератор
        for admin in admins:
            user_id = admin.user.id
            if user_id in admin_ids:
                continue  # Пропускаем админов

            token = await conn.fetchrow("SELECT * FROM tokens WHERE user_id = $1", user_id)

            if not token:
                try:
                    await context.bot.ban_chat_member(CHANNEL_ID, user_id, until_date=int(now_utc.timestamp()) + 30)
                    logging.info(f"🛑 Удалён неизвестный участник ID {user_id}")
                except Exception as e:
                    logging.warning(f"❌ Ошибка удаления неизвестного участника: {e}")
                continue

            if token["subscription_ends"].replace(tzinfo=pytz.utc) < now_utc:
                try:
                    await context.bot.ban_chat_member(CHANNEL_ID, user_id, until_date=int(now_utc.timestamp()) + 30)
                    await conn.execute("UPDATE tokens SET used = FALSE WHERE user_id = $1", user_id)
                    logging.info(f"⌛ Удалён участник ID {user_id} — подписка истекла")
                except Exception as e:
                    logging.warning(f"❌ Ошибка удаления участника ID {user_id}: {e}")

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
        # Получаем ID пользователя, если команда вызвана в ответ на его сообщение
        target_user_id = update.message.reply_to_message.from_user.id if update.message.reply_to_message else None
        
        invite = await context.bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            expire_date=expires,
            member_limit=1
        )
        
        async with context.application.bot_data["db"].acquire() as conn:
            # Удаляем старые токены
            await conn.execute("DELETE FROM tokens WHERE username = $1", username)
            
            # Если target_user_id не указан, используем ID админа как временное значение
            user_id_to_store = target_user_id if target_user_id else update.message.from_user.id
            
            await conn.execute("""
                INSERT INTO tokens (token, username, user_id, invite_link, expires, subscription_ends, used)
                VALUES ($1, $2, $3, $4, $5, $6, TRUE)
            """, token, username, user_id_to_store, invite.invite_link, expires, subscription_ends)

        ends_msk = subscription_ends.replace(tzinfo=pytz.utc).astimezone(MOSCOW_TZ)
        await update.message.reply_text(
            f"✅ Новый токен для @{username}:\n{invite.invite_link}\n"
            f"Подписка до: {ends_msk.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"User ID: {user_id_to_store}"
        )
        logging.info(f"Перевыдан доступ @{username} (ID: {user_id_to_store})")

    except Exception as e:
        logging.error(f"Ошибка генерации ссылки: {e}")
        await update.message.reply_text("⚠️ Ошибка создания ссылки.")

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

        # Создаем пул соединений с базой
        pool = await get_db_pool()
        app.bot_data["db"] = pool
        logging.info("✅ Подключение к базе данных установлено")

        # Планируем автокик каждые 5 минут
        app.job_queue.run_repeating(kick_expired_members, interval=300, first=10)
        logging.info("⏳ Запущена периодическая проверка подписок (каждые 5 минут)")
    except Exception as e:
        logging.error(f"❌ Ошибка при запуске: {e}")
        raise

# ─────────────── /test ───────────────
async def test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Бот работает!")

# ─────────────── MAIN ───────────────
if __name__ == "__main__":
    # 🔒 Telegram API сам обрабатывает конфликт при запуске нескольких ботов

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
    
    # Правильное добавление временной команды для тестирования
    async def force_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await kick_expired_members(context)
        await update.message.reply_text("✅ Проверка автокика выполнена")
    
    app.add_handler(CommandHandler("force_kick", force_kick))
    
    app.post_init = on_startup
    
    # Запуск с защитой от конфликтов
    app.run_polling(
        close_loop=False,
        stop_signals=None,
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )
