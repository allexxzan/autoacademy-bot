import traceback  # для показа полного стектрейса ошибок
import logging
import datetime
import asyncio
import os
import re  # импортируем только один раз

from sheets import log_subscription  # логгирование подписки в Google Sheets
from telegram import ReplyKeyboardMarkup
from telegram.ext import MessageHandler, filters  # импортируем только один раз

def to_msk(dt_utc: datetime.datetime) -> datetime.datetime:
    msk_tz = datetime.timezone(datetime.timedelta(hours=3))
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=datetime.timezone.utc)
    return dt_utc.astimezone(msk_tz)

from dotenv import load_dotenv
from telegram import ChatInviteLink, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, ChatMemberHandler
)
from telegram.error import TelegramError

from db import Database  # Импортируем класс базы

load_dotenv()

# Загружаем переменные окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(','))) if os.getenv("ADMIN_IDS") else []
CURATOR_ID = int(os.getenv("CURATOR_ID", "0"))
SUBSCRIPTION_MINUTES = int(os.getenv("SUBSCRIPTION_MINUTES", "525600"))

# Настройка логгера
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Создаём экземпляр базы данных
db = Database()

# Проверка, является ли пользователь админом
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# --- Команда /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["Старт"]]  # кнопка "Старт" снизу
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)

    await update.message.reply_text(
        "Получи доступ к закрытому каналу АвтоАкадемии.\n"
        "Нажми кнопку «Старт» ниже, чтобы получить ссылку и активировать подписку.",
        reply_markup=reply_markup
    )

    user = update.effective_user
    username = user.username.lower() if user.username else None
    user_id = user.id

    logger.info(f"/start от @{username} ({user_id})")

    if not username:
        await update.message.reply_text("⛔ У вас не задан username. Обратитесь к куратору.")
        return

    student = await db.get_student(username)
    logger.info(f"Поиск @{username} в БД: {'Найден' if student else 'НЕ найден'}")

    if not student:
        await context.bot.send_message(CURATOR_ID, f"🚨 Левак: @{username} запустил бота.")
        await update.message.reply_text("⛔ Канал доступен только ученикам АвтоАкадемии.")
        return

    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    valid_until = now + datetime.timedelta(minutes=SUBSCRIPTION_MINUTES)

    logger.info(f"Текущий UTC: {now.isoformat()}")

    if student["valid_until"] and student["valid_until"] <= now:
        await update.message.reply_text("❌ Ваша подписка уже закончилась. Для повторного доступа — только через куратора.")
        return

    if student["invite_sent_at"]:
        await update.message.reply_text("📬 Ссылка уже была выдана. Обратитесь к куратору для новой.")
        return

import re  # для флага игнорирования регистра

async def on_start_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = user.username.lower() if user.username else None
    user_id = user.id

    logger.info(f"Кнопка Старт нажата @{username} ({user_id})")

    if not username:
        await update.message.reply_text("⛔ У вас не задан username. Обратитесь к куратору.")
        return

    student = await db.get_student(username)
    logger.info(f"Поиск @{username} в БД: {'Найден' if student else 'НЕ найден'}")

    if not student:
        await context.bot.send_message(CURATOR_ID, f"🚨 Левак: @{username} нажал кнопку Старт.")
        await update.message.reply_text("⛔ Канал доступен только ученикам АвтоАкадемии.")
        return

    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)

    if student["valid_until"] and student["valid_until"] <= now:
        await update.message.reply_text("❌ Ваша подписка уже закончилась. Для повторного доступа — только через куратора.")
        return

    if student["invite_sent_at"]:
        await update.message.reply_text("📬 Ссылка уже была выдана. Обратитесь к куратору для новой.")
        return

    expire = now + datetime.timedelta(hours=1)
    try:
        invite_link_obj = await context.bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            name=f"Ссылка для @{username}",
            member_limit=1,
            expire_date=expire,
            creates_join_request=False
        )
        invite_link = invite_link_obj.invite_link
    except Exception as e:
        logger.error(f"Ошибка при генерации ссылки для @{username}: {e}")
        await update.message.reply_text("⚠️ Не удалось сгенерировать ссылку. Попробуйте позже.")
        return

    logger.info(f"Выдана ссылка для @{username}: {invite_link}")

    await db.record_invite_sent(username, invite_link, now)

    await update.message.reply_text(
        f"🔗 Вот ваша уникальная ссылка для входа в канал:\n{invite_link}\n\n"
        f"❗️ Ссылка одноразовая, срок действия 1 час.\n"
        f"⚠️ Подписка активируется после перехода по ссылке и присоединения к каналу."
    )

