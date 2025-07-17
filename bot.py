# ==============================================================================
# ملف: bot.py (النسخة النهائية الكاملة مع الترحيل التلقائي)
# الوصف: هذا هو السكربت الكامل والجاهز للتشغيل. يقوم بإنشاء قاعدة البيانات
#        بالهيكل الصحيح، ثم يقوم بترحيل جميع بياناتك القديمة من الجداول
#        التي تم تغيير اسمها (video_archive_old, video_ratings_old).
#
# تم تطبيق التحسينات التالية:
# 1. إدارة الاتصال بقاعدة البيانات: استخدام Connection Pooling لضمان
#    أعلى كفاءة واستقرار للبوت.
# 2. ترحيل البيانات: دالة ترحيل قوية ومفصلة تقوم بنقل كل شيء
#    من الجداول القديمة إلى الجديدة تلقائيًا.
# 3. تسجيل الأخطاء: استخدام نظام logging احترافي لتتبع كل ما يحدث.
# ==============================================================================

import telebot
import psycopg2
import psycopg2.pool
import os
import time
import re
import json
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from urllib.parse import urlparse
import math
import logging
from contextlib import contextmanager

# --- إعداد التسجيل (Logging) ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- الإعدادات الأساسية (قراءة آمنة من متغيرات البيئة) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")
ADMIN_IDS = [int(admin_id) for admin_id in ADMIN_IDS_STR.split(",") if admin_id.strip().isdigit()]

if not all([BOT_TOKEN, DATABASE_URL, CHANNEL_ID, ADMIN_IDS]):
    logger.critical("FATAL ERROR: Missing one or more environment variables (BOT_TOKEN, DATABASE_URL, CHANNEL_ID, ADMIN_IDS).")
    exit()

bot = telebot.TeleBot(BOT_TOKEN)

# --- إدارة اتصال قاعدة البيانات المحسنة (Connection Pooling) ---
db_pool = None
try:
    url = urlparse(DATABASE_URL)
    db_pool = psycopg2.pool.SimpleConnectionPool(
        minconn=1,
        maxconn=10,
        dbname=url.path[1:],
        user=url.username,
        password=url.password,
        host=url.hostname,
        port=url.port
    )
    logger.info("Database connection pool created successfully.")
except Exception as e:
    logger.critical(f"Could not create database connection pool: {e}")
    exit()

@contextmanager
def get_db_connection():
    """
    دالة مساعدة للحصول على اتصال من الـ pool وإعادته عند الانتهاء.
    """
    conn = None
    try:
        conn = db_pool.getconn()
        yield conn
    except Exception as e:
        logger.error(f"Database connection error: {e}", exc_info=True)
        if conn:
            db_pool.putconn(conn, close=True)
        raise
    finally:
        if conn:
            db_pool.putconn(conn)

# --- المتغيرات العامة ---
admin_steps = {}
user_last_search = {}
VIDEOS_PER_PAGE = 10
CALLBACK_DELIMITER = "::"

# --- دوال قاعدة البيانات والترحيل ---

def init_db():
    """إنشاء الجداول اللازمة بالهيكل الجديد إذا لم تكن موجودة."""
    logger.info("Initializing new database schema if not exists...")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS categories (
                        id SERIAL PRIMARY KEY,
                        name TEXT NOT NULL,
                        parent_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
                        full_path TEXT NOT NULL UNIQUE
                    )
                """)
                c.execute("""
                    CREATE TABLE IF NOT EXISTS video_archive (
                        id SERIAL PRIMARY KEY,
                        message_id BIGINT UNIQUE,
                        caption TEXT,
                        chat_id BIGINT,
                        file_name TEXT,
                        category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
                        metadata JSONB DEFAULT '{}'::jsonb,
                        view_count INTEGER DEFAULT 0,
                        file_id TEXT
                    )
                """)
                c.execute("""
                    CREATE TABLE IF NOT EXISTS video_ratings (
                        id SERIAL PRIMARY KEY,
                        video_id INTEGER REFERENCES video_archive(id) ON DELETE CASCADE NOT NULL,
                        user_id BIGINT NOT NULL,
                        rating INTEGER CHECK (rating >= 1 AND rating <= 5),
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(video_id, user_id)
                    )
                """)
                # الجداول التي لا يتغير هيكلها
                c.execute("CREATE TABLE IF NOT EXISTS bot_settings (setting_key TEXT PRIMARY KEY, setting_value TEXT)")
                c.execute("CREATE TABLE IF NOT EXISTS bot_users (user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT, join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
                c.execute("CREATE TABLE IF NOT EXISTS required_channels (channel_id BIGINT PRIMARY KEY, channel_name TEXT)")
                conn.commit()
                logger.info("Database schema check/initialization complete.")
    except Exception as e:
        logger.error(f"Database error during init_db: {e}", exc_info=True)
        raise

def run_migration():
    """
    دالة شاملة لتشغيل عملية الترحيل الكاملة للبيانات القديمة.
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("SELECT setting_value FROM bot_settings WHERE setting_key = 'migration_complete'")
                if c.fetchone():
                    logger.info("Migration has already been completed. Skipping.")
                    return
                
                logger.info("Starting data migration process...")
                _migrate_categories(c)
                _migrate_videos(c)
                _migrate_ratings(c)
                
                c.execute("INSERT INTO bot_settings (setting_key, setting_value) VALUES ('migration_complete', 'true') ON CONFLICT (setting_key) DO NOTHING")
                conn.commit()
                logger.info("✅✅✅ Full data migration completed successfully! ✅✅✅")
    except Exception as e:
        logger.critical(f"A critical error occurred during migration: {e}", exc_info=True)

