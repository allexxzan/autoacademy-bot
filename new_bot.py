import os 
import uuid
import logging
import datetime
import asyncpg
import pytz
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ChatMemberHandler
)

# Загрузка переменных окружения
load_dotenv()

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(','))) if os.getenv("ADMIN_IDS") else []
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class Database:
    """Улучшенный класс для работы с базой данных"""
    def __init__(self):
        self.conn = None

    async def connect(self):
        self.conn = await asyncpg.connect(DATABASE_URL)
        await self._create_tables()
        await self._create_indexes()

    async def _create_tables(self):
        """Создание таблиц в базе данных с улучшенной структурой"""
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS students (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                user_id BIGINT,
                is_active BOOLEAN DEFAULT FALSE,
                join_date TIMESTAMP WITH TIME ZONE,
                expire_date TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)

        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS invite_links (
                id SERIAL PRIMARY KEY,
                token TEXT UNIQUE NOT NULL,
                username TEXT NOT NULL,
                user_id BIGINT,
                link TEXT NOT NULL,
                expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
                uses_count INT DEFAULT 0,
                max_uses INT DEFAULT 1,
                is_active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                FOREIGN KEY (username) REFERENCES students (username)
            )
        """)

        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL,
                user_id BIGINT,
                has_joined BOOLEAN DEFAULT FALSE,
                join_date TIMESTAMP WITH TIME ZONE,
                expire_date TIMESTAMP WITH TIME ZONE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                FOREIGN KEY (username) REFERENCES students (username)
            )
        """)

    async def _create_indexes(self):
        """Создание индексов для ускорения запросов"""
        await self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_students_username ON students (username);
        """)
        await self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_students_active ON students (is_active);
        """)
        await self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_students_expire ON students (expire_date);
        """)
        await self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_invites_active ON invite_links (is_active, expires_at);
        """)

    async def add_student(self, username: str):
        """Добавление нового студента в базу с обработкой ошибок"""
        try:
            await self.conn.execute(
                "INSERT INTO students (username) VALUES ($1) ON CONFLICT (username) DO NOTHING",
                username.lower()
            )
            return True
        except Exception as e:
            logger.error(f"Ошибка при добавлении студента @{username}: {str(e)}", exc_info=True)
            return False

    async def is_student(self, username: str) -> bool:
        """Проверка, является ли пользователь студентом"""
        try:
            return await self.conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM students WHERE username = $1)",
                username.lower()
            )
        except Exception as e:
            logger.error(f"Ошибка при проверке студента @{username}: {str(e)}", exc_info=True)
            return False

    async def get_student(self, username: str):
        """Получение информации о студенте с проверкой подписки"""
        try:
            student = await self.conn.fetchrow(
                "SELECT * FROM students WHERE username = $1",
                username.lower()
            )
            
            # Если подписка истекла, деактивируем студента
            if student and student['is_active'] and student['expire_date'] < datetime.datetime.now(MOSCOW_TZ):
                await self.deactivate_student(username)
                student['is_active'] = False
                
            return student
        except Exception as e:
            logger.error(f"Ошибка при получении данных студента @{username}: {str(e)}", exc_info=True)
            return None

    async def create_invite_link(self, username: str, user_id: int, link: str, expires_at: datetime.datetime):
        """Создание записи о пригласительной ссылке с улучшенной логикой"""
        try:
            token = uuid.uuid4().hex
            await self.conn.execute(
                """
                INSERT INTO invite_links 
                (token, username, user_id, link, expires_at, max_uses)
                VALUES ($1, $2, $3, $4, $5, 1)
                """,
                token, username.lower(), user_id, link, expires_at
            )
            return token
        except Exception as e:
            logger.error(f"Ошибка при создании ссылки для @{username}: {str(e)}", exc_info=True)
            return None

    async def get_active_invite(self, username: str):
        """Получение активной пригласительной ссылки с проверкой использования"""
        try:
            return await self.conn.fetchrow(
                """
                SELECT * FROM invite_links 
                WHERE username = $1 
                AND expires_at > NOW() 
                AND is_active = TRUE
                AND uses_count < max_uses
                ORDER BY created_at DESC
                LIMIT 1
                """,
                username.lower()
            )
        except Exception as e:
            logger.error(f"Ошибка при получении активной ссылки для @{username}: {str(e)}", exc_info=True)
            return None

    async def mark_invite_used(self, token: str):
        """Пометка ссылки как использованной с увеличением счетчика"""
        try:
            await self.conn.execute(
                """
                UPDATE invite_links 
                SET uses_count = uses_count + 1,
                is_active = (uses_count + 1) < max_uses
                WHERE token = $1
                """,
                token
            )
            return True
        except Exception as e:
            logger.error(f"Ошибка при отметке использования ссылки {token}: {str(e)}", exc_info=True)
            return False

    async def activate_student(self, username: str, user_id: int):
        """Активация студента с улучшенной обработкой ошибок"""
        try:
            join_date = datetime.datetime.now(MOSCOW_TZ)
            expire_date = join_date + datetime.timedelta(days=365)
            
            async with self.conn.transaction():
                # Обновляем статус студента
                await self.conn.execute(
                    """
                    UPDATE students 
                    SET is_active = TRUE, 
                        user_id = $1, 
                        join_date = $2, 
                        expire_date = $3
                    WHERE username = $4
                    """,
                    user_id, join_date, expire_date, username.lower()
                )

                # Добавляем запись в статистику
                await self.conn.execute(
                    """
                    INSERT INTO stats 
                    (username, user_id, has_joined, join_date, expire_date)
                    VALUES ($1, $2, TRUE, $3, $4)
                    """,
                    username.lower(), user_id, join_date, expire_date
                )
            
            return True
        except Exception as e:
            logger.error(f"Ошибка при активации студента @{username}: {str(e)}", exc_info=True)
            return False

    async def deactivate_student(self, username: str):
        """Деактивация студента с обработкой ошибок"""
        try:
            await self.conn.execute(
                "UPDATE students SET is_active = FALSE WHERE username = $1",
                username.lower()
            )
            return True
        except Exception as e:
            logger.error(f"Ошибка при деактивации студента @{username}: {str(e)}", exc_info=True)
            return False

    async def can_request_new_link(self, username: str) -> bool:
        """Проверка возможности запросить новую ссылку"""
        try:
            active_invites = await self.conn.fetchval(
                """
                SELECT COUNT(*) FROM invite_links 
                WHERE username = $1 
                AND expires_at > NOW() 
                AND is_active = TRUE
                AND uses_count < max_uses
                """,
                username.lower()
            )
            return active_invites == 0
        except Exception as e:
            logger.error(f"Ошибка при проверке возможности запроса ссылки для @{username}: {str(e)}", exc_info=True)
            return False

    async def get_expired_students(self):
        """Получение студентов с истекшей подпиской"""
        try:
            return await self.conn.fetch(
                """
                SELECT * FROM students 
                WHERE is_active = TRUE 
                AND expire_date < NOW()
                """
            )
        except Exception as e:
            logger.error(f"Ошибка при получении студентов с истекшей подпиской: {str(e)}", exc_info=True)
            return []

    async def get_stats(self):
        """Получение статистики с улучшенным форматированием"""
        try:
            return await self.conn.fetch(
                """
                SELECT 
                    s.username,
                    s.user_id,
                    s.join_date,
                    s.expire_date,
                    COUNT(il.id) FILTER (WHERE il.uses_count > 0) AS successful_joins,
                    COUNT(il.id) FILTER (WHERE il.uses_count = 0 AND il.expires_at < NOW()) AS expired_links
                FROM students s
                LEFT JOIN invite_links il ON s.username = il.username
                GROUP BY s.username, s.user_id, s.join_date, s.expire_date
                ORDER BY s.join_date DESC
                """
            )
        except Exception as e:
            logger.error(f"Ошибка при получении статистики: {str(e)}", exc_info=True)
            return []

    async def reset_invite_links(self, username: str):
        """Сброс всех пригласительных ссылок для пользователя"""
        try:
            await self.conn.execute(
                "UPDATE invite_links SET is_active = FALSE WHERE username = $1",
                username.lower()
            )
            return True
        except Exception as e:
            logger.error(f"Ошибка при сбросе ссылок для @{username}: {str(e)}", exc_info=True)
            return False

    async def close(self):
        """Закрытие соединения с базой данных с обработкой ошибок"""
        try:
            if self.conn:
                await self.conn.close()
        except Exception as e:
            logger.error(f"Ошибка при закрытии соединения с БД: {str(e)}", exc_info=True)