# --- Генерация уникальной ссылки ---
async def generate_invite_link(bot, username: str) -> str | None:
    try:
        now = datetime.datetime.utcnow()
        logger.info(f"Генерация ссылки для @{username}...")

        expire = now + datetime.timedelta(minutes=10)
        invite_link: ChatInviteLink = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            name=f"Ссылка для @{username}",
            member_limit=1,
            expire_date=expire,
            creates_join_request=False
        )

        logger.info(f"Ссылка для @{username} создана: {invite_link.invite_link}")
        logger.info(f"Срок действия до: {invite_link.expire_date}")
        logger.info(f"invite.full: {invite_link.to_dict()}")

        return invite_link.invite_link

    except TelegramError as e:
        logger.error(f"[TG ERROR] Ошибка Telegram при генерации для @{username}: {e}")
        logger.error(traceback.format_exc())
    except Exception as e:
        logger.error(f"[PYTHON ERROR] Ошибка генерации ссылки для @{username}: {e}")
        logger.error(traceback.format_exc())

    return None

# --- Автоудаление по подписке ---
async def kick_expired_subscriptions(context: ContextTypes.DEFAULT_TYPE):
    logger.info("🧹 Проверка на кик просроченных...")

    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    logger.info(f"🔍 Текущее UTC время: {now.isoformat()}")

    expired_students = await db.get_expired_students(now)

    logger.info(f"👀 Найдено студентов с истёкшей подпиской: {len(expired_students)}")
    logger.info(f"🧾 Кандидаты на кик: {[s['username'] for s in expired_students]}")

    for student in expired_students:
        username = student["username"]
        user_id = student["user_id"]

        if not user_id:
            logger.warning(f"⚠️ Пропущен @{username} — нет user_id")
            continue

        try:
            await context.bot.ban_chat_member(CHANNEL_ID, user_id, until_date=now + datetime.timedelta(seconds=60))
            await context.bot.unban_chat_member(CHANNEL_ID, user_id)

            await db.mark_kicked(username, now)
            logger.info(f"✅ Кикнут @{username}")

            try:
                await context.bot.send_message(user_id, "⏳ Ваша подписка завершена. Доступ к каналу закрыт.")
            except Exception:
                logger.warning(f"❗ Не удалось отправить сообщение @{username} после кика")

        except Exception as e:
            logger.error(f"💥 Ошибка при удалении @{username}: {e}")

