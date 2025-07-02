import os
import uuid
import pytz
import logging
import datetime
import asyncpg
import aiohttp

from dotenv import load_dotenv
from telegram import Update, ChatInviteLink, User
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    JobQueue, ChatMemberHandler, MessageHandler, filters
)
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest

load_dotenv()

# ====== Конфигурация ======
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GOOGLE_SHEETS_WEBHOOK = os.getenv("GOOGLE_SHEETS_WEBHOOK")

MOSCOW_TZ = pytz.timezone('Europe/Moscow')
CHANNEL_ID = -1002673430364

ADMINS = {
    5744533263: "Главный куратор",
    324109605: "Александр (@allexx34)",
    8116299506: "Анна (@KuratorAutoAcademy) — Куратор АвтоАкадемии",
    754549018: "Дмитрий Булатов (@dimabu5)"
}

# Логирование — максимальный уровень, формат с временем и уровнем
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
)
logger = logging.getLogger(__name__)

async def get_db_pool():
    try:
        logger.info("Подключаемся к базе данных...")
        pool = await asyncpg.create_pool(DATABASE_URL)
        logger.info("Подключение к базе установлено успешно.")
        return pool
    except Exception as e:
        logger.critical(f"Ошибка подключения к базе данных: {e}", exc_info=True)
        raise

# Часть 2: Команда /start, отправка статистики в Google Sheets
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username
    now = datetime.datetime.utcnow()

    # Проверка username
    if not username:
        await update.message.reply_text(
            "❗️ У тебя не указан username в Telegram. Добавь его в настройках профиля."
        )
        logger.info(f"Пользователь {user.id} без username попытался начать.")
        return

    # Проверка, является ли учеником
    if username.lower() not in context.application.bot_data.get("approved_usernames", set()):
        await update.message.reply_text(
            "⛔️ Ты не в списке учеников АвтоАкадемии. Доступ запрещён.\n"
            "Если произошла ошибка, свяжись со своим куратором."
        )
        logger.info(f"Пользователь @{username} не в списке учеников.")
        return

    async with context.application.bot_data["db"].acquire() as conn:
        # Проверяем активную подписку
        active = await conn.fetchrow("""
            SELECT * FROM tokens
            WHERE username = $1 AND used = TRUE AND subscription_ends > $2
            LIMIT 1
        """, username.lower(), now)

        if active:
            ends_msk = active["subscription_ends"].replace(tzinfo=pytz.utc).astimezone(MOSCOW_TZ)
            await update.message.reply_text(
                f"🔐 У тебя уже есть доступ до {ends_msk.strftime('%Y-%m-%d %H:%M:%S %Z')}.\n"
                "Если есть вопросы — обратись к своему куратору."
            )
            logger.info(f"Пользователь @{username} запросил /start, но доступ уже активен.")
            return

        # Проверяем, была ли уже выдана ссылка
        old_token = await conn.fetchrow("""
            SELECT * FROM tokens
            WHERE username = $1
            LIMIT 1
        """, username.lower())

        if old_token:
            await update.message.reply_text(
                "⚠️ Ссылка уже была выдана ранее. Повторная выдача невозможна.\n"
                "Обратитесь к своему куратору для сброса."
            )
            logger.info(f"Пользователь @{username} запросил /start, но ссылка уже была выдана.")
            return

        # Генерируем уникальную ссылку
        token = uuid.uuid4().hex[:8]
        invite_expires = now + datetime.timedelta(minutes=60)
        subscription_ends = now + datetime.timedelta(hours=1)

        try:
            invite: ChatInviteLink = await context.bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                expire_date=invite_expires,
                member_limit=1
            )
        except Exception as e:
            logger.error(f"Ошибка создания ссылки для @{username}: {e}", exc_info=True)
            await update.message.reply_text("Ошибка при создании ссылки. Попробуйте позже.")
            return

        # Сохраняем токен в базу
        await conn.execute("""
            INSERT INTO tokens (token, username, user_id, invite_link, expires, subscription_ends, used, joined)
            VALUES ($1, $2, $3, $4, $5, $6, FALSE, FALSE)
        """, token, username.lower(), user.id, invite.invite_link, invite_expires, subscription_ends)

        ends_msk = subscription_ends.replace(tzinfo=pytz.utc).astimezone(MOSCOW_TZ)
        await update.message.reply_text(
            f"Привет! Вот твоя уникальная ссылка для доступа в закрытый телеграм канал.\n"
            f"Нажми на неё, чтобы подписаться.\n"
            f"Срок действия ссылки — 1 час.\n\n"
            f"🔗 Ссылка: {invite.invite_link}\n"
            f"⏳ Подписка закончится: {ends_msk.strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n"
            f"Если есть вопросы — обратись к своему куратору."
        )
        logger.info(f"✅ Выдана ссылка @{username} (ID: {user.id}) до {subscription_ends}")

        # Отправляем данные в Google Sheets
        await send_to_google_sheets(
            user.id,
            username.lower(),
            user.first_name or "",
            now.strftime("%Y-%m-%d %H:%M:%S"),
            subscription_ends.strftime("%Y-%m-%d %H:%M:%S")
        )