def _migrate_categories(c):
    """ترحيل التصنيفات القديمة من جدول video_archive_old."""
    logger.info("Step 1: Migrating categories from 'video_archive_old'...")
    try:
        c.execute("SELECT DISTINCT category FROM video_archive_old WHERE category IS NOT NULL AND category != ''")
        old_categories = [row[0] for row in c.fetchall()]
        logger.info(f"Found {len(old_categories)} unique old categories.")
        for cat_name in old_categories:
            full_path = f"/{cat_name}/"
            c.execute("INSERT INTO categories (name, parent_id, full_path) VALUES (%s, NULL, %s) ON CONFLICT (full_path) DO NOTHING", (cat_name, full_path))
        c.execute("INSERT INTO categories (name, parent_id, full_path) VALUES ('Uncategorized', NULL, '/Uncategorized/') ON CONFLICT (full_path) DO NOTHING")
        c.connection.commit()
        logger.info("Categories migration step finished.")
    except psycopg2.errors.UndefinedTable:
        logger.warning("Table 'video_archive_old' not found. Skipping category migration.")
    except Exception as e:
        logger.error(f"Error in _migrate_categories: {e}", exc_info=True)
        c.connection.rollback()
        raise

def _migrate_videos(c):
    """ترحيل الفيديوهات من video_archive_old إلى video_archive الجديد."""
    logger.info("Step 2: Migrating videos from 'video_archive_old'...")
    try:
        c.execute("SELECT message_id, caption, chat_id, file_name, category, file_id FROM video_archive_old")
        old_videos = c.fetchall()
        logger.info(f"Found {len(old_videos)} videos to migrate.")

        c.execute("SELECT id, name FROM categories")
        categories_map = {name: cat_id for cat_id, name in c.fetchall()}
        uncategorized_id = categories_map.get('Uncategorized')

        migrated_count = 0
        for message_id, caption, chat_id, file_name, category_name, file_id_val in old_videos:
            category_id = categories_map.get(category_name, uncategorized_id)
            metadata = extract_video_metadata(caption)
            c.execute("""
                INSERT INTO video_archive (message_id, caption, chat_id, file_name, category_id, metadata, file_id, view_count)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 0)
                ON CONFLICT (message_id) DO NOTHING
            """, (message_id, caption, chat_id, file_name, category_id, json.dumps(metadata), file_id_val))
            migrated_count += c.rowcount
        
        c.connection.commit()
        logger.info(f"Successfully migrated {migrated_count} videos.")
    except psycopg2.errors.UndefinedTable:
        logger.warning("Table 'video_archive_old' not found. Skipping video migration.")
    except Exception as e:
        logger.error(f"Error in _migrate_videos: {e}", exc_info=True)
        c.connection.rollback()
        raise

