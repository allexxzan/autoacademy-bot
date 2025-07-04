import os
import uuid
import logging
import datetime
import asyncpg
import pytz
import json  # Добавляем импорт json
from dotenv import load_dotenv
from telegram import Update, ChatInviteLink
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, ChatMemberHandler
from telegram.error import TelegramError

# Загружаем переменные окружения
load_dotenv()

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1002673430364"))
ADMINS = os.getenv("ADMINS")  # Список админов в виде JSON
ADMINS = json.loads(ADMINS) if ADMINS else {}  # Преобразуем строку в словарь
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Ошибки
class ErrorMessages:
    NO_USERNAME = "❌ Для доступа нужен username в настройках Telegram!"
    DB_ERROR = "🔧 Ошибка базы данных. Попробуйте позже."
    LINK_ERROR = "❌ Ошибка создания ссылки. Попробуйте позже."
    PERMISSION_DENIED = "⛔ У вас нет прав для этой команды"
    INTERNAL_ERROR = "💥 Внутренняя ошибка. Администратор уведомлен."

# Функция отправки уведомлений администраторам
async def notify_admins(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Отправка уведомлений администраторам"""
    for admin_id in ADMINS:
        try:
            await context.bot.send_message(admin_id, message)
        except TelegramError as e:
            logger.warning(f"Не удалось уведомить администратора {admin_id}: {e}")

# Функция для создания и отправки ссылки
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка команды /start для получения ссылки"""
    user = update.effective_user
    if not user.username:
        await update.message.reply_text(ErrorMessages.NO_USERNAME)
        return

    logger.info(f"Обработка /start для @{user.username}")

    # Создание ссылки
    now = datetime.datetime.utcnow().replace(tzinfo=pytz.utc)
    invite_expires = now + datetime.timedelta(minutes=30)
    subscription_ends = now + datetime.timedelta(hours=1)
    kick_at = now + datetime.timedelta(hours=1)  # Пример времени кика после 1 часа

    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            expire_date=int(invite_expires.timestamp()),  # Преобразуем в timestamp
            member_limit=1  # Лимит 1 участник для ссылки
        )
    except TelegramError as e:
        logger.error(f"Ошибка создания ссылки для @{user.username}: {e}")
        await update.message.reply_text(ErrorMessages.LINK_ERROR)
        return

    # Сохранение в базе данных
    try:
        async with await asyncpg.connect(DATABASE_URL) as conn:
            await conn.execute("""
                INSERT INTO tokens (token, username, invite_link, expires, subscription_ends, kick_at, used)
                VALUES ($1, $2, $3, $4, $5, $6, FALSE)
            """, uuid.uuid4().hex[:8], user.username.lower(), invite.invite_link, invite_expires, subscription_ends, kick_at)
            logger.info(f"Новый токен создан для @{user.username}")
    except asyncpg.PostgresError as e:
        logger.error(f"Ошибка БД при сохранении данных для @{user.username}: {e}")
        await update.message.reply_text(ErrorMessages.DB_ERROR)
        await notify_admins(context, f"Ошибка БД при создании токена для @{user.username}: {e}")
        return

    # Отправка пользователю ссылки
    await update.message.reply_text(
        f"🔗 Ваша ссылка: {invite.invite_link}\n"
        f"⏳ Действует 30 минут\n"
        f"📅 Подписка активна до: {subscription_ends.astimezone(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )

# Функция для автокика пользователей с истекшей подпиской
async def kick_expired_members(context: ContextTypes.DEFAULT_TYPE):
    """Автоматический кик пользователей с истекшей подпиской"""
    logger.info("Проверка истекших подписок и времени для автокика...")

    try:
        async with await asyncpg.connect(DATABASE_URL) as conn:
            # Получаем всех пользователей, у которых истекло время для кика
            expired_users = await conn.fetch("""
                SELECT username, kick_at
                FROM tokens
                WHERE kick_at <= NOW() AND used = TRUE
            """)
            for user in expired_users:
                try:
                    user_id = await get_user_id_by_username(context, user['username'])
                    if user_id:
                        # Кикаем пользователя
                        await context.bot.ban_chat_member(
                            chat_id=CHANNEL_ID,
                            user_id=user_id,
                            until_date=int((datetime.datetime.utcnow() + datetime.timedelta(seconds=30)).timestamp())
                        )
                        logger.info(f"Пользователь @{user['username']} кикнут по истечении времени.")
                except TelegramError as e:
                    logger.error(f"Ошибка кика для пользователя @{user['username']}: {e}")
    except Exception as e:
        logger.critical(f"Ошибка в функции автокика: {e}", exc_info=True)
        await notify_admins(context, f"Ошибка автокика: {e}")

# Функция получения user_id по username
async def get_user_id_by_username(context: ContextTypes.DEFAULT_TYPE, username: str):
    """Получение user_id по username"""
    try:
        user = await context.bot.get_chat_member(CHANNEL_ID, username)
        return user.user.id
    except TelegramError as e:
        logger.warning(f"Не удалось получить user_id для @{username}: {e}")
        return None

# Главная точка входа
async def main():
    """Запуск бота"""
    try:
        # Проверка подключения к БД
        try:
            conn = await asyncpg.connect(DATABASE_URL)
            await conn.close()
        except Exception as e:
            logger.critical(f"Не удалось подключиться к БД: {e}")
            raise

        # Создание таблиц если их нет
        async with await asyncpg.connect(DATABASE_URL) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS tokens (
                    token TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    invite_link TEXT NOT NULL,
                    expires TIMESTAMP NOT NULL,
                    subscription_ends TIMESTAMP NOT NULL,
                    kick_at TIMESTAMP,
                    used BOOLEAN DEFAULT FALSE
                )
            """)
            logger.info("Таблица tokens готова!")

        # Настройка бота
        application = ApplicationBuilder().token(BOT_TOKEN).build()

        # Регистрация обработчиков команд
        application.add_handler(CommandHandler("start", start))

        # Настройка задач
        application.job_queue.run_repeating(kick_expired_members, interval=300, first=10)

        # Запуск
        logger.info("Запуск бота...")
        await application.run_polling()

    except Exception as e:
        logger.critical(f"Фатальная ошибка при запуске бота: {e}", exc_info=True)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
