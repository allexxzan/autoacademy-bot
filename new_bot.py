import os
import uuid
import pytz
import logging
import datetime
import asyncpg
import aiohttp

# Загрузка переменных окружения из .env
from dotenv import load_dotenv

# Telegram API — основные классы и инструменты
from telegram import Update, ChatInviteLink, User
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    JobQueue, ChatMemberHandler, MessageHandler, filters
)
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest

# Подгружаем .env
load_dotenv()

# ====== Конфигурация ======
# Токен бота Telegram
BOT_TOKEN = os.getenv("BOT_TOKEN")
# URL базы данных PostgreSQL (для asyncpg)
DATABASE_URL = os.getenv("DATABASE_URL")
# Вебхук для Google Sheets (куда отправлять данные)
GOOGLE_SHEETS_WEBHOOK = os.getenv("GOOGLE_SHEETS_WEBHOOK")

# Часовой пояс Москвы для отображения времени
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# ID канала, в котором работает бот (отрицательное число — супер-группа/канал)
CHANNEL_ID = -1002673430364

# ID чата, куда слать подозрения на леваков
CURATOR_CHAT_ID = 5744533263  # Можно выбрать любого куратора

# Словарь с ID админов (ключ — ID пользователя, значение — описание)
ADMINS = {
    5744533263: "Главный куратор",
    324109605: "Александр (@allexx34)",
    8116299506: "Анна (@KuratorAutoAcademy) — Куратор АвтоАкадемии",
    754549018: "Дмитрий Булатов (@dimabu5)"
}

# Логирование — включаем DEBUG-уровень, чтобы видеть всё
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ====== Функции для работы с базой и вспомогательные ======
async def get_db_pool():
    """
    Создаёт пул соединений с PostgreSQL.
    Используем asyncpg.create_pool для удобной работы с асинхронной БД.
    """
    try:
        logger.info("Подключаемся к базе данных...")
        pool = await asyncpg.create_pool(DATABASE_URL, max_size=10)
        logger.info("Подключение к базе установлено успешно.")
        return pool
    except Exception as e:
        logger.critical(f"Ошибка подключения к базе данных: {e}", exc_info=True)
        raise

async def notify_kurators(context: ContextTypes.DEFAULT_TYPE, message: str):
    for admin_id in ADMINS:
        try:
            await context.bot.send_message(admin_id, message)
            logger.debug(f"Отправлено уведомление куратору {ADMINS[admin_id]} (ID: {admin_id})")
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление куратору {admin_id}: {e}", exc_info=True)