# Часть 3: Обработка вступления, оповещения и автокик

async def notify_kurators(context: ContextTypes.DEFAULT_TYPE, message: str):
    for admin_id in ADMINS.keys():
        try:
            await context.bot.send_message(admin_id, message)
            logger.debug(f"Отправлено уведомление куратору {ADMINS[admin_id]} (ID: {admin_id})")
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление куратору {admin_id}: {e}", exc_info=True)

async def handle_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

            if subscription_ends < now:
                msg = (
                    f"⚠️ Пользователь @{username} (ID: {user_id}) вошёл в канал, "
                    f"но подписка истекла {subscription_ends.astimezone(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}.\n"
                    "Проверьте и при необходимости удалите."
                )
                await notify_kurators(context, msg)
                logger.info(f"Пользователь @{username} с истекшей подпиской вошёл в канал.")
                return

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
            if user_id not in ADMINS:
                msg = (
                    f"⚠️ В канал вступил неизвестный пользователь: @{username} (ID: {user_id}).\n"
                    "Его нет в базе. Проверьте самостоятельно."
                )
                await notify_kurators(context, msg)
                logger.info(f"Обнаружен чужак @{username} в канале.")

async def kick_expired_members(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Запускаем проверку истекших подписок")
    now_utc = datetime.datetime.utcnow().replace(tzinfo=pytz.utc)

    async with context.application.bot_data["db"].acquire() as conn:
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

            if 0 < time_left <= 600 and is_in_chat:
                try:
                    await context.bot.send_message(
                        user_id,
                        "⚠️ Завтра истекает срок действия твоей подписки."
                    )
                    logger.info(f"Предупреждение отправлено @{username}")
                except Exception as e:
                    logger.warning(f"Не удалось отправить предупреждение @{username}: {e}", exc_info=True)

            if time_left <= 0 and is_in_chat:
                try:
                    await context.bot.ban_chat_member(CHANNEL_ID, user_id, until_date=int(now_utc.timestamp()) + 30)
                    await conn.execute("UPDATE tokens SET used = FALSE WHERE user_id = $1", user_id)
                    logger.info(f"Пользователь @{username} удалён по окончании подписки")

                    try:
                        await context.bot.send_message(
                            user_id,
                            "Привет! Твоя подписка истекла. Благодарим, что был с нами.\nТвоя АвтоАкадемия :)"
                        )
                    except Exception as e:
                        logger.warning(f"Не удалось отправить сообщение после кика @{username}: {e}", exc_info=True)

                except Exception as e:
                    logger.error(f"Ошибка кика @{username}: {e}", exc_info=True)

        logger.info("Проверка на нелегальных участников")
        try:
            admins = await context.bot.get_chat_administrators(CHANNEL_ID)
            admin_ids = {admin.user.id for admin in admins}
        except Exception as e:
            logger.error(f"Не удалось получить список админов: {e}", exc_info=True)
            return

        EXCEPTION_IDS = set(ADMINS.keys())
        EXCEPTIONS = admin_ids.union(EXCEPTION_IDS)

        allowed_ids = {row["user_id"] for row in await conn.fetch("""
            SELECT user_id FROM tokens
            WHERE used = TRUE AND subscription_ends > $1 AND user_id IS NOT NULL
        """, now_utc)}

        all_known = await conn.fetch("SELECT user_id FROM tokens WHERE user_id IS NOT NULL")
        known_ids = {row["user_id"] for row in all_known}

        for user_id in known_ids:
            if user_id in allowed_ids or user_id in EXCEPTIONS:
                continue

            try:
                member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
                if member.status in ['member', 'restricted']:
                    username = member.user.username or f"ID_{user_id}"
                    logger.info(f"Обнаружен чужак @{username} (ID: {user_id})")

                    msg = (
                        f"⚠️ В канал вступил неизвестный пользователь: @{username} (ID: {user_id}).\n"
                        "Его нет в активных подписках. Проверьте и при необходимости удалите."
                    )
                    await notify_kurators(context, msg)
            except Exception as e:
                logger.warning(f"Не удалось обработать участника ID {user_id}: {e}", exc_info=True)

# Часть 4: Управление ссылками, добавление учеников, статистика

async def sendlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMINS:
        await update.message.reply_text("⛔️ Доступ запрещён.")
        return

    if not context.args:
        await update.message.reply_text("Используй: /sendlink @username")
        return

    username = context.args[0].lstrip("@").lower()

    if username.lower() not in context.application.bot_data.get("approved_usernames", set()):
        await update.message.reply_text("❌ Пользователь не найден в списке учеников.")
        return

    now = datetime.datetime.utcnow()
    async with context.application.bot_data["db"].acquire() as conn:
        existing = await conn.fetchrow("""
            SELECT user_id FROM tokens
            WHERE username = $1 AND used = TRUE AND user_id IS NOT NULL
        """, username)

        if existing:
            try:
                member = await context.bot.get_chat_member(CHANNEL_ID, existing["user_id"])
                if member.status in ["member", "restricted"]:
                    await update.message.reply_text(
                        "⚠️ Ссылка ранее уже была использована. Убедись, что левак покинул канал."
                    )
                    logger.warning(f"Попытка повторной выдачи, но кто-то уже в канале под @{username}")
                    return
            except Exception as e:
                logger.warning(f"Не удалось проверить участника {existing['user_id']}: {e}", exc_info=True)

        old_links = await conn.fetch("SELECT invite_link FROM tokens WHERE username = $1", username)
        for link_rec in old_links:
            link = link_rec["invite_link"]
            if link:
                try:
                    await context.bot.revoke_chat_invite_link(CHANNEL_ID, link)
                    logger.info(f"Старая ссылка @{username} деактивирована.")
                except Exception as e:
                    logger.warning(f"Не удалось деактивировать старую ссылку @{username}: {e}", exc_info=True)

        await conn.execute("DELETE FROM tokens WHERE username = $1", username)

        token = uuid.uuid4().hex[:8]
        invite_expires = now + datetime.timedelta(minutes=30)
        subscription_ends = now + datetime.timedelta(hours=1)

        try:
            invite: ChatInviteLink = await context.bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                expire_date=invite_expires,
                member_limit=1
            )
        except Exception as e:
            logger.error(f"Ошибка создания ссылки для @{username}: {e}", exc_info=True)
            await update.message.reply_text("Ошибка при создании ссылки. Попробуйте позже.")
            return

        await conn.execute("""
            INSERT INTO tokens (token, username, user_id, invite_link, expires, subscription_ends, used)
            VALUES ($1, $2, NULL, $3, $4, $5, FALSE)
        """, token, username, invite.invite_link, invite_expires, subscription_ends)

        ends_msk = subscription_ends.replace(tzinfo=pytz.utc).astimezone(MOSCOW_TZ)
        expires_msk = invite_expires.replace(tzinfo=pytz.utc).astimezone(MOSCOW_TZ)

        await update.message.reply_text(
            f"♻️ Ссылка для @{username} обновлена и сброшены все предыдущие данные.\n"
            f"Срок действия ссылки: {expires_msk.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"Подписка действует до: {ends_msk.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"Попроси ученика ввести /start и использовать ссылку."
        )
        logger.info(f"Выдана новая ссылка @{username} до {subscription_ends}")

async def add_student(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        await update.message.reply_text("⛔️ Доступ запрещён.")
        return

    if len(context.args) < 1:
        await update.message.reply_text("Используй: /addstudent @username")
        return

    username = context.args[0].lstrip("@").lower()

    if username.lower() in context.application.bot_data.get("approved_usernames", set()):
        await update.message.reply_text(f"Пользователь @{username} уже в списке учеников.")
        return

    # Добавление
    async with context.application.bot_data["db"].acquire() as conn:
        await conn.execute("INSERT INTO students (username) VALUES ($1)", username.lower())

    # Обновляем список в bot_data
    context.application.bot_data["approved_usernames"].add(username.lower())

    await update.message.reply_text(
        f"✅ Пользователь @{username} добавлен в список учеников.\nОн сможет получить доступ через /start."
    )
    logger.info(f"Пользователь @{username} добавлен администратором {update.effective_user.id}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMINS:
        await update.message.reply_text("⛔️ Доступ запрещён.")
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

import asyncio

async def main():
    # Создаём приложение бота
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Инициализация пула БД и сохранение в bot_data для доступа из хендлеров
    db_pool = await get_db_pool()
    application.bot_data["db"] = db_pool

    # Загружаем список учеников из базы
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT username FROM students")
        approved = {row["username"].lower() for row in rows}
        application.bot_data["approved_usernames"] = approved
        logger.info(f"✅ Загружено учеников: {len(approved)}")


    # Регистрируем хендлеры команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("sendlink", sendlink))
    application.add_handler(CommandHandler("addstudent", add_student))
    application.add_handler(CommandHandler("stats", stats))

    # Хендлер на изменение статуса чата (вступление/выход)
    application.add_handler(ChatMemberHandler(handle_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    # Планировщик — автокик по расписанию, например, раз в 5 минут
    job_queue: JobQueue = application.job_queue
    job_queue.run_repeating(kick_expired_members, interval=300, first=10)

    logger.info("Бот запущен!")

    # Запуск бота (async)
    await application.run_polling()

    # Закрываем соединение с БД при остановке бота
    await db_pool.close()

if __name__ == "__main__":
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        print("⚠️ Event loop уже работает, запускаем main как задачу")
        loop.create_task(main())
        loop.run_forever()  # держим луп живым
    else:
        print("🚀 Запускаем через asyncio.run()")
        asyncio.run(main())
