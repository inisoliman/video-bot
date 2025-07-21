# ==============================================================================
# ملف: bot.py (النسخة النهائية المصححة مع ترحيل قوي ومعالجة أخطاء محسنة)
# الوصف: النسخة المطورة من البوت مع التصنيفات الشجرية واختيار الجودة والبث الغني
#        مع تصحيحات شاملة لهيكل قاعدة البيانات وعملية الترحيل والتقييمات.
# ==============================================================================

import telebot
import os
import time

#from db_manager import init_db, migrate_old_data
from handlers import register_handlers

# --- الإعدادات الأساسية (قراءة آمنة من متغيرات البيئة) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id] # معرفات حسابات الآدمن

if not all([BOT_TOKEN, DATABASE_URL, CHANNEL_ID, ADMIN_IDS]):
    print("FATAL ERROR: Missing one or more environment variables (BOT_TOKEN, DATABASE_URL, CHANNEL_ID, ADMIN_IDS).")
    exit()

bot = telebot.TeleBot(BOT_TOKEN)

# --- نقطة انطلاق البوت ---

if __name__ == "__main__":
    init_db()
    migrate_old_data()  # ترحيل البيانات القديمة إذا وجدت
    register_handlers(bot, CHANNEL_ID, ADMIN_IDS) # تسجيل المعالجات من ملف handlers.py
    print("Enhanced bot with robust migration is starting...")
    while True:
        try:
            bot.polling(non_stop=True)
        except Exception as e:
            print(f"An error occurred in the main loop: {e}")
            print("Restarting in 15 seconds...")
            time.sleep(15)