db = Database()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Улучшенная обработка команды /start"""
    user = update.effective_user
    
    if not user.username:
        await update.message.reply_text(
            "❌ Для доступа к каналу у вас должен быть установлен username в настройках Telegram."
        )
        return

    try:
        logger.info(f"Обработка /start для @{user.username} (ID: {user.id})")

        # Проверяем, есть ли пользователь в базе студентов
        is_student = await db.is_student(user.username)
        
        if not is_student:
            # Уведомляем администраторов о попытке доступа левака
            message = (
                f"⚠️ Попытка доступа левака!\n"
                f"Username: @{user.username}\n"
                f"ID: {user.id}\n"
                f"Имя: {user.full_name}\n"
                f"Время: {datetime.datetime.now(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S')}"
            )
            await notify_admins(context, message)
            
            await update.message.reply_text(
                "❌ Этот канал доступен только для студентов АвтоАкадемии."
            )
            return

        # Получаем информацию о студенте (с автоматической проверкой срока подписки)
        student = await db.get_student(user.username)
        
        if not student:
            await update.message.reply_text("⛔ Произошла ошибка. Администратор уведомлен.")
            await notify_admins(
                context, 
                f"Ошибка при получении данных студента @{user.username} (ID: {user.id})"
            )
            return
        
        # Если подписка истекла
        if student['is_active'] and student['expire_date'] < datetime.datetime.now(MOSCOW_TZ):
            await update.message.reply_text(
                "⏳ Ваша подписка на канал истекла. Для продления обратитесь к администратору."
            )
            return
        
        # Если уже подписан
        if student['is_active']:
            await update.message.reply_text(
                f"✅ Вы уже подписаны на канал.\n"
                f"Дата вступления: {student['join_date'].astimezone(MOSCOW_TZ).strftime('%d.%m.%Y %H:%M')}\n"
                f"Подписка действительна до: {student['expire_date'].astimezone(MOSCOW_TZ).strftime('%d.%m.%Y %H:%M')}"
            )
            return

        # Проверяем, есть ли активная ссылка
        active_invite = await db.get_active_invite(user.username)
        
        if active_invite:
            await update.message.reply_text(
                f"🔗 Ваша ссылка для вступления в канал:\n{active_invite['link']}\n"
                f"⏳ Ссылка действительна до: {active_invite['expires_at'].astimezone(MOSCOW_TZ).strftime('%d.%m.%Y %H:%M')}\n"
                f"🔄 Осталось использований: {active_invite['max_uses'] - active_invite['uses_count']}"
            )
            return

        # Создаем новую ссылку
        expires_at = datetime.datetime.now(MOSCOW_TZ) + datetime.timedelta(hours=1)
        invite_link = await context.bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            expire_date=int(expires_at.timestamp()),
            member_limit=1
        )

        # Сохраняем ссылку в базу
        token = await db.create_invite_link(user.username, user.id, invite_link.invite_link, expires_at)
        
        if not token:
            await update.message.reply_text("⛔ Не удалось создать ссылку. Администратор уведомлен.")
            await notify_admins(
                context, 
                f"Ошибка при создании ссылки для @{user.username} (ID: {user.id})"
            )
            return
        
        await update.message.reply_text(
            f"🔗 Ваша ссылка для вступления в канал:\n{invite_link.invite_link}\n"
            f"⏳ Ссылка действительна в течение 1 часа (до {expires_at.strftime('%d.%m.%Y %H:%M')})\n"
            f"🔄 Можно использовать: 1 раз\n\n"
            f"⚠️ После вступления ваша подписка будет активна 1 год."
        )

    except Exception as e:
        logger.error(
            f"Критическая ошибка в обработке /start для @{user.username} (ID: {user.id}): {str(e)}", 
            exc_info=True
        )
        await update.message.reply_text("⛔ Произошла критическая ошибка. Администратор уведомлен.")
        await notify_admins(
            context, 
            f"Критическая ошибка в обработке /start для @{user.username} (ID: {user.id}): {str(e)}"
        )

async def handle_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка обновлений статуса участника канала с улучшенной логикой"""
    try:
        chat_member = update.chat_member
        if chat_member.chat.id != CHANNEL_ID:
            return

        user = chat_member.new_chat_member.user
        if not user.username:
            logger.warning(f"Пользователь без username (ID: {user.id}) пытается вступить в канал")
            return

        logger.info(f"Обработка chat_member_update для @{user.username} (ID: {user.id})")

        # Проверяем, был ли это переход по нашей ссылке
        if (chat_member.old_chat_member.status == 'left' and 
            chat_member.new_chat_member.status == 'member'):
            
            # Активируем студента
            success = await db.activate_student(user.username, user.id)
            
            if not success:
                logger.error(f"Не удалось активировать студента @{user.username} (ID: {user.id})")
                await context.bot.ban_chat_member(
                    chat_id=CHANNEL_ID,
                    user_id=user.id,
                    until_date=int((datetime.datetime.now() + datetime.timedelta(minutes=1)).timestamp())
                )
                return

            # Получаем информацию о студенте
            student = await db.get_student(user.username)
            
            if not student:
                logger.error(f"Не удалось получить данные студента @{user.username} после активации")
                return
            
            # Отправляем сообщение пользователю
            try:
                await context.bot.send_message(
                    chat_id=user.id,
                    text=(
                        f"✅ Вы успешно подписались на канал!\n"
                        f"Дата вступления: {student['join_date'].astimezone(MOSCOW_TZ).strftime('%d.%m.%Y %H:%M')}\n"
                        f"Подписка действительна до: {student['expire_date'].astimezone(MOSCOW_TZ).strftime('%d.%m.%Y %H:%M')}\n\n"
                        f"Через год подписка автоматически прекратится."
                    )
                )
            except Exception as e:
                logger.error(f"Не удалось отправить сообщение пользователю @{user.username} (ID: {user.id}): {str(e)}")

    except Exception as e:
        logger.error(
            f"Ошибка в обработке chat_member_update для пользователя {getattr(user, 'username', 'N/A')} (ID: {getattr(user, 'id', 'N/A')}): {str(e)}", 
            exc_info=True
        )
        await notify_admins(
            context, 
            f"Ошибка в обработке chat_member_update: {str(e)}"
        )