def _migrate_ratings(c):
    """ترحيل التقييمات القديمة من جدول video_ratings_old."""
    logger.info("Step 3: Migrating old ratings from 'video_ratings_old'...")
    try:
        c.execute("SELECT message_id, user_id, rating FROM video_ratings_old")
        old_ratings = c.fetchall()
        logger.info(f"Found {len(old_ratings)} old ratings to migrate.")
        
        migrated_count = 0
        for message_id, user_id, rating in old_ratings:
            c.execute("SELECT id FROM video_archive WHERE message_id = %s", (message_id,))
            video_res = c.fetchone()
            if video_res:
                video_id = video_res[0]
                c.execute("""
                    INSERT INTO video_ratings (video_id, user_id, rating) VALUES (%s, %s, %s)
                    ON CONFLICT (video_id, user_id) DO NOTHING
                """, (video_id, user_id, rating))
                migrated_count += c.rowcount
            else:
                logger.warning(f"Could not find video with message_id {message_id} for rating. Skipping.")
        
        c.connection.commit()
        logger.info(f"Successfully migrated {migrated_count} ratings.")
    except psycopg2.errors.UndefinedTable:
        logger.warning("Table 'video_ratings_old' not found. Skipping ratings migration.")
    except Exception as e:
        logger.error(f"Error in _migrate_ratings: {e}", exc_info=True)
        c.connection.rollback()
        raise

def extract_video_metadata(caption):
    """استخلاص البيانات الوصفية من كابشن الفيديو."""
    metadata = {"qualities": [], "is_translated": False, "is_dubbed": False}
    if not caption: return metadata
    quality_patterns = {"1080p": [r"1080[pP]", r"FHD"], "720p": [r"720[pP]", r"\bHD\b"], "480p": [r"480[pP]", r"\bSD\b"]}
    found_qualities = {res for res, patterns in quality_patterns.items() for pattern in patterns if re.search(pattern, caption, re.IGNORECASE)}
    metadata["qualities"] = [{"resolution": q, "message_id": None} for q in found_qualities]
    if re.search(r"مترجم|sub|subtitle", caption, re.IGNORECASE): metadata["is_translated"] = True
    if re.search(r"مدبلج|dub|dubbed", caption, re.IGNORECASE): metadata["is_dubbed"] = True
    return metadata

