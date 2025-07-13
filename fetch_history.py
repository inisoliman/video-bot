# ==============================================================================
# ملف: fetch_history.py
# الوصف: هذا الملف يستخدم لمرة واحدة فقط لجلب كل الفيديوهات القديمة
# من القناة وحفظها في قاعدة البيانات.
# ==============================================================================

import telebot
import psycopg2
import os
import time
from urllib.parse import urlparse

# --- الإعدادات الأساسية (قراءة آمنة من متغيرات البيئة) ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
bot = telebot.TeleBot(BOT_TOKEN)

DATABASE_URL = os.getenv('DATABASE_URL')
DB_CONFIG = {}
if DATABASE_URL:
    url = urlparse(DATABASE_URL)
    DB_CONFIG = {
        'dbname': url.path[1:],
        'user': url.username,
        'password': url.password,
        'host': url.hostname,
        'port': url.port
    }

CHANNEL_ID = os.getenv('CHANNEL_ID')

# --- دوال قاعدة البيانات (مكررة ليعمل الملف بشكل مستقل) ---
def init_db_fetch():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        # تم تغيير اسم الجدول إلى video_archive لإصلاح خطأ حجم البيانات
        c.execute('''
            CREATE TABLE IF NOT EXISTS video_archive (
                id SERIAL PRIMARY KEY,
                message_id INTEGER UNIQUE,
                caption TEXT,
                chat_id BIGINT,
                file_name TEXT,
                category TEXT DEFAULT 'Uncategorized'
            )
        ''')
        conn.commit()
        conn.close()
        print("Database for fetch script initialized successfully.")
    except Exception as e:
        print(f"Database error during fetch init: {e}")

def add_video_fetch(message_id, caption, chat_id, file_name=None, category='Uncategorized'):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("""
            INSERT INTO video_archive (message_id, caption, chat_id, file_name, category) 
            VALUES (%s, %s, %s, %s, %s) 
            ON CONFLICT (message_id) DO NOTHING
        """, (message_id, caption or "No caption", chat_id, file_name or "", category))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Add video error during fetch: {e}")

# --- دالة جلب الرسائل القديمة ---
def fetch_all_videos():
    """جلب كل الفيديوهات من القناة."""
    print("Starting to fetch all video messages from the channel. This may take a while...")
    offset = 0
    videos_fetched = 0
    while True:
        try:
            updates = bot.get_updates(offset=offset, limit=100, timeout=30)
            if not updates:
                print("No more messages to fetch.")
                break

            for update in updates:
                offset = update.update_id + 1
                message = update.message or update.channel_post
                
                if message and str(message.chat.id) == CHANNEL_ID and message.video:
                    add_video_fetch(
                        message_id=message.message_id,
                        caption=message.caption,
                        chat_id=message.chat.id,
                        file_name=message.video.file_name if message.video else ""
                    )
                    videos_fetched += 1
                    print(f"Fetched video #{videos_fetched} | Message ID: {message.message_id}")

            time.sleep(2)

        except Exception as e:
            print(f"An error occurred while fetching messages: {e}")
            print("Waiting for 30 seconds before retrying...")
            time.sleep(30)
    
    print(f"\nFetching complete! Total videos saved: {videos_fetched}")

# --- نقطة انطلاق السكربت ---
if __name__ == "__main__":
    if not all([BOT_TOKEN, DATABASE_URL, CHANNEL_ID]):
        print("Error: Missing one or more environment variables (BOT_TOKEN, DATABASE_URL, CHANNEL_ID).")
    else:
        init_db_fetch()
        fetch_all_videos()