async def send_to_google_sheets(user_id: int, username: str, first_name: str, start_date: str, end_date: str):
    if not GOOGLE_SHEETS_WEBHOOK:
        logger.warning("🚨 GOOGLE_SHEETS_WEBHOOK не задан, данные не отправляются")
        return

    data = {
        "user_id": user_id,
        "username": username,
        "first_name": first_name,
        "subscription_start": start_date,
        "subscription_end": end_date
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(GOOGLE_SHEETS_WEBHOOK, json=data) as resp:
                if resp.status == 200:
                    logger.info(f"✅ Данные пользователя @{username} отправлены в Google Sheets")
                else:
                    logger.error(f"❌ Ошибка отправки данных в Google Sheets: HTTP {resp.status}")
        except Exception as e:
            logger.error(f"❌ Ошибка при отправке данных в Google Sheets: {e}", exc_info=True)

# ====== Старт ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username
    if not username:
        await update.message.reply_text("❗️ Нужно, чтобы в профиле был установлен username.")
        return

    username = username.lower()
    now = datetime.datetime.utcnow().replace(tzinfo=pytz.utc)

    # Проверка базы данных на наличие активной ссылки
    async with context.application.bot_data["db"].acquire() as conn:
        record = await conn.fetchrow("""
            SELECT invite_link, expires, subscription_ends
            FROM tokens
            WHERE username = $1
            ORDER BY expires DESC
            LIMIT 1
        """, username)

        if record:
            expires = record["expires"].replace(tzinfo=pytz.utc)
            invite_link = record["invite_link"]
            subscription_ends = record["subscription_ends"].replace(tzinfo=pytz.utc)

            # Если ссылка активна — выводим её
            if expires > now:
                await update.message.reply_text(
                    f"🔗 Вот твоя ссылка:\n{invite_link}\n\n"
                    f"Срок действия: до {expires.astimezone(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                    f"Подписка до: {subscription_ends.astimezone(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                    f"Пожалуйста, используй её вовремя."
                )
                return
            else:
                # Если ссылка просрочена
                await update.message.reply_text(
                    "⏳ Твоя ссылка истекла. Обратись к администратору для получения новой."
                )
                return

        # Если активной ссылки нет, создаем новую
        token = uuid.uuid4().hex[:8]  # Генерируем новый токен
        invite_expires = now + datetime.timedelta(minutes=30)  # Срок действия ссылки 30 минут
        subscription_ends = now + datetime.timedelta(hours=1)  # Подписка на 1 час

        invite_expires_ts = int(invite_expires.timestamp())  # преобразуем в timestamp

        try:
            invite: ChatInviteLink = await context.bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                expire_date=invite_expires_ts,
                member_limit=1  # Одноразовая ссылка
            )
        except Exception as e:
            await update.message.reply_text("❗️ Ошибка при создании ссылки, попробуй позже.")
            logger.error(f"Ошибка создания ссылки для {username}: {e}", exc_info=True)
            return

        # Записываем новую ссылку в базу данных
        async with context.application.bot_data["db"].acquire() as conn:
            await conn.execute("""
                INSERT INTO tokens (token, username, invite_link, expires, subscription_ends)
                VALUES ($1, $2, $3, $4, $5)
            """, token, username, invite.invite_link, invite_expires, subscription_ends)

        # Отправляем ссылку пользователю
        await update.message.reply_text(
            f"🔗 Вот твоя новая ссылка:\n{invite.invite_link}\n\n"
            f"Срок действия: до {invite_expires.astimezone(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"Подписка до: {subscription_ends.astimezone(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"Пожалуйста, используй её вовремя."
        )
        return

# ====== Обработчик смены статуса участника в чате (например, вступление) ======
async def handle_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает событие, когда пользователь меняет статус в чате (вступает, выходит и т.д.)
    Проверяет подписку и уведомляет кураторов о нарушениях.
    """
    chat_member = update.my_chat_member
    user = chat_member.new_chat_member.user
    user_id = user.id
    username = user.username or f"ID_{user_id}"
    now = datetime.datetime.utcnow().replace(tzinfo=pytz.utc)

    if user.is_bot:
        logger.debug(f"Игнорируем бота @{username}")
        return

    async with context.application.bot_data["db"].acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM tokens WHERE user_id = $1 LIMIT 1", user_id)

        if row:
            subscription_ends = row["subscription_ends"].replace(tzinfo=pytz.utc)
            used = row["used"]
            joined = row.get("joined", False)

            # Если подписка истекла — уведомляем кураторов
            if subscription_ends < now:
                msg = (
                    f"⚠️ Пользователь @{username} (ID: {user_id}) вошёл в канал, "
                    f"но подписка истекла {subscription_ends.astimezone(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}.\n"
                    "Проверьте и при необходимости удалите."
                )
                await notify_kurators(context, msg)
                logger.info(f"Пользователь @{username} с истекшей подпиской вошёл в канал.")
                return

            # Если юзер впервые вошёл — обновляем статус в базе и приветствуем
            if not joined:
                await conn.execute("""
                    UPDATE tokens SET used = TRUE, joined = TRUE, joined_at = $2 WHERE user_id = $1
                """, user_id, now)

                ends_msk = subscription_ends.astimezone(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')
                try:
                    await context.bot.send_message(
                        user_id,
                        f"🎉 Отлично! Ты успешно вступил в закрытый ТГ АвтоАкадемии.\n"
                        f"⏳ Подписка закончится {ends_msk}.\n"
                        "Приятного изучения!"
                    )
                    logger.info(f"Отправлено приветственное сообщение @{username}")
                except Exception as e:
                    logger.warning(f"Не удалось отправить вступительное сообщение @{username}: {e}", exc_info=True)
        else:
            # Если юзера нет в базе и он не админ — уведомляем кураторов о чужаке
            if user_id not in ADMINS:
                msg = (
                    f"⚠️ В канал вступил неизвестный пользователь: @{username} (ID: {user_id}).\n"
                    "Его нет в базе. Проверьте самостоятельно."
                )
                await notify_kurators(context, msg)
                logger.info(f"Обнаружен чужак @{username} в канале.")

# ====== Автоматический кик по истечении подписки ======
async def kick_expired_members(context: ContextTypes.DEFAULT_TYPE):
    """
    Запускается периодически (например, раз в 5 минут).
    Проверяет всех пользователей с активными ссылками и подпиской.
    Удаляет (кикает) тех, у кого подписка истекла.
    """
    logger.info("Запускаем проверку истекших подписок")
    now_utc = datetime.datetime.utcnow().replace(tzinfo=pytz.utc)

    async with context.application.bot_data["db"].acquire() as conn:
        # Получаем всех пользователей с активной подпиской и использованных ссылок
        rows = await conn.fetch("""
            SELECT * FROM tokens
            WHERE used = TRUE AND subscription_ends IS NOT NULL AND user_id != 0
        """)

        for row in rows:
            user_id = row["user_id"]
            username = row["username"]
            sub_ends = row["subscription_ends"].replace(tzinfo=pytz.utc)

            # Проверяем, находится ли пользователь в канале
            try:
                member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
                is_in_chat = member.status in ['member', 'restricted']
            except BadRequest as e:
                is_in_chat = False if "user not found" in str(e).lower() else True

            time_left = (sub_ends - now_utc).total_seconds()

            # Если осталось меньше 10 минут — отправляем предупреждение
            if 0 < time_left <= 600 and is_in_chat:
                try:
                    await context.bot.send_message(
                        user_id,
                        "⚠️ Срок действия твоей подписки скоро истекает."
                    )
                    logger.info(f"Предупреждение отправлено @{username}")
                except Exception as e:
                    logger.warning(f"Не удалось отправить предупреждение @{username}: {e}", exc_info=True)

            # Если срок подписки истёк — кикаем пользователя
            if time_left <= 0 and is_in_chat:
                try:
                    # Баним на 30 секунд, чтобы кикнуть
                    await context.bot.ban_chat_member(CHANNEL_ID, user_id, until_date=int(now_utc.timestamp()) + 30)
                    # Обновляем статус токена в базе — используем FALSE, чтобы пометить, что пользователь неактивен
                    await conn.execute("UPDATE tokens SET used = FALSE WHERE user_id = $1", user_id)
                    logger.info(f"Пользователь @{username} удалён по окончании подписки")

                    # Отправляем уведомление пользователю о завершении подписки
                    try:
                        await context.bot.send_message(
                            user_id,
                            "Привет! Твоя подписка истекла. Благодарим, что был с нами.\nТвоя АвтоАкадемия :)"
                        )
                    except Exception as e:
                        # Если не удалось отправить сообщение после кика — просто логируем, не критично
                        logger.warning(f"Не удалось отправить сообщение после кика @{username}: {e}", exc_info=True)

                except Exception as e:
                    # Ошибка во время самого кика — важная, логируем как ошибку
                    logger.error(f"Ошибка кика @{username}: {e}", exc_info=True)

        # После обработки подписок — проверяем наличие "чужих" (нелегальных) участников в канале
        logger.info("Проверка на нелегальных участников")
        try:
            # Получаем список админов канала через API Telegram
            admins = await context.bot.get_chat_administrators(CHANNEL_ID)
            admin_ids = {admin.user.id for admin in admins}  # Множество ID админов
        except Exception as e:
            logger.error(f"Не удалось получить список админов: {e}", exc_info=True)
            return

        # ID админов из конфигурации и из канала — исключаем их из проверки
        EXCEPTION_IDS = set(ADMINS.keys())
        EXCEPTIONS = admin_ids.union(EXCEPTION_IDS)

        # Получаем из базы всех с активной подпиской (used=TRUE и не истекшей)
        allowed_ids = {row["user_id"] for row in await conn.fetch("""
            SELECT user_id FROM tokens
            WHERE used = TRUE AND subscription_ends > $1 AND user_id IS NOT NULL
        """, now_utc)}

        # Все пользователи, известные базе (чьи user_id есть)
        all_known = await conn.fetch("SELECT user_id FROM tokens WHERE user_id IS NOT NULL")
        known_ids = {row["user_id"] for row in all_known}

        # Проходим по всем известным user_id
        for user_id in known_ids:
            # Если пользователь в списке разрешённых или админ — пропускаем
            if user_id in allowed_ids or user_id in EXCEPTIONS:
                continue

            try:
                member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
                # Если пользователь в статусе "member" или "restricted", то он в канале
                if member.status in ['member', 'restricted']:
                    username = member.user.username or f"ID_{user_id}"
                    logger.info(f"Обнаружен чужак @{username} (ID: {user_id})")

                    # Уведомляем кураторов о появлении чужака в канале
                    msg = (
                        f"⚠️ В канал вступил неизвестный пользователь: @{username} (ID: {user_id}).\n"
                        "Его нет в активных подписках. Проверьте и при необходимости удалите."
                    )
                    await notify_kurators(context, msg)
            except Exception as e:
                logger.warning(f"Не удалось обработать участника ID {user_id}: {e}", exc_info=True)

# ====== Команда /sendlink — полное очищение данных и выдача новой одноразовой ссылки приглашения ======
async def sendlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMINS:
        await update.message.reply_text("⛔️ Доступ запрещён.")
        return

    if not context.args:
        await update.message.reply_text("Используй: /sendlink @username")
        return

    username = context.args[0].lstrip("@").lower()

    async with context.application.bot_data["db"].acquire() as conn:
        # Удаляем старые ссылки пользователя
        await conn.execute("""
            DELETE FROM tokens WHERE username = $1
        """, username)

        # Генерируем новую ссылку
        invite_expires = datetime.datetime.utcnow().replace(tzinfo=pytz.utc) + datetime.timedelta(minutes=30)
        subscription_ends = datetime.datetime.utcnow().replace(tzinfo=pytz.utc) + datetime.timedelta(hours=1)
        invite_expires_ts = int(invite_expires.timestamp())

        try:
            invite: ChatInviteLink = await context.bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                expire_date=invite_expires_ts,
                member_limit=1
            )
        except Exception as e:
            await update.message.reply_text("❌ Ошибка при создании ссылки. Попробуйте позже.")
            logger.error(f"Ошибка создания ссылки для @{username}: {e}", exc_info=True)
            return

        # Записываем новую ссылку в базу
        await conn.execute("""
            INSERT INTO tokens (token, username, invite_link, expires, subscription_ends)
            VALUES ($1, $2, $3, $4, $5)
        """, uuid.uuid4().hex[:8], username, invite.invite_link, invite_expires, subscription_ends)

    # Отправляем сообщение с новой ссылкой
    await update.message.reply_text(
        f"♻️ Ссылка для @{username} обновлена.\n"
        f"Срок действия ссылки: {invite_expires.astimezone(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"Подписка действует до: {subscription_ends.astimezone(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"Попроси ученика ввести /start и использовать ссылку."
    )

# ====== Команда /addstudent — добавление нового ученика в список ======
async def add_student(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Админская команда для добавления нового ученика (username).
    Добавляет в базу и обновляет локальный кэш approved_usernames.
    """
    if update.effective_user.id not in ADMINS:
        await update.message.reply_text("⛔️ Доступ запрещён.")
        logger.warning(f"Пользователь {update.effective_user.id} попытался использовать /addstudent без доступа")
        return

    if len(context.args) < 1:
        await update.message.reply_text("Используй: /addstudent @username")
        return

    username = context.args[0].lstrip("@").lower()

    # Проверяем, есть ли уже в списке
    if username in context.application.bot_data.get("approved_usernames", set()):
        await update.message.reply_text(f"Пользователь @{username} уже в списке учеников.")
        return

    async with context.application.bot_data["db"].acquire() as conn:
        # Вставляем в таблицу students
        await conn.execute("INSERT INTO students (username) VALUES ($1)", username)
        logger.info(f"Добавлен новый ученик @{username}")

    # Обновляем локальный кэш
    context.application.bot_data["approved_usernames"].add(username)

    await update.message.reply_text(
        f"✅ Пользователь @{username} добавлен в список учеников.\nОн сможет получить доступ через /start."
    )
    logger.info(f"Пользователь @{username} добавлен администратором {update.effective_user.id}")

# ====== Команда /stats — статистика токенов ======

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Выводит статистику по токенам: всего, использованных, активных и истекших.
    Доступна только админам.
    """
    if update.effective_user.id not in ADMINS:
        await update.message.reply_text("⛔️ Доступ запрещён.")
        logger.warning(f"Пользователь {update.effective_user.id} попытался использовать /stats без доступа")
        return

    async with context.application.bot_data["db"].acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM tokens")
        used = await conn.fetchval("SELECT COUNT(*) FROM tokens WHERE used = TRUE")
        unused = await conn.fetchval("SELECT COUNT(*) FROM tokens WHERE used = FALSE")
        active = await conn.fetchval("SELECT COUNT(*) FROM tokens WHERE used = TRUE AND subscription_ends > now()")
        expired = await conn.fetchval("SELECT COUNT(*) FROM tokens WHERE used = TRUE AND subscription_ends <= now()")

    await update.message.reply_text(
        f"📊 Статистика токенов:\n"
        f"• Всего: {total}\n"
        f"• ✅ Использованных: {used}\n"
        f"• 🕸 Неиспользованных: {unused}\n"
        f"• 🟢 Активных: {active}\n"
        f"• 🔴 Истекших: {expired}"
    )
    logger.info(f"Отправлена статистика пользователю {update.effective_user.id}")

# ====== Главная точка входа — запуск бота вручную без run_polling ======
async def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    db_pool = await get_db_pool()
    application.bot_data["db"] = db_pool

    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT username FROM students")
        approved = {row["username"].lower() for row in rows}
        application.bot_data["approved_usernames"] = approved
        logger.info(f"✅ Загружено учеников: {len(approved)}")

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("sendlink", sendlink))
    application.add_handler(CommandHandler("addstudent", add_student))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(ChatMemberHandler(handle_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    job_queue = application.job_queue
    job_queue.run_repeating(kick_expired_members, interval=300, first=10)

    logger.info("🚀 Бот запущен!")

    # ===== 🧠 Ручной запуск =====
    await application.initialize()
    await application.start()
    await application.updater.start_polling()  # не run_polling()

if __name__ == "__main__":
    import asyncio

    loop = asyncio.get_event_loop()
    loop.create_task(main())
    loop.run_forever()