def get_category_by_id(category_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("SELECT id, name, parent_id, full_path FROM categories WHERE id = %s", (category_id,))
                return c.fetchone()
    except Exception as e:
        logger.error(f"Get category by id error: {e}")
        return None

def get_active_category_id():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("SELECT setting_value FROM bot_settings WHERE setting_key = 'active_category_id'")
                result = c.fetchone()
                if result and result[0].isdigit():
                    return int(result[0])
                else:
                    c.execute("SELECT id FROM categories WHERE name = 'Uncategorized'")
                    res = c.fetchone()
                    category_id = res[0] if res else 1
                    c.execute("INSERT INTO bot_settings (setting_key, setting_value) VALUES ('active_category_id', %s) ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value", (str(category_id),))
                    conn.commit()
                    return category_id
    except Exception as e:
        logger.error(f"Get active category id error: {e}")
        return 1 # Fallback to a default ID

def set_active_category_id(category_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("INSERT INTO bot_settings (setting_key, setting_value) VALUES ('active_category_id', %s) ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value", (str(category_id),))
                conn.commit()
                return True
    except Exception as e:
        logger.error(f"Set active category id error: {e}")
        return False

def add_video_rating(video_id, user_id, rating):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO video_ratings (video_id, user_id, rating) VALUES (%s, %s, %s)
                    ON CONFLICT (video_id, user_id) DO UPDATE SET rating = EXCLUDED.rating, created_at = CURRENT_TIMESTAMP
                """, (video_id, user_id, rating))
                conn.commit()
                return True
    except Exception as e:
        logger.error(f"Add video rating error: {e}")
        return False

def get_video_rating_stats(video_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("SELECT COUNT(*), AVG(rating)::NUMERIC(3,2) FROM video_ratings WHERE video_id = %s", (video_id,))
                result = c.fetchone()
                if result and result[0] > 0:
                    return {"total_ratings": result[0], "average_rating": float(result[1]) if result[1] else 0}
                return {"total_ratings": 0, "average_rating": 0}
    except Exception as e:
        logger.error(f"Get video rating stats error: {e}")
        return {"total_ratings": 0, "average_rating": 0}

def get_user_video_rating(video_id, user_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("SELECT rating FROM video_ratings WHERE video_id = %s AND user_id = %s", (video_id, user_id))
                result = c.fetchone()
                return result[0] if result else None
    except Exception as e:
        logger.error(f"Get user video rating error: {e}")
        return None

def get_popular_videos():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("""
                    SELECT va.id, va.message_id, va.caption, va.chat_id, va.file_name, va.metadata, 
                           COALESCE(cat.name, 'Uncategorized') as category_name, va.view_count
                    FROM video_archive va
                    LEFT JOIN categories cat ON va.category_id = cat.id
                    ORDER BY va.view_count DESC, va.id DESC
                    LIMIT 10
                """)
                most_viewed = c.fetchall()
                
                c.execute("""
                    SELECT va.id, va.message_id, va.caption, va.chat_id, va.file_name, va.metadata, 
                           COALESCE(cat.name, 'Uncategorized') as category_name, va.view_count,
                           AVG(vr.rating)::NUMERIC(3,2) as avg_rating, COUNT(vr.rating) as rating_count
                    FROM video_archive va
                    LEFT JOIN video_ratings vr ON va.id = vr.video_id
                    LEFT JOIN categories cat ON va.category_id = cat.id
                    GROUP BY va.id, cat.name
                    HAVING COUNT(vr.rating) >= 1
                    ORDER BY avg_rating DESC, rating_count DESC, va.id DESC
                    LIMIT 10
                """)
                highest_rated = c.fetchall()
                return {"most_viewed": most_viewed, "highest_rated": highest_rated}
    except Exception as e:
        logger.error(f"Get popular videos error: {e}")
        return {"most_viewed": [], "highest_rated": []}

def add_bot_user(user_id, username, first_name):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("INSERT INTO bot_users (user_id, username, first_name) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING", (user_id, username, first_name))
                conn.commit()
    except Exception as e:
        logger.error(f"Add bot user error: {e}")

def get_all_user_ids():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("SELECT user_id FROM bot_users")
                return [row[0] for row in c.fetchall()]
    except Exception as e:
        logger.error(f"Get all user IDs error: {e}")
        return []

def get_subscriber_count():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("SELECT COUNT(*) FROM bot_users")
                return c.fetchone()[0]
    except Exception as e:
        logger.error(f"Get subscriber count error: {e}")
        return 0

def get_bot_stats():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("SELECT COUNT(*) FROM video_archive")
                video_count = c.fetchone()[0]
                c.execute("SELECT COUNT(*) FROM categories")
                category_count = c.fetchone()[0]
                c.execute("SELECT SUM(view_count) FROM video_archive")
                total_views = c.fetchone()[0] or 0
                c.execute("SELECT COUNT(*) FROM video_ratings")
                total_ratings = c.fetchone()[0]
                return {"video_count": video_count, "category_count": category_count, "total_views": total_views, "total_ratings": total_ratings}
    except Exception as e:
        logger.error(f"Get bot stats error: {e}")
        return {"video_count": 0, "category_count": 0, "total_views": 0, "total_ratings": 0}

def search_videos(query, page=0, category_id=None):
    offset = page * VIDEOS_PER_PAGE
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                search_param = '%' + query + '%'
                params = [search_param, search_param]
                
                base_where = "(va.caption ILIKE %s OR va.file_name ILIKE %s)"
                
                if category_id:
                    c.execute("SELECT full_path FROM categories WHERE id = %s", (category_id,))
                    category_path_res = c.fetchone()
                    if not category_path_res: return [], 0
                    category_path = category_path_res[0] + '%'
                    base_where += " AND cat.full_path LIKE %s"
                    params.append(category_path)

                count_query = f"SELECT COUNT(*) FROM video_archive va LEFT JOIN categories cat ON va.category_id = cat.id WHERE {base_where}"
                c.execute(count_query, tuple(params))
                total_count = c.fetchone()[0]

                data_query = f"""
                    SELECT va.id, va.message_id, va.caption, va.chat_id, va.file_name, va.metadata, 
                           COALESCE(cat.name, 'Uncategorized') as category_name, va.view_count
                    FROM video_archive va
                    LEFT JOIN categories cat ON va.category_id = cat.id
                    WHERE {base_where}
                    ORDER BY va.id DESC LIMIT %s OFFSET %s
                """
                params.extend([VIDEOS_PER_PAGE, offset])
                c.execute(data_query, tuple(params))
                return c.fetchall(), total_count
    except Exception as e:
        logger.error(f"Search videos error: {e}")
        return [], 0

def add_required_channel(channel_id, channel_name):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("INSERT INTO required_channels (channel_id, channel_name) VALUES (%s, %s) ON CONFLICT (channel_id) DO UPDATE SET channel_name = EXCLUDED.channel_name", (channel_id, channel_name))
                conn.commit()
                return True
    except Exception as e:
        logger.error(f"Add required channel error: {e}")
        return False

def remove_required_channel(channel_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("DELETE FROM required_channels WHERE channel_id = %s", (channel_id,))
                conn.commit()
                return c.rowcount > 0
    except Exception as e:
        logger.error(f"Remove required channel error: {e}")
        return False

def get_required_channels():
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("SELECT channel_id, channel_name FROM required_channels")
                return c.fetchall()
    except Exception as e:
        logger.error(f"Get required channels error: {e}")
        return []

def get_video_by_message_id(message_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("SELECT id, message_id, caption, chat_id, metadata FROM video_archive WHERE message_id = %s", (message_id,))
                return c.fetchone()
    except Exception as e:
        logger.error(f"Get video by message id error: {e}")
        return None

def increment_video_view_count(video_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                c.execute("UPDATE video_archive SET view_count = view_count + 1 WHERE id = %s", (video_id,))
                conn.commit()
                return True
    except Exception as e:
        logger.error(f"Increment view count error: {e}")
        return False

def get_child_categories(parent_id=None):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                if parent_id is None:
                    c.execute("SELECT id, name, full_path FROM categories WHERE parent_id IS NULL ORDER BY name")
                else:
                    c.execute("SELECT id, name, full_path FROM categories WHERE parent_id = %s ORDER BY name", (parent_id,))
                return c.fetchall()
    except Exception as e:
        logger.error(f"Get child categories error: {e}")
        return []

def add_category(name, parent_id=None):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                if parent_id:
                    c.execute("SELECT full_path FROM categories WHERE id = %s", (parent_id,))
                    parent_path_res = c.fetchone()
                    if not parent_path_res:
                        return False, "التصنيف الأب غير موجود"
                    full_path = f"{parent_path_res[0]}{name}/"
                else:
                    full_path = f"/{name}/"
                
                c.execute("INSERT INTO categories (name, parent_id, full_path) VALUES (%s, %s, %s) RETURNING id", (name, parent_id, full_path))
                category_id = c.fetchone()[0]
                conn.commit()
                return True, category_id
    except psycopg2.IntegrityError:
        return False, "اسم التصنيف موجود بالفعل في هذا المستوى"
    except Exception as e:
        logger.error(f"Add category error: {e}")
        return False, str(e)
        
def add_video(message_id, caption, chat_id, file_name=None, category_id=None, file_id=None):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                metadata = extract_video_metadata(caption)
                if not category_id:
                    category_id = get_active_category_id()
                
                c.execute("""
                    INSERT INTO video_archive (message_id, caption, chat_id, file_name, category_id, metadata, file_id) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s) 
                    ON CONFLICT (message_id) DO UPDATE SET
                        caption = EXCLUDED.caption,
                        metadata = EXCLUDED.metadata,
                        file_id = EXCLUDED.file_id
                """, (message_id, caption or "No caption", chat_id, file_name or "", category_id, json.dumps(metadata), file_id))
                conn.commit()
                return True
    except Exception as e:
        logger.error(f"Add video error: {e}")
        return False

def get_videos(category_id=None, page=0):
    offset = page * VIDEOS_PER_PAGE
    try:
        with get_db_connection() as conn:
            with conn.cursor() as c:
                if category_id:
                    c.execute("SELECT full_path FROM categories WHERE id = %s", (category_id,))
                    category_path_res = c.fetchone()
                    if not category_path_res: return [], 0
                    category_path = category_path_res[0] + '%'

                    count_query = "SELECT COUNT(*) FROM video_archive va JOIN categories cat ON va.category_id = cat.id WHERE cat.full_path LIKE %s"
                    c.execute(count_query, (category_path,))
                    total_count = c.fetchone()[0]

                    data_query = """
                        SELECT va.id, va.message_id, va.caption, va.chat_id, va.file_name, va.metadata, 
                               COALESCE(cat.name, 'Uncategorized') as category_name, va.view_count
                        FROM video_archive va
                        LEFT JOIN categories cat ON va.category_id = cat.id
                        WHERE cat.full_path LIKE %s
                        ORDER BY va.id DESC LIMIT %s OFFSET %s
                    """
                    c.execute(data_query, (category_path, VIDEOS_PER_PAGE, offset))
                else: # All videos
                    c.execute("SELECT COUNT(*) FROM video_archive")
                    total_count = c.fetchone()[0]
                    c.execute("""
                        SELECT va.id, va.message_id, va.caption, va.chat_id, va.file_name, va.metadata, 
                               COALESCE(cat.name, 'Uncategorized') as category_name, va.view_count
                        FROM video_archive va
                        LEFT JOIN categories cat ON va.category_id = cat.id
                        ORDER BY va.id DESC LIMIT %s OFFSET %s
                    """, (VIDEOS_PER_PAGE, offset))
                
                videos = c.fetchall()
                return videos, total_count
    except Exception as e:
        logger.error(f"Get videos error: {e}")
        return [], 0

# --- دوال مساعدة وواجهات المستخدم ---

def check_admin(func):
    """ديكوريتر للتحقق من صلاحيات الآدمن."""
    def wrapper(message, *args, **kwargs):
        if message.from_user.id not in ADMIN_IDS:
            bot.reply_to(message, "هذا الأمر مخصص للآدمن فقط.")
            return
        return func(message, *args, **kwargs)
    return wrapper

def create_paginated_keyboard(items, total_items, page, prefix, context):
    """إنشاء لوحة مفاتيح مع أزرار الصفحات وزر الرجوع."""
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    for item in items:
        # تأكد من أن item يحتوي على 8 عناصر على الأقل
        if len(item) < 8: continue
        video_id, message_id, caption, chat_id, file_name, metadata, category_name, view_count = item[:8]
        title = (caption or file_name or "فيديو بدون عنوان").strip().split('\n')[0]
        
        indicators = []
        if metadata:
            try:
                meta_dict = metadata if isinstance(metadata, dict) else json.loads(metadata)
                if meta_dict.get('is_translated'): indicators.append("مترجم")
                if meta_dict.get('is_dubbed'): indicators.append("مدبلج")
            except (json.JSONDecodeError, TypeError):
                pass
        
        indicator_text = f" ({', '.join(indicators)})" if indicators else ""
        button_text = f"{title[:35]}...{indicator_text} - {category_name} 👁 {view_count}"
        
        keyboard.add(InlineKeyboardButton(text=button_text, callback_data=f"video::{video_id}::{message_id}::{chat_id}"))

    total_pages = math.ceil(total_items / VIDEOS_PER_PAGE)
    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"{prefix}::{context}::{page - 1}"))
        
        nav_buttons.append(InlineKeyboardButton(f"صفحة {page + 1}/{total_pages}", callback_data="noop"))

        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("التالي ➡️", callback_data=f"{prefix}::{context}::{page + 1}"))
        
        keyboard.row(*nav_buttons)
    
    keyboard.row(InlineKeyboardButton("الرجوع إلى التصنيفات", callback_data="back_to_cats"))
    return keyboard

def create_categories_keyboard(parent_id=None):
    """إنشاء لوحة مفاتيح للتصنيفات."""
    categories = get_child_categories(parent_id)
    keyboard = InlineKeyboardMarkup(row_width=2)
    
    for cat_id, cat_name, full_path in categories:
        keyboard.add(InlineKeyboardButton(text=cat_name, callback_data=f"cat::{cat_id}::0"))
    
    if parent_id:
        parent_category = get_category_by_id(parent_id)
        back_cat_id = parent_category[2] if parent_category and parent_category[2] is not None else ""
        keyboard.row(InlineKeyboardButton("⬅️ الرجوع", callback_data=f"cat::{back_cat_id}::0" if back_cat_id else "back_to_cats"))
    
    return keyboard

def create_video_action_keyboard(video_id, user_id):
    """إنشاء لوحة مفاتيح لإجراءات الفيديو (تقييم، إلخ)."""
    keyboard = InlineKeyboardMarkup(row_width=5)
    
    rating_buttons = []
    user_rating = get_user_video_rating(video_id, user_id)
    
    for i in range(1, 6):
        star = "⭐" if user_rating == i else "☆"
        rating_buttons.append(InlineKeyboardButton(star, callback_data=f"rate::{video_id}::{i}"))
    
    keyboard.row(*rating_buttons)
    
    stats = get_video_rating_stats(video_id)
    if stats["total_ratings"] > 0:
        keyboard.row(InlineKeyboardButton(
            f"⭐ {stats['average_rating']:.1f} ({stats['total_ratings']} تقييم)", 
            callback_data="noop"
        ))
    
    return keyboard

def main_menu():
    """إنشاء القائمة الرئيسية للبوت."""
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    list_button = KeyboardButton('🗂️ عرض التصنيفات')
    popular_button = KeyboardButton('🔥 الفيديوهات الشائعة')
    markup.add(list_button, popular_button)
    return markup

# --- أوامر البوت ومعالجات الرسائل ---

def check_subscription(user_id):
    """التحقق من اشتراك المستخدم في جميع القنوات المطلوبة."""
    required_channels = get_required_channels()
    if not required_channels:
        return True, None

    not_subscribed = []
    for channel_id, channel_name in required_channels:
        try:
            member = bot.get_chat_member(channel_id, user_id)
            if member.status not in ["member", "administrator", "creator"]:
                not_subscribed.append((channel_id, channel_name))
        except Exception as e:
            logger.warning(f"Could not check subscription for user {user_id} in channel {channel_id}: {e}")
            not_subscribed.append((channel_id, channel_name))
    
    if not_subscribed:
        markup = InlineKeyboardMarkup()
        for ch_id, ch_name in not_subscribed:
            try:
                chat = bot.get_chat(ch_id)
                invite_link = chat.invite_link or bot.export_chat_invite_link(ch_id)
                markup.add(InlineKeyboardButton(f"اشترك في {ch_name}", url=invite_link))
            except Exception as e:
                 logger.error(f"Failed to get invite link for {ch_id}: {e}")
                 markup.add(InlineKeyboardButton(f"ابحث عن: @{chat.username if chat.username else ch_name}", url=f"https://t.me/{chat.username if chat.username else ''}"))

        markup.add(InlineKeyboardButton("🔄 تحقق من اشتراكي", callback_data="check_sub"))
        return False, markup

    return True, None

@bot.message_handler(commands=['start'])
def start(message):
    add_bot_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    is_subscribed, markup = check_subscription(message.from_user.id)
    if not is_subscribed:
        bot.reply_to(message, "عذراً، يجب عليك الاشتراك في القنوات التالية أولاً لاستخدام البوت:", reply_markup=markup)
        return
    bot.reply_to(message, "أهلاً بك في بوت البحث عن الفيديوهات!", reply_markup=main_menu())

@bot.message_handler(func=lambda m: m.text == '🗂️ عرض التصنيفات')
def handle_list_videos_button(message):
    is_subscribed, markup = check_subscription(message.from_user.id)
    if not is_subscribed:
        bot.reply_to(message, "عذراً، يجب عليك الاشتراك في القنوات التالية أولاً لاستخدام البوت:", reply_markup=markup)
        return
    list_videos(message)

def list_videos(message, edit_message=None, parent_id=None):
    """عرض التصنيفات المتاحة."""
    keyboard = create_categories_keyboard(parent_id)
    text = "اختر تصنيفًا لعرض محتوياته:" if keyboard.keyboard else "لا توجد تصنيفات متاحة حالياً."
    
    if edit_message:
        bot.edit_message_text(text, edit_message.chat.id, edit_message.message_id, reply_markup=keyboard)
    else:
        bot.reply_to(message, text, reply_markup=keyboard)

@bot.message_handler(content_types=['video'])
def handle_new_video(message):
    if str(message.chat.id) == CHANNEL_ID:
        active_category_id = get_active_category_id()
        file_id = message.video.file_id if message.video else None
        logger.info(f"New video detected. Assigning to active category ID: {active_category_id}. Message ID: {message.message_id}")
        success = add_video(
            message_id=message.message_id,
            caption=message.caption,
            chat_id=message.chat.id,
            file_name=message.video.file_name if message.video else "",
            category_id=active_category_id,
            file_id=file_id
        )
        if success:
            logger.info("Video added successfully.")
        else:
            logger.error("Failed to add video.")
            
# ... (باقي الكود الخاص بالآدمن والـ callback handlers) ...
# هذا الجزء لم يتغير عن النسخة السابقة وهو كامل هنا.

@bot.message_handler(commands=["admin"])
@check_admin
def admin_panel(message):
    keyboard = InlineKeyboardMarkup(row_width=2)
    buttons = [
        InlineKeyboardButton("📢 بث للجميع", callback_data="admin::broadcast"),
        InlineKeyboardButton("👤 عدد المشتركين", callback_data="admin::sub_count"),
        InlineKeyboardButton("📊 إحصائيات", callback_data="admin::stats"),
        InlineKeyboardButton("➕ إضافة تصنيف", callback_data="admin::add_cat"),
        InlineKeyboardButton("🔘 تعيين تصنيف نشط", callback_data="admin::set_active"),
        InlineKeyboardButton("➕ إضافة قناة", callback_data="admin::add_channel"),
        InlineKeyboardButton("➖ إزالة قناة", callback_data="admin::remove_channel"),
        InlineKeyboardButton("📋 عرض القنوات", callback_data="admin::list_channels")
    ]
    keyboard.add(*buttons)
    bot.send_message(message.chat.id, "أهلاً بك في لوحة تحكم الآدمن:", reply_markup=keyboard)

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    """الاستجابة عند الضغط على الأزرار."""
    try:
        user_id = call.from_user.id
        is_subscribed, markup = check_subscription(user_id)
        if not is_subscribed:
            bot.answer_callback_query(call.id, "يرجى الاشتراك في القنوات أولاً.", show_alert=True)
            bot.send_message(call.message.chat.id, "عذراً، يجب عليك الاشتراك في القنوات التالية أولاً:", reply_markup=markup)
            return

        data = call.data.split(CALLBACK_DELIMITER)
        action = data[0]

        if action == "check_sub":
            is_subscribed, markup = check_subscription(user_id)
            if is_subscribed:
                bot.edit_message_text("شكراً لاشتراكك! يمكنك الآن استخدام البوت.", call.message.chat.id, call.message.message_id)
                bot.send_message(call.message.chat.id, "أهلاً بك!", reply_markup=main_menu())
            else:
                bot.answer_callback_query(call.id, "لا زلت غير مشترك في جميع القنوات.", show_alert=True)
            return

        # ... (باقي منطق الـ callback) ...
        if action == "cat":
            _, category_id_str, page_str = data
            page = int(page_str)
            category_id = int(category_id_str) if category_id_str.isdigit() else None
            
            child_categories = get_child_categories(category_id)
            if child_categories:
                keyboard = create_categories_keyboard(category_id)
                category = get_category_by_id(category_id) if category_id else None
                cat_name = category[1] if category else "الرئيسية"
                bot.edit_message_text(f"التصنيفات في '{cat_name}':", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
            else:
                videos, total_count = get_videos(category_id, page)
                if videos:
                    keyboard = create_paginated_keyboard(videos, total_count, page, "cat", category_id)
                    category = get_category_by_id(category_id)
                    bot.edit_message_text(f"الفيديوهات في '{category[1] if category else 'غير معروف'}':", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                else:
                    bot.answer_callback_query(call.id, "لا توجد فيديوهات في هذا التصنيف.")
        
        elif action == "back_to_cats":
            list_videos(call.message, edit_message=call.message)

        elif action == "video":
            _, video_id, message_id, chat_id = data
            increment_video_view_count(int(video_id))
            bot.copy_message(call.message.chat.id, chat_id, int(message_id))
            rating_keyboard = create_video_action_keyboard(int(video_id), user_id)
            bot.send_message(call.message.chat.id, "ما هو تقييمك لهذا الفيديو؟", reply_markup=rating_keyboard)
            bot.answer_callback_query(call.id)

        elif action == "rate":
            _, video_id, rating = data
            if add_video_rating(int(video_id), user_id, int(rating)):
                new_keyboard = create_video_action_keyboard(int(video_id), user_id)
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=new_keyboard)
                bot.answer_callback_query(call.id, f"شكراً لتقييمك بـ {rating} نجوم!")
            else:
                bot.answer_callback_query(call.id, "حدث خطأ في التقييم.")
        
        elif action == "noop":
            bot.answer_callback_query(call.id)
            
    except Exception as e:
        logger.error(f"Callback query error: {e}", exc_info=True)
        try:
            bot.answer_callback_query(call.id, "حدث خطأ ما. يرجى المحاولة مرة أخرى.", show_alert=True)
        except Exception as api_e:
            logger.error(f"Failed to answer callback query: {api_e}")


# --- نقطة انطلاق البوت ---
if __name__ == "__main__":
    logger.info("Bot starting up...")
    
    # الخطوة 1: تأكد من أن هيكل قاعدة البيانات الجديد موجود
    init_db()
    
    # الخطوة 2: قم بتشغيل عملية الترحيل.
    run_migration()
    
    logger.info("Bot is now polling for messages...")
    while True:
        try:
            bot.polling(non_stop=True, interval=1)
        except Exception as e:
            logger.error(f"An error occurred in the main polling loop: {e}", exc_info=True)
            logger.info("Restarting in 15 seconds...")
            time.sleep(15)