async def remind_expiring_subscriptions(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    logger.info("📢 Проверка на напоминания...")

    students = await db.get_students_near_expiry(now + datetime.timedelta(days=3))

    logger.info(f"🔔 Напоминаний к отправке: {len(students)}")

    for student in students:
        username = student["username"]
        user_id = student["user_id"]
        full_name = student["full_name"]
        valid_until = student["valid_until"]

        if not user_id:
            logger.warning(f"❌ @{username} без user_id — не отправляем напоминание")
            continue

        try:
            await context.bot.send_message(
                user_id,
                f"⏰ Привет, {full_name}!\n"
                f"Через 3 дня заканчивается твоя подписка на канал.\n"
                f"Если хочешь остаться — свяжись с куратором."
            )
            await db.mark_reminded(username)
            logger.info(f"✅ Напоминание отправлено @{username}")
        except Exception as e:
            logger.error(f"❗ Ошибка при отправке напоминания @{username}: {e}")

# --- Обработчик новых участников канала ---
async def check_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_member = update.chat_member
    new_user = chat_member.new_chat_member.user
    username = new_user.username.lower() if new_user.username else None

    # 👉 Фикс от ложных срабатываний
    if chat_member.new_chat_member.status != "member":
        logger.info(f"👻 @{username or new_user.id} не вступил — статус: {chat_member.new_chat_member.status}")
        return

    logger.debug(
        f"[chat_member] @{username or new_user.id} status change: {chat_member.old_chat_member.status} → {chat_member.new_chat_member.status}"
    )

    if not username:
        await context.bot.send_message(
            CURATOR_ID,
            f"🚨 В канал зашел пользователь без username: {new_user.id} ({new_user.first_name} {new_user.last_name or ''})"
        )
        return

    student = await db.get_student(username)
    if not student:
        await context.bot.send_message(
            CURATOR_ID,
            f"🚨 Левак @{username} зашел в канал! user_id={new_user.id}"
        )
        # Можешь сразу кикать, если хочешь:
        # await context.bot.ban_chat_member(update.chat_member.chat.id, new_user.id)
        # await context.bot.unban_chat_member(update.chat_member.chat.id, new_user.id)
        return

    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    valid_until = now + datetime.timedelta(minutes=SUBSCRIPTION_MINUTES)

    await db.activate_subscription(username, now, valid_until)
    await db.save_user_id(username, new_user.id)

    await context.bot.send_message(
        new_user.id,
        f"✅ Вы присоединились к каналу. Подписка активирована на 365 дней."
    )

    try:
        log_subscription(username, student["full_name"], now, valid_until)
    except Exception as e:
        logger.error(f"Не удалось залогировать подписку @{username} в Google Sheets: {e}")

    logger.info(f"Подписка для @{username} активирована при вступлении в канал до {to_msk(valid_until).isoformat()}")

# --- Админ-команды ---
async def add_student(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ Нет доступа")

    if len(context.args) < 2:
        return await update.message.reply_text("Использование: /addstudent @username ФИО")

    username = context.args[0].lstrip("@").lower()
    full_name = " ".join(context.args[1:])
    await db.add_student(username, full_name)
    await update.message.reply_text(f"✅ @{username} добавлен в базу.")


async def deletestudent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ Нет доступа")

    if not context.args:
        return await update.message.reply_text("Использование: /deletestudent @username")

    username = context.args[0].lstrip("@").lower()
    await db.delete_student(username)
    await update.message.reply_text(f"🗑️ @{username} удалён.")

async def reset_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ Нет доступа")

    if not context.args:
        return await update.message.reply_text("Использование: /resetlink @username")

    username = context.args[0].lstrip("@").lower()
    await db.reset_link(username)
    await update.message.reply_text(f"♻️ Ссылка для @{username} сброшена.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    total, active, expired = await db.get_stats()
    await update.message.reply_text(
        f"📊 Статистика:\n"
        f"👥 Всего: {total}\n"
        f"✅ Активных: {active}\n"
        f"⌛ Просроченных: {expired}"
    )

# --- Удаление тех, кто не из базы ---
async def kickuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ Нет доступа")

    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Использование: /kickuser <user_id>")

    user_id = int(context.args[0])

    try:
        await context.bot.ban_chat_member(CHANNEL_ID, user_id)
        await context.bot.unban_chat_member(CHANNEL_ID, user_id)
        await db.delete_student_by_id(user_id)
        await update.message.reply_text(f"✅ Пользователь с user_id={user_id} кикнут и удалён из базы.")
    except Exception as e:
        logger.error(f"Ошибка при кике user_id={user_id}: {e}")
        await update.message.reply_text(f"❌ Не удалось кикнуть пользователя с user_id={user_id}. Ошибка: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    await update.message.reply_text(
        "🛠 Команды администратора:\n"
        "/addstudent @username ФИО — добавить студента\n"
        "/resetlink @username — сбросить ссылку\n"
        "/deletestudent @username — удалить\n"
        "/kickexpired — кикнуть истекших\n"
        "/stats — статистика\n"
        "/help — помощь"
    )

async def kickexpired(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ Нет доступа")

    await kick_expired_subscriptions(context)
    await update.message.reply_text("✅ Просроченные удалены.")


# --- Тестовая команда для отладки автокика ---
async def testkick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("⛔ Нет доступа")

    if not context.args:
        return await update.message.reply_text("Использование: /testkick @username")

    username = context.args[0].lstrip("@").lower()
    now = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)

    # Просрочим подписку на 10 минут назад
    expired_at = now - datetime.timedelta(minutes=10)

    # Обновим подписку так, чтобы она была просрочена
    await db.activate_subscription(username, expired_at - datetime.timedelta(minutes=5), expired_at)

    # НЕ вызываем set_kick_time — просто не меняем kicked_at,
    # чтобы бот мог кикнуть пользователя при следующем запуске автокика

    await update.message.reply_text(f"🔄 @{username} теперь считается просроченным. Ждём автокика или запускай /kickexpired.")

# --- Молчанка для левых сообщений ---
async def silent_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass

# --- Запуск бота ---
async def main():
    await db.connect()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # --- Основные команды ---
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addstudent", add_student))
    app.add_handler(CommandHandler("deletestudent", deletestudent))
    app.add_handler(CommandHandler("resetlink", reset_link))
    app.add_handler(CommandHandler("kickexpired", kickexpired))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("kickuser", kickuser))

    # --- Обработчик кнопки "Старт" с игнорированием регистра ---
    app.add_handler(MessageHandler(filters.Regex("^(?i)старт$"), on_start_button))

    # --- Тестовая команда ---
    app.add_handler(CommandHandler("testkick", testkick))  # ✅ Вот она

    # --- Хендлер на вступление в канал ---
    app.add_handler(ChatMemberHandler(check_new_member, ChatMemberHandler.CHAT_MEMBER))

    # --- Молчанка на все остальные сообщения ---
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, silent_handler))

    # --- Планировщик автокика ---
    app.job_queue.run_repeating(kick_expired_subscriptions, interval=86400, first=20)  # Каждые 24 часа

    # --- Оповещение за 3 дня до истечения подписки ---
    app.job_queue.run_repeating(remind_expiring_subscriptions, interval=86400, first=20) 

    logger.info("✅ Бот запущен")
    await app.run_polling()

if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()

    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
