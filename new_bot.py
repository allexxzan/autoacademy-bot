import os
import logging
import asyncio
import datetime
from dotenv import load_dotenv
from telegram import Update, ChatInviteLink
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, ChatMemberHandler
from db import Database  # твой класс из db.py

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(','))) if os.getenv("ADMIN_IDS") else []

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

logger.info(f"Администраторы загружены: {ADMIN_IDS}")

db = Database()

# Помощь по правам
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# Старт
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Бот работает.")

# --- Команды админа ---

async def add_student(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ У вас нет прав.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /addstudent @username")
        return

    username = context.args[0].lstrip("@").lower()
    logger.info(f"Добавляем студента @{username} по команде от {user.username}")
    await db.add_student(username)
    await update.message.reply_text(f"✅ Студент @{username} добавлен.")

async def delete_student(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ У вас нет прав.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /deletestudent @username")
        return

    username = context.args[0].lstrip("@").lower()
    logger.info(f"Удаляем студента @{username} по команде от {user.username}")
    await db.delete_student(username)
    await update.message.reply_text(f"✅ Студент @{username} удалён.")

async def reset_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ У вас нет прав.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /resetlink @username")
        return

    username = context.args[0].lstrip("@").lower()
    # Здесь сбрасываем invite_* поля (логика в db.py, например)
    logger.info(f"Сбрасываем ссылку для @{username} по команде от {user.username}")

    # Обнулим invite_link, invite_created_at, invite_sent_at в БД
    await db.update_invite_link(username, None)  # Передаём None чтобы сбросить
    await update.message.reply_text(f"✅ Ссылка сброшена для @{username}.")

async def kick_expired(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("⛔ У вас нет прав.")
        return

    logger.info(f"Запущен ручной кик просроченных студентов по команде от {user.username}")
    await kick_expired_subscriptions(context)
    await update.message.reply_text("✅ Проверка завершена, просроченные студенты удалены.")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return

    text = (
        "🛠 <b>Доступные команды:</b>\n"
        "/addstudent @username — добавить студента\n"
        "/resetlink @username — сбросить ссылку\n"
        "/deletestudent @username — удалить студента\n"
        "/kickexpired — вручную кикнуть всех с истёкшей подпиской\n"
        "/stats — показать статистику\n"
        "/help — показать это сообщение"
    )
    await update.message.reply_text(text, parse_mode="HTML")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        return

    # Считаем студентов, активных и просроченных
    all_students = await db.get_all_students()
    now = datetime.datetime.utcnow()
    active = [s for s in all_students if s['valid_until'] and s['valid_until'] > now]
    expired = [s for s in all_students if s['valid_until'] and s['valid_until'] <= now]

    text = (
        f"📊 Статистика:\n"
        f"Всего студентов: {len(all_students)}\n"
        f"Активных: {len(active)}\n"
        f"Просроченных: {len(expired)}"
    )
    await update.message.reply_text(text)

# --- Автокик (задача в фоне) ---
async def kick_expired_subscriptions(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Автоудаление подписчиков с истёкшим сроком...")

    expired_students = await db.get_expired_students()
    bot = context.bot

    for student in expired_students:
        username = student['username']
        invite_link = student['invite_link']
        try:
            # Удаляем из канала
            logger.info(f"Кикаем @{username} из канала")
            await bot.ban_chat_member(CHANNEL_ID, student['telegram_id'], until_date=datetime.datetime.utcnow()+datetime.timedelta(seconds=60))
            await bot.unban_chat_member(CHANNEL_ID, student['telegram_id'])
            # Обновляем время кика
            await db.update_kick_time(username)
        except Exception as e:
            logger.error(f"Ошибка при кике @{username}: {e}")

# --- Генерация новой ссылки ---
async def generate_invite_link(username: str, bot):
    logger.info(f"Генерируем инвайт-ссылку для @{username}")
    try:
        # Создаём invite ссылку с ограничением по времени или кол-ву
        invite_link_obj = await bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            expire_date=datetime.datetime.utcnow() + datetime.timedelta(days=365),
            member_limit=1,
            name=f"Invite for {username}"
        )
        invite_link = invite_link_obj.invite_link
        await db.update_invite_link(username, invite_link)
        return invite_link
    except Exception as e:
        logger.error(f"Ошибка генерации ссылки для @{username}: {e}")
        return None

# --- Обработка запроса ссылки от студента ---
async def request_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.effective_user.username
    if not username:
        await update.message.reply_text("Нельзя получить ссылку, если нет username в Telegram.")
        return

    username = username.lower()
    student = await db.get_student(username)
    if not student:
        await update.message.reply_text("Вы не зарегистрированы как студент. Обратитесь к администратору.")
        return

    # Проверяем, можно ли выдавать ссылку (логика из базы)
    # Здесь примерно: если invite_sent_at пустой или прошло достаточно времени
    can_send = True  # Пока в заглушке, потом из базы
    if not can_send:
        await update.message.reply_text("Вы уже получили ссылку, повторно запросить можно позже.")
        return

    invite_link = await generate_invite_link(username, context.bot)
    if invite_link:
        await update.message.reply_text(f"Ваша ссылка для вступления: {invite_link}")
    else:
        await update.message.reply_text("Ошибка при создании ссылки, попробуйте позже.")

# --- Основной запуск ---
async def main():
    await db.connect()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addstudent", add_student))
    app.add_handler(CommandHandler("deletestudent", delete_student))
    app.add_handler(CommandHandler("resetlink", reset_link))
    app.add_handler(CommandHandler("kickexpired", kick_expired))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("getlink", request_link))

    job_queue = app.job_queue
    job_queue.run_repeating(kick_expired_subscriptions, interval=3600, first=10)

    logger.info("Бот запущен и готов к работе.")
    await app.run_polling()  # ВАЖНО: это async функция!

# Вместо asyncio.run(main()) — вот так:
if __name__ == "__main__":
    import nest_asyncio
    import asyncio

    nest_asyncio.apply()  # 💡 фикс для "loop already running" и других ошибок с event loop

    asyncio.run(main())