async def check_expired_subscriptions(context: ContextTypes.DEFAULT_TYPE):
    """Проверка и удаление пользователей с истекшей подпиской"""
    try:
        logger.info("Запуск проверки истекших подписок")
        expired_students = await db.get_expired_students()
        
        if not expired_students:
            logger.info("Нет студентов с истекшей подпиской")
            return
        
        logger.info(f"Найдено {len(expired_students)} студентов с истекшей подпиской")
        
        for student in expired_students:
            try:
                logger.info(f"Обработка студента @{student['username']} (ID: {student['user_id']})")
                
                # Исключаем пользователя из канала
                await context.bot.ban_chat_member(
                    chat_id=CHANNEL_ID,
                    user_id=student['user_id'],
                    until_date=int((datetime.datetime.now() + datetime.timedelta(minutes=1)).timestamp())
                )
                
                # Помечаем как неактивного
                success = await db.deactivate_student(student['username'])
                
                if not success:
                    logger.error(f"Не удалось деактивировать студента @{student['username']}")
                    continue
                
                # Уведомляем пользователя
                try:
                    await context.bot.send_message(
                        chat_id=student['user_id'],
                        text=(
                            "⏳ Ваша годовая подписка на канал АвтоАкадемии истекла.\n"
                            "Вы были автоматически отписаны от канала.\n\n"
                            "Если вам нужно продлить подписку, обратитесь к администратору."
                        )
                    )
                except Exception as e:
                    logger.error(f"Не удалось уведомить пользователя @{student['username']}: {str(e)}")
                
                logger.info(f"Пользователь @{student['username']} успешно отписан по истечении срока.")
                
            except Exception as e:
                logger.error(
                    f"Ошибка при обработке студента @{student['username']} (ID: {student['user_id']}): {str(e)}", 
                    exc_info=True
                )
                continue

    except Exception as e:
        logger.error(f"Критическая ошибка в check_expired_subscriptions: {str(e)}", exc_info=True)
        await notify_admins(
            context, 
            f"Критическая ошибка в check_expired_subscriptions: {str(e)}"
        )

