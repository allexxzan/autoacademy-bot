#!/bin/bash
echo "📦 Миграция базы (init_db.py)"
python init_db.py

echo "🚀 Запуск Telegram-бота"
python new_bot.py