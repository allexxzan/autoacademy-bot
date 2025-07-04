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
    """
    Отправляет сообщение всем кураторам (админам).
    Используется для уведомления о важных событиях, например, о входе чужого пользователя.
    """
    for admin_id in ADMINS:
        try:
            await context.bot.send_message(admin_id, message)
            logger.debug(f"Отправлено уведомление куратору {ADMINS[admin_id]} (ID: {admin_id})")
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление куратору {admin_id}: {e}", exc_info=True)

async def send_to_google_sheets(user_id: int, username: str, first_name: str, start_date: str, end_date: str):
    """
    Отправляет данные пользователя (подписки) в Google Sheets через webhook.
    Используем aiohttp для POST-запроса.
    """
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
    user = update.effective_user
    username = user.username

    if not username:
        await update.message.reply_text(
            "❗️ У тебя не указан username в Telegram. Добавь его в настройках профиля."
        )
        return

    approved = context.application.bot_data.get("approved_usernames", set())
    if username.lower() not in approved:
        await update.message.reply_text(
            "⛔️ Ты не в списке учеников АвтоАкадемии. Доступ запрещён.\n"
            "Если произошла ошибка, свяжись со своим куратором."
        )
        return

    now_utc = datetime.datetime.utcnow().replace(tzinfo=pytz.utc)

    async with context.application.bot_data["db"].acquire() as conn:
        token = await conn.fetchrow("""
            SELECT * FROM tokens
            WHERE LOWER(username) = $1
            ORDER BY created_at DESC
            LIMIT 1
        """, username.lower())

        if token:
            # 🎯 Уже получал ссылку ранее — ничего больше не даём
            await update.message.reply_text(
                "⚠️ Ты уже получил ссылку. Повторно её может выдать только куратор."
            )
            return

        # 🎯 Впервые — создаём новую ссылку
        try:
            new_invite = await context.bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                expire_date=now_utc + datetime.timedelta(minutes=30),
                member_limit=1
            )
        except Exception as e:
            logger.error(f"Ошибка создания ссылки для @{username}: {e}", exc_info=True)
            await update.message.reply_text("⚠️ Ошибка при создании ссылки. Обратись к куратору.")
            return

        new_expires = now_utc + datetime.timedelta(minutes=30)
        new_ends = now_utc + datetime.timedelta(hours=1)

        await conn.execute("""
            INSERT INTO tokens (token, username, user_id, invite_link, expires, subscription_ends, used, created_at)
            VALUES ($1, $2, NULL, $3, $4, $5, FALSE, $6)
        """, uuid.uuid4().hex[:8], username.lower(), new_invite.invite_link, new_expires, new_ends, now_utc)

        expires_msk = new_expires.astimezone(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')
        ends_msk = new_ends.astimezone(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')

        await update.message.reply_text(
            f"🔗 Вот твоя ссылка:\n{new_invite.invite_link}\n\n"
            f"Срок действия: до {expires_msk}\n"
            f"Подписка до: {ends_msk}\n"
            "Пожалуйста, используй её вовремя. Повторно получить можно только через куратора."
        )

    # 🎯 Уже есть запись — проверяем
    invite_expires = token["expires"].replace(tzinfo=pytz.utc)
    subscription_ends = token["subscription_ends"].replace(tzinfo=pytz.utc)
    used = token["used"]
    stored_user_id = token["user_id"]

    if used and stored_user_id:  # ссылка уже была использована и user_id есть
        await update.message.reply_text(
            "⚠️ Ты уже использовал свою ссылку. Новую может выдать только куратор."
        )
        return

    if invite_expires < now_utc and stored_user_id:  # просрочена и user_id уже установлен
        await update.message.reply_text(
            "⚠️ Срок действия твоей ссылки истёк. Новую может выдать только куратор."
        )
        return

    # 🎯 Всё ещё действует — просто напоминаем
    expires_msk = invite_expires.astimezone(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')
    ends_msk = subscription_ends.astimezone(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')

    await update.message.reply_text(
        f"⚠️ Ты уже получил ссылку, которая ещё действует.\n"
        f"Срок действия: до {expires_msk}\n"
        f"Подписка до: {ends_msk}\n"
        "Если ссылка не работает — обратись к куратору."
    )

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
                        "⚠️ Завтра истекает срок действия твоей подписки."
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

# ====== Команда /sendlink — выдача новой ссылки приглашения ======

async def sendlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Админская команда для сброса и выдачи новой ссылки приглашения ученику.
    Проверяет, что пользователь есть в списке, деактивирует старые ссылки,
    создаёт новую, записывает в базу и уведомляет админа.
    """
    user = update.effective_user
    if user.id not in ADMINS:
        await update.message.reply_text("⛔️ Доступ запрещён.")
        logger.warning(f"Пользователь {user.id} попытался использовать /sendlink без доступа")
        return

    # Проверяем, передан ли username аргументом
    if not context.args:
        await update.message.reply_text("Используй: /sendlink @username")
        return

    username = context.args[0].lstrip("@").lower()

    # Проверяем, есть ли пользователь в списке учеников
    if username not in context.application.bot_data.get("approved_usernames", set()):
        await update.message.reply_text("❌ Пользователь не найден в списке учеников.")
        return

    now = datetime.datetime.utcnow()
    async with context.application.bot_data["db"].acquire() as conn:
        # Проверяем, есть ли уже использованная ссылка для этого пользователя (т.е. он уже в канале)
        existing = await conn.fetchrow("""
            SELECT user_id FROM tokens
            WHERE username = $1 AND used = TRUE AND user_id IS NOT NULL
        """, username)

        if existing:
            try:
                member = await context.bot.get_chat_member(CHANNEL_ID, existing["user_id"])
                # Если пользователь уже в канале — предупреждаем админа
                if member.status in ["member", "restricted"]:
                    await update.message.reply_text(
                        "⚠️ Ссылка ранее уже была использована. Убедись, что левак покинул канал."
                    )
                    logger.warning(f"Попытка повторной выдачи, но кто-то уже в канале под @{username}")
                    return
            except Exception as e:
                logger.warning(f"Не удалось проверить участника {existing['user_id']}: {e}", exc_info=True)

        # Деактивируем старые ссылки пользователя (если были)
        old_links = await conn.fetch("SELECT invite_link FROM tokens WHERE username = $1", username)
        for link_rec in old_links:
            link = link_rec["invite_link"]
            if link:
                try:
                    await context.bot.revoke_chat_invite_link(CHANNEL_ID, link)
                    logger.info(f"Старая ссылка @{username} деактивирована.")
                except Exception as e:
                    logger.warning(f"Не удалось деактивировать старую ссылку @{username}: {e}", exc_info=True)

        # Удаляем старые записи в базе для пользователя (чтобы потом создать новую)
        await conn.execute("DELETE FROM tokens WHERE username = $1", username)

        # Создаём новую ссылку и подписку
        token = uuid.uuid4().hex[:8]
        invite_expires = now + datetime.timedelta(minutes=30)
        subscription_ends = now + datetime.timedelta(hours=1)

        try:
            invite: ChatInviteLink = await context.bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                expire_date=invite_expires,
                member_limit=1
            )
            logger.info(f"Создана новая ссылка для @{username}")
        except Exception as e:
            logger.error(f"Ошибка создания ссылки для @{username}: {e}", exc_info=True)
            await update.message.reply_text("Ошибка при создании ссылки. Попробуйте позже.")
            return

        # Вставляем новую запись в таблицу tokens
        await conn.execute("""
            INSERT INTO tokens (token, username, user_id, invite_link, expires, subscription_ends, used)
            VALUES ($1, $2, NULL, $3, $4, $5, FALSE)
        """, token, username, invite.invite_link, invite_expires, subscription_ends)

        # Форматируем даты в московском часовом поясе для удобства
        ends_msk = subscription_ends.replace(tzinfo=pytz.utc).astimezone(MOSCOW_TZ)
        expires_msk = invite_expires.replace(tzinfo=pytz.utc).astimezone(MOSCOW_TZ)

        # Отправляем администратору результат
        await update.message.reply_text(
            f"♻️ Ссылка для @{username} обновлена и сброшены все предыдущие данные.\n"
            f"Срок действия ссылки: {expires_msk.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"Подписка действует до: {ends_msk.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"Попроси ученика ввести /start и использовать ссылку."
        )
        logger.info(f"Выдана новая ссылка @{username} до {subscription_ends}")

        # Обновляем кэш approved_usernames в памяти бота
        context.application.bot_data["approved_usernames"].add(username)

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