async def notify_admins(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Улучшенная функция уведомления администраторов"""
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=message)
        except Exception as e:
            logger.error(f"Не удалось уведомить администратора {admin_id}: {str(e)}")

async def admin_add_student(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда администратора для добавления студента с улучшенной обработкой"""
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет прав для выполнения этой команды.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /addstudent @username")
        return

    username = context.args[0].lstrip('@').lower()
    
    logger.info(f"Админ {user.username} (ID: {user.id}) добавляет студента @{username}")
    
    try:
        success = await db.add_student(username)
        
        if success:
            await update.message.reply_text(f"✅ Пользователь @{username} добавлен в базу студентов.")
            logger.info(f"Пользователь @{username} успешно добавлен")
        else:
            await update.message.reply_text(f"⛔ Не удалось добавить пользователя @{username}.")
            logger.warning(f"Не удалось добавить пользователя @{username}")
            
    except Exception as e:
        await update.message.reply_text(f"⛔ Произошла ошибка при добавлении пользователя.")
        logger.error(f"Ошибка при добавлении студента @{username}: {str(e)}", exc_info=True)

async def admin_reset_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда администратора для сброса ссылки с улучшенной обработкой"""
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет прав для выполнения этой команды.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /resetlink @username")
        return

    username = context.args[0].lstrip('@').lower()
    
    logger.info(f"Админ {user.username} (ID: {user.id}) сбрасывает ссылки для @{username}")
    
    try:
        success = await db.reset_invite_links(username)
        
        if success:
            await update.message.reply_text(f"✅ Ссылки для @{username} сброшены. Пользователь может запросить новую ссылку.")
            logger.info(f"Ссылки для @{username} успешно сброшены")
        else:
            await update.message.reply_text(f"⛔ Не удалось сбросить ссылки для @{username}.")
            logger.warning(f"Не удалось сбросить ссылки для @{username}")
            
    except Exception as e:
        await update.message.reply_text(f"⛔ Произошла ошибка при сбросе ссылок.")
        logger.error(f"Ошибка при сбросе ссылок для @{username}: {str(e)}", exc_info=True)

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда администратора для получения статистики с улучшенным форматированием"""
    user = update.effective_user
    
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ У вас нет прав для выполнения этой команды.")
        return

    logger.info(f"Админ {user.username} (ID: {user.id}) запросил статистику")
    
    try:
        stats = await db.get_stats()
        
        if not stats:
            await update.message.reply_text("📊 Статистика пуста.")
            return

        # Формируем сообщение со статистикой
        message = ["📊 <b>Статистика подписок</b>\n"]
        
        for stat in stats:
            user_info = f"\n👤 <b>@{stat['username']}</b> (ID: {stat['user_id']})"
            
            if stat['join_date']:
                join_date = stat['join_date'].astimezone(MOSCOW_TZ).strftime('%d.%m.%Y %H:%M')
                expire_date = stat['expire_date'].astimezone(MOSCOW_TZ).strftime('%d.%m.%Y %H:%M')
                status = f"✅ Подписан до {expire_date}"
            else:
                join_date = "Не подписан"
                expire_date = "Н/Д"
                status = "❌ Не подписан"
            
            stats_info = (
                f"{user_info}\n"
                f"📅 Статус: {status}\n"
                f"🔗 Успешных вступлений: {stat['successful_joins']}\n"
                f"⌛️ Просроченных ссылок: {stat['expired_links']}\n"
            )
            
            message.append(stats_info)

        # Разбиваем сообщение на части, если оно слишком длинное
        full_message = "\n".join(message)
        for i in range(0, len(full_message), 4096):
            part = full_message[i:i+4096]
            await update.message.reply_text(
                text=part,
                parse_mode="HTML"
            )

    except Exception as e:
        await update.message.reply_text("⛔ Не удалось получить статистику.")
        logger.error(f"Ошибка при получении статистики: {str(e)}", exc_info=True)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка обычных сообщений - игнорируем"""
    pass

async def init_db():
    """Инициализация базы данных с обработкой ошибок"""
    try:
        await db.connect()
        logger.info("База данных успешно подключена")
    except Exception as e:
        logger.error(f"Ошибка при подключении к базе данных: {str(e)}", exc_info=True)
        raise

async def shutdown(application):
    """Завершение работы бота с обработкой ошибок"""
    try:
        await db.close()
        logger.info("База данных успешно отключена")
    except Exception as e:
        logger.error(f"Ошибка при отключении от базы данных: {str(e)}", exc_info=True)

def main():
    """Запуск бота с улучшенной обработкой ошибок"""
    try:
        application = ApplicationBuilder() \
            .token(BOT_TOKEN) \
            .post_init(init_db) \
            .post_shutdown(shutdown) \
            .build()

        # Обработчики команд
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("addstudent", admin_add_student))
        application.add_handler(CommandHandler("resetlink", admin_reset_link))
        application.add_handler(CommandHandler("stats", admin_stats))

        # Обработчик обновлений участников канала
        application.add_handler(ChatMemberHandler(handle_chat_member_update))

        # Обработчик обычных сообщений (игнорирует все)
        application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))

        # Периодическая задача для проверки истекших подписок
        job_queue = application.job_queue
        job_queue.run_repeating(
            check_expired_subscriptions, 
            interval=86400,  # Проверка раз в день
            first=10
        )

        logger.info("Бот запускается...")
        application.run_polling()

    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске бота: {str(e)}", exc_info=True)

if __name__ == "__main__":
    main()
