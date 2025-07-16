# ==============================================================================
# ملف: bot.py (النسخة المصححة نهائياً)
# الوصف: النسخة المطورة من البوت مع التصنيفات الشجرية واختيار الجودة والبث الغني
#        مع تصحيحات شاملة لهيكل قاعدة البيانات وعملية الترحيل والتقييمات.
# ==============================================================================

import telebot
import psycopg2
import os
import time
import re
import json
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from urllib.parse import urlparse
import math
from datetime import datetime

# --- الإعدادات الأساسية (قراءة آمنة من متغيرات البيئة) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id] # معرفات حسابات الآدمن

if not all([BOT_TOKEN, DATABASE_URL, CHANNEL_ID, ADMIN_IDS]):
    print("FATAL ERROR: Missing one or more environment variables (BOT_TOKEN, DATABASE_URL, CHANNEL_ID, ADMIN_IDS).")
    exit()

bot = telebot.TeleBot(BOT_TOKEN)

DB_CONFIG = {}
if DATABASE_URL:
    url = urlparse(DATABASE_URL)
    DB_CONFIG = {
        "dbname": url.path[1:],
        "user": url.username,
        "password": url.password,
        "host": url.hostname,
        "port": url.port
    }

# قاموس مؤقت لتخزين بيانات عمليات الآدمن
admin_steps = {}
user_last_search = {} # لتخزين آخر عملية بحث لكل مستخدم
VIDEOS_PER_PAGE = 10 # عدد الفيديوهات في كل صفحة
CALLBACK_DELIMITER = "::" # فاصل آمن للبيانات

# --- دوال قاعدة البيانات ---

def init_db():
    """إنشاء الجداول اللازمة إذا لم تكن موجودة."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        # جدول التصنيفات الجديد (شجري)
        c.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                parent_id INTEGER REFERENCES categories(id) ON DELETE CASCADE,
                full_path TEXT NOT NULL UNIQUE
            )
        """)
        
        # جدول الفيديوهات المحدث
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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # جدول التقييمات
        c.execute("""
            CREATE TABLE IF NOT EXISTS video_ratings (
                id SERIAL PRIMARY KEY,
                video_id INTEGER REFERENCES video_archive(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                rating INTEGER CHECK (rating >= 1 AND rating <= 5),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(video_id, user_id)
            )
        """)
        
        # باقي الجداول
        c.execute("""
            CREATE TABLE IF NOT EXISTS bot_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT
            )
        """)
        
        c.execute("""
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        c.execute("""
            CREATE TABLE IF NOT EXISTS required_channels (
                channel_id BIGINT PRIMARY KEY,
                channel_name TEXT
            )
        """)
        
        conn.commit()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Database error during init: {e}")

def migrate_old_data():
    """ترحيل البيانات القديمة من النظام القديم إلى النظام الجديد."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        # التحقق من وجود عمود category القديم في video_archive
        c.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'video_archive' AND column_name = 'category'
        """)
        
        if c.fetchone():
            print("Found old category column in video_archive, starting migration...")
            
            # الحصول على جميع التصنيفات الفريدة من النظام القديم
            c.execute("SELECT DISTINCT category FROM video_archive WHERE category IS NOT NULL AND category != ''")
            old_categories = [row[0] for row in c.fetchall()]
            print(f"Found {len(old_categories)} unique categories to migrate: {old_categories}")

            # إنشاء التصنيفات في الجدول الجديد
            for cat_name in old_categories:
                full_path = f"/{cat_name}/"
                try:
                    c.execute("""
                        INSERT INTO categories (name, parent_id, full_path)
                        VALUES (%s, NULL, %s)
                        ON CONFLICT (full_path) DO NOTHING
                    """, (cat_name, full_path))
                    print(f"Created category: {cat_name}")
                except Exception as e:
                    print(f"Error creating category {cat_name}: {e}")

            # تحديث جدول video_archive لاستخدام category_id
            c.execute("""
                UPDATE video_archive 
                SET category_id = categories.id
                FROM categories
                WHERE video_archive.category = categories.name
                AND video_archive.category_id IS NULL
            """)
            
            # التحقق من عدد الفيديوهات التي تم ترحيلها
            c.execute("SELECT COUNT(*) FROM video_archive WHERE category_id IS NOT NULL")
            migrated_count = c.fetchone()[0]
            print(f"Migrated {migrated_count} videos to new category system")

            # حذف عمود category القديم بعد الترحيل
            c.execute("ALTER TABLE video_archive DROP COLUMN IF EXISTS category")
            
            conn.commit()
            print("Migration completed successfully.")
        else:
            print("No old category column found in video_archive, checking if migration is needed...")
            
            # التحقق من وجود فيديوهات بدون category_id
            c.execute("SELECT COUNT(*) FROM video_archive WHERE category_id IS NULL")
            orphaned_videos = c.fetchone()[0]
            
            if orphaned_videos > 0:
                print(f"Found {orphaned_videos} videos without category_id, creating default category...")
                
                # إنشاء تصنيف افتراضي للفيديوهات اليتيمة
                default_category_name = "Uncategorized"
                full_path = f"/{default_category_name}/"
                
                c.execute("""
                    INSERT INTO categories (name, parent_id, full_path)
                    VALUES (%s, NULL, %s)
                    ON CONFLICT (full_path) DO NOTHING
                    RETURNING id
                """, (default_category_name, full_path))
                
                result = c.fetchone()
                if result:
                    default_category_id = result[0]
                else:
                    # إذا كان التصنيف موجود بالفعل، احصل على معرفه
                    c.execute("SELECT id FROM categories WHERE full_path = %s", (full_path,))
                    default_category_id = c.fetchone()[0]
                
                # تحديث الفيديوهات اليتيمة
                c.execute("""
                    UPDATE video_archive 
                    SET category_id = %s 
                    WHERE category_id IS NULL
                """, (default_category_id,))
                
                conn.commit()
                print(f"Assigned {orphaned_videos} orphaned videos to default category")
            
        conn.close()
    except Exception as e:
        print(f"Migration error: {e}")

def extract_video_metadata(caption):
    """استخلاص البيانات الوصفية من كابشن الفيديو."""
    metadata = {"qualities": [], "is_translated": False, "is_dubbed": False}
    if not caption:
        return metadata

    # استخلاص الجودات
    quality_patterns = {
        "1080p": [r"1080[pP]", r"FHD", r"Full\s*HD"],
        "720p": [r"720[pP]", r"\bHD\b"],
        "480p": [r"480[pP]", r"\bSD\b"]
    }
    
    found_qualities = set()
    for res, patterns in quality_patterns.items():
        for pattern in patterns:
            if re.search(pattern, caption, re.IGNORECASE):
                found_qualities.add(res)
    
    # إضافة الجودات المكتشفة
    for quality in found_qualities:
        metadata["qualities"].append({"resolution": quality, "message_id": None})

    # استخلاص حالة الترجمة/الدبلجة
    if re.search(r"مترجم|sub|subbed|subtitle", caption, re.IGNORECASE):
        metadata["is_translated"] = True
    if re.search(r"مدبلج|dub|dubbed|arabic", caption, re.IGNORECASE):
        metadata["is_dubbed"] = True

    return metadata

# --- دوال التصنيفات الجديدة ---

def add_category(name, parent_id=None):
    """إضافة تصنيف جديد."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        # بناء المسار الكامل
        if parent_id:
            c.execute("SELECT full_path FROM categories WHERE id = %s", (parent_id,))
            parent_path = c.fetchone()
            if parent_path:
                full_path = f"{parent_path[0]}{name}/"
            else:
                return False, "التصنيف الأب غير موجود"
        else:
            full_path = f"/{name}/"
        
        c.execute("""
            INSERT INTO categories (name, parent_id, full_path)
            VALUES (%s, %s, %s)
            RETURNING id
        """, (name, parent_id, full_path))
        
        category_id = c.fetchone()[0]
        conn.commit()
        conn.close()
        return True, category_id
    except psycopg2.IntegrityError:
        return False, "اسم التصنيف موجود بالفعل"
    except Exception as e:
        print(f"Add category error: {e}")
        return False, str(e)

def get_categories_tree():
    """الحصول على شجرة التصنيفات."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("""
            SELECT id, name, parent_id, full_path 
            FROM categories 
            ORDER BY full_path
        """)
        categories = c.fetchall()
        conn.close()
        return categories
    except Exception as e:
        print(f"Get categories tree error: {e}")
        return []

def get_child_categories(parent_id=None):
    """الحصول على التصنيفات الفرعية لتصنيف معين."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        if parent_id is None:
            c.execute("""
                SELECT id, name, full_path 
                FROM categories 
                WHERE parent_id IS NULL
                ORDER BY name
            """)
        else:
            c.execute("""
                SELECT id, name, full_path 
                FROM categories 
                WHERE parent_id = %s
                ORDER BY name
            """, (parent_id,))
        categories = c.fetchall()
        conn.close()
        return categories
    except Exception as e:
        print(f"Get child categories error: {e}")
        return []

def get_category_by_id(category_id):
    """الحصول على تصنيف بواسطة المعرف."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT id, name, parent_id, full_path FROM categories WHERE id = %s", (category_id,))
        category = c.fetchone()
        conn.close()
        return category
    except Exception as e:
        print(f"Get category by id error: {e}")
        return None

# --- دوال الفيديوهات المحدثة ---

def add_video(message_id, caption, chat_id, file_name=None, category_id=None):
    """إضافة فيديو جديد إلى قاعدة البيانات."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        # استخلاص البيانات الوصفية من الكابشن
        metadata = extract_video_metadata(caption)
        
        # إذا لم يتم تحديد category_id، استخدم التصنيف النشط
        if not category_id:
            category_id = get_active_category_id()
        
        c.execute("""
            INSERT INTO video_archive (message_id, caption, chat_id, file_name, category_id, metadata) 
            VALUES (%s, %s, %s, %s, %s, %s) 
            ON CONFLICT (message_id) DO UPDATE SET
                caption = EXCLUDED.caption,
                metadata = EXCLUDED.metadata
        """, (message_id, caption or "No caption", chat_id, file_name or "", category_id, json.dumps(metadata)))
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Add video error: {e}")
        return False

def get_videos(category_id=None, page=0):
    """استرداد الفيديوهات من قاعدة البيانات مع نظام الصفحات."""
    offset = page * VIDEOS_PER_PAGE
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        if category_id:
            # الحصول على الفيديوهات في تصنيف معين وجميع التصنيفات الفرعية
            c.execute("""
                SELECT COUNT(*) FROM video_archive va
                LEFT JOIN categories cat ON va.category_id = cat.id
                WHERE cat.id = %s OR cat.full_path LIKE (
                    SELECT full_path || '%%' FROM categories WHERE id = %s
                )
            """, (category_id, category_id))
            total_count = c.fetchone()[0]

            c.execute("""
                SELECT va.id, va.message_id, va.caption, va.chat_id, va.file_name, va.metadata, 
                       COALESCE(cat.name, 'Uncategorized') as category_name, va.view_count
                FROM video_archive va
                LEFT JOIN categories cat ON va.category_id = cat.id
                WHERE cat.id = %s OR cat.full_path LIKE (
                    SELECT full_path || '%%' FROM categories WHERE id = %s
                )
                ORDER BY va.id LIMIT %s OFFSET %s
            """, (category_id, category_id, VIDEOS_PER_PAGE, offset))
        else:
            c.execute("SELECT COUNT(*) FROM video_archive")
            total_count = c.fetchone()[0]

            c.execute("""
                SELECT va.id, va.message_id, va.caption, va.chat_id, va.file_name, va.metadata, 
                       COALESCE(cat.name, 'Uncategorized') as category_name, va.view_count
                FROM video_archive va
                LEFT JOIN categories cat ON va.category_id = cat.id
                ORDER BY va.id LIMIT %s OFFSET %s
            """, (VIDEOS_PER_PAGE, offset))
        
        videos = c.fetchall()
        conn.close()
        return videos, total_count
    except Exception as e:
        print(f"Get videos error: {e}")
        return [], 0

def increment_video_view_count(video_id):
    """زيادة عداد المشاهدات للفيديو."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("UPDATE video_archive SET view_count = view_count + 1 WHERE id = %s", (video_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Increment view count error: {e}")
        return False

def get_video_by_message_id(message_id):
    """الحصول على فيديو بواسطة message_id."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT id, message_id, caption, chat_id, metadata FROM video_archive WHERE message_id = %s", (message_id,))
        video = c.fetchone()
        conn.close()
        return video
    except Exception as e:
        print(f"Get video by message id error: {e}")
        return None

def get_active_category_id():
    """الحصول على معرف التصنيف النشط."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT setting_value FROM bot_settings WHERE setting_key = 'active_category_id'")
        result = c.fetchone()
        
        if result:
            conn.close()
            return int(result[0])
        else:
            # إنشاء تصنيف افتراضي إذا لم يوجد
            c.execute("""
                INSERT INTO categories (name, parent_id, full_path)
                VALUES ('Uncategorized', NULL, '/Uncategorized/')
                ON CONFLICT (full_path) DO NOTHING
                RETURNING id
            """)
            
            result = c.fetchone()
            if result:
                category_id = result[0]
            else:
                # إذا كان التصنيف موجود بالفعل، احصل على معرفه
                c.execute("SELECT id FROM categories WHERE full_path = '/Uncategorized/'")
                category_id = c.fetchone()[0]
            
            # تعيين التصنيف كنشط
            c.execute("""
                INSERT INTO bot_settings (setting_key, setting_value) 
                VALUES ('active_category_id', %s)
                ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value
            """, (str(category_id),))
            
            conn.commit()
            conn.close()
            return category_id
    except Exception as e:
        print(f"Get active category id error: {e}")
        return None

def set_active_category_id(category_id):
    """تعيين التصنيف النشط."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("""
            INSERT INTO bot_settings (setting_key, setting_value) 
            VALUES ('active_category_id', %s)
            ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value
        """, (str(category_id),))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Set active category id error: {e}")
        return False

# --- دوال التقييمات ---

def add_video_rating(video_id, user_id, rating):
    """إضافة أو تحديث تقييم فيديو."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("""
            INSERT INTO video_ratings (video_id, user_id, rating)
            VALUES (%s, %s, %s)
            ON CONFLICT (video_id, user_id) DO UPDATE SET
                rating = EXCLUDED.rating,
                created_at = CURRENT_TIMESTAMP
        """, (video_id, user_id, rating))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Add video rating error: {e}")
        return False

def get_video_rating_stats(video_id):
    """الحصول على إحصائيات تقييم فيديو."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("""
            SELECT 
                COUNT(*) as total_ratings,
                AVG(rating)::NUMERIC(3,2) as average_rating
            FROM video_ratings 
            WHERE video_id = %s
        """, (video_id,))
        result = c.fetchone()
        conn.close()
        
        if result and result[0] > 0:
            return {
                "total_ratings": result[0],
                "average_rating": float(result[1]) if result[1] else 0
            }
        return {"total_ratings": 0, "average_rating": 0}
    except Exception as e:
        print(f"Get video rating stats error: {e}")
        return {"total_ratings": 0, "average_rating": 0}

def get_user_video_rating(video_id, user_id):
    """الحصول على تقييم المستخدم لفيديو معين."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT rating FROM video_ratings WHERE video_id = %s AND user_id = %s", (video_id, user_id))
        result = c.fetchone()
        conn.close()
        return result[0] if result else None
    except Exception as e:
        print(f"Get user video rating error: {e}")
        return None

def get_popular_videos():
    """الحصول على الفيديوهات الأكثر شعبية."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        # الأكثر مشاهدة
        c.execute("""
            SELECT va.id, va.message_id, va.caption, va.chat_id, va.file_name, va.metadata, 
                   COALESCE(cat.name, 'Uncategorized') as category_name, va.view_count
            FROM video_archive va
            LEFT JOIN categories cat ON va.category_id = cat.id
            ORDER BY va.view_count DESC
            LIMIT 10
        """)
        most_viewed = c.fetchall()
        
        # الأعلى تقييماً
        c.execute("""
            SELECT va.id, va.message_id, va.caption, va.chat_id, va.file_name, va.metadata, 
                   COALESCE(cat.name, 'Uncategorized') as category_name, va.view_count,
                   AVG(vr.rating)::NUMERIC(3,2) as avg_rating, COUNT(vr.rating) as rating_count
            FROM video_archive va
            LEFT JOIN categories cat ON va.category_id = cat.id
            LEFT JOIN video_ratings vr ON va.id = vr.video_id
            GROUP BY va.id, cat.name
            HAVING COUNT(vr.rating) >= 3
            ORDER BY AVG(vr.rating) DESC, COUNT(vr.rating) DESC
            LIMIT 10
        """)
        highest_rated = c.fetchall()
        
        conn.close()
        return {
            "most_viewed": most_viewed,
            "highest_rated": highest_rated
        }
    except Exception as e:
        print(f"Get popular videos error: {e}")
        return {"most_viewed": [], "highest_rated": []}

# --- باقي الدوال من النسخة السابقة (مع التعديلات اللازمة) ---

def add_bot_user(user_id, username, first_name):
    """إضافة مستخدم جديد إلى قاعدة البيانات عند أول تفاعل."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("""
            INSERT INTO bot_users (user_id, username, first_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO NOTHING
        """, (user_id, username, first_name))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Add bot user error: {e}")

def get_all_user_ids():
    """الحصول على جميع معرفات المستخدمين للبث."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT user_id FROM bot_users")
        user_ids = [row[0] for row in c.fetchall()]
        conn.close()
        return user_ids
    except Exception as e:
        print(f"Get all user IDs error: {e}")
        return []

def get_subscriber_count():
    """الحصول على العدد الإجمالي للمشتركين."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM bot_users")
        count = c.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        print(f"Get subscriber count error: {e}")
        return 0

def get_bot_stats():
    """الحصول على إحصائيات المحتوى."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM video_archive")
        video_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM categories")
        category_count = c.fetchone()[0]
        c.execute("SELECT SUM(view_count) FROM video_archive")
        total_views = c.fetchone()[0] or 0
        c.execute("SELECT COUNT(*) FROM video_ratings")
        total_ratings = c.fetchone()[0]
        conn.close()
        return {
            "video_count": video_count,
            "category_count": category_count,
            "total_views": total_views,
            "total_ratings": total_ratings
        }
    except Exception as e:
        print(f"Get bot stats error: {e}")
        return {"video_count": 0, "category_count": 0, "total_views": 0, "total_ratings": 0}

def search_videos(query, page=0, category_id=None):
    """البحث عن فيديوهات في قاعدة البيانات مع تطبيع الحروف العربية."""
    offset = page * VIDEOS_PER_PAGE
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        def normalize_text(text):
            text = text.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا')
            text = text.replace('ة', 'ه')
            text = text.replace('ى', 'ي')
            return text

        normalized_query = normalize_text(query)
        search_param = '%' + normalized_query + '%'

        def normalize_sql(column_name):
            return f"REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({column_name}, 'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ة', 'ه'), 'ى', 'ي')"

        base_where = f"({normalize_sql('va.caption')} ILIKE %s OR {normalize_sql('va.file_name')} ILIKE %s)"
        params = [search_param, search_param]

        if category_id:
            base_where += " AND (cat.id = %s OR cat.full_path LIKE (SELECT full_path || '%%' FROM categories WHERE id = %s))"
            params.extend([category_id, category_id])

        count_query = f"""
            SELECT COUNT(*) FROM video_archive va
            LEFT JOIN categories cat ON va.category_id = cat.id
            WHERE {base_where}
        """
        c.execute(count_query, tuple(params))
        total_count = c.fetchone()[0]

        data_query = f"""
            SELECT va.id, va.message_id, va.caption, va.chat_id, va.file_name, va.metadata, 
                   COALESCE(cat.name, 'Uncategorized') as category_name, va.view_count
            FROM video_archive va
            LEFT JOIN categories cat ON va.category_id = cat.id
            WHERE {base_where}
            ORDER BY va.id LIMIT %s OFFSET %s
        """
        params.extend([VIDEOS_PER_PAGE, offset])
        c.execute(data_query, tuple(params))
        results = c.fetchall()
        
        conn.close()
        return results, total_count
    except Exception as e:
        print(f"Search videos error: {e}")
        return [], 0

# --- دوال القنوات المطلوبة ---

def add_required_channel(channel_id, channel_name):
    """إضافة قناة مطلوبة إلى قاعدة البيانات."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("""
            INSERT INTO required_channels (channel_id, channel_name)
            VALUES (%s, %s)
            ON CONFLICT (channel_id) DO UPDATE SET channel_name = EXCLUDED.channel_name
        """, (channel_id, channel_name))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Add required channel error: {e}")
        return False

def remove_required_channel(channel_id):
    """إزالة قناة مطلوبة من قاعدة البيانات."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("DELETE FROM required_channels WHERE channel_id = %s", (channel_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Remove required channel error: {e}")
        return False

def get_required_channels():
    """الحصول على جميع القنوات المطلوبة."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT channel_id, channel_name FROM required_channels")
        channels = c.fetchall()
        conn.close()
        return channels
    except Exception as e:
        print(f"Get required channels error: {e}")
        return []

def check_subscription(user_id, channel_id):
    """التحقق مما إذا كان المستخدم مشتركًا في قناة معينة."""
    try:
        member = bot.get_chat_member(channel_id, user_id)
        if member.status in ["member", "administrator", "creator"]:
            return True
        return False
    except Exception as e:
        print(f"Check subscription error for user {user_id} in channel {channel_id}: {e}")
        return False

# --- دوال مساعدة ---

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
        video_id, message_id, caption, chat_id, file_name, metadata, category_name, view_count = item
        title = caption or file_name or "فيديو بدون عنوان"
        
        # إضافة مؤشرات الترجمة/الدبلجة
        indicators = []
        if metadata:
            try:
                meta_dict = json.loads(metadata) if isinstance(metadata, str) else metadata
                if meta_dict.get('is_translated'):
                    indicators.append("مترجم")
                if meta_dict.get('is_dubbed'):
                    indicators.append("مدبلج")
            except:
                pass
        
        indicator_text = f" ({', '.join(indicators)})" if indicators else ""
        button_text = f"{title[:35]}{indicator_text} - {category_name} 👁 {view_count}"
        
        keyboard.add(InlineKeyboardButton(text=button_text, callback_data=f"video::{video_id}::{message_id}::{chat_id}"))

    # أزرار التنقل
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
    
    if parent_id:  # إذا كنا في تصنيف فرعي، أضف زر الرجوع
        parent_category = get_category_by_id(parent_id)
        if parent_category and parent_category[2] is not None:  # parent_id موجود
            keyboard.row(InlineKeyboardButton("⬅️ الرجوع", callback_data=f"cat::{parent_category[2]}::0"))
        else:
            keyboard.row(InlineKeyboardButton("⬅️ الرجوع للرئيسية", callback_data="back_to_cats"))
    
    return keyboard

def create_video_action_keyboard(video_id, user_id):
    """إنشاء لوحة مفاتيح لإجراءات الفيديو (تقييم، إلخ)."""
    keyboard = InlineKeyboardMarkup(row_width=5)
    
    # أزرار التقييم
    rating_buttons = []
    user_rating = get_user_video_rating(video_id, user_id)
    
    for i in range(1, 6):
        star = "⭐" if user_rating == i else "☆"
        rating_buttons.append(InlineKeyboardButton(star, callback_data=f"rate::{video_id}::{i}"))
    
    keyboard.row(*rating_buttons)
    
    # إحصائيات التقييم
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
    list_button = KeyboardButton('🎬 عرض كل الفيديوهات')
    popular_button = KeyboardButton('🔥 الفيديوهات الشائعة')
    markup.add(list_button, popular_button)
    return markup

# --- أوامر البوت ---

@bot.message_handler(commands=['start'])
def start(message):
    add_bot_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    
    required_channels = get_required_channels()
    if required_channels:
        not_subscribed_channels = []
        for channel_id, channel_name in required_channels:
            if not check_subscription(message.from_user.id, channel_id):
                not_subscribed_channels.append((channel_id, channel_name))
        
        if not_subscribed_channels:
            markup = InlineKeyboardMarkup()
            for channel_id, channel_name in not_subscribed_channels:
                markup.add(InlineKeyboardButton(f"اشترك في {channel_name}", url=f"https://t.me/c/{str(channel_id).replace('-100', '')}"))
            bot.reply_to(message, "يرجى الاشتراك في القنوات التالية لاستخدام البوت:", reply_markup=markup)
            return

    bot.reply_to(message, "أهلاً بك في بوت البحث عن الفيديوهات!", reply_markup=main_menu())

@bot.message_handler(commands=["myid"])
def get_my_id(message):
    bot.reply_to(message, f"معرف حسابك هو: `{message.from_user.id}`", parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == '🎬 عرض كل الفيديوهات')
def handle_list_videos_button(message):
    list_videos(message)

@bot.message_handler(func=lambda message: message.text == '🔥 الفيديوهات الشائعة')
def handle_popular_videos_button(message):
    show_popular_videos(message)

def show_popular_videos(message):
    """عرض الفيديوهات الشائعة."""
    popular = get_popular_videos()
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("📈 الأكثر مشاهدة", callback_data="popular::most_viewed"))
    keyboard.add(InlineKeyboardButton("⭐ الأعلى تقييماً", callback_data="popular::highest_rated"))
    
    bot.reply_to(message, "اختر نوع الفيديوهات الشائعة:", reply_markup=keyboard)

def list_videos(message, edit_message=None, parent_id=None):
    """عرض التصنيفات المتاحة."""
    keyboard = create_categories_keyboard(parent_id)
    
    if keyboard.keyboard:  # إذا كان هناك تصنيفات
        text = "اختر تصنيفًا لعرض محتوياته:"
        if edit_message:
            bot.edit_message_text(text, edit_message.chat.id, edit_message.message_id, reply_markup=keyboard)
        else:
            bot.reply_to(message, text, reply_markup=keyboard)
    else:
        text = "لا توجد تصنيفات متاحة حالياً."
        if edit_message:
            bot.answer_callback_query(edit_message.id, text)
        else:
            bot.reply_to(message, text)

# --- لوحة تحكم الآدمن ---

def generate_admin_panel():
    """إنشاء لوحة تحكم الآدمن."""
    keyboard = InlineKeyboardMarkup(row_width=2)
    btn_broadcast = InlineKeyboardButton("📢 إرسال رسالة للجميع", callback_data="admin::broadcast")
    btn_subs = InlineKeyboardButton("👤 عدد المشتركين", callback_data="admin::sub_count")
    btn_stats = InlineKeyboardButton("📊 إحصائيات المحتوى", callback_data="admin::stats")
    btn_add_cat = InlineKeyboardButton("➕ إضافة تصنيف جديد", callback_data="admin::add_new_cat")
    btn_set_active = InlineKeyboardButton("🔘 تعيين التصنيف النشط", callback_data="admin::set_active")
    btn_help = InlineKeyboardButton("ℹ️ عرض المساعدة", callback_data="admin::help")
    btn_add_channel = InlineKeyboardButton("➕ إضافة قناة مطلوبة", callback_data="admin::add_channel")
    btn_remove_channel = InlineKeyboardButton("➖ إزالة قناة مطلوبة", callback_data="admin::remove_channel")
    btn_list_channels = InlineKeyboardButton("📋 عرض القنوات المطلوبة", callback_data="admin::list_channels")
    
    keyboard.add(btn_broadcast, btn_subs, btn_stats, btn_add_cat, btn_set_active, btn_help, btn_add_channel, btn_remove_channel, btn_list_channels)
    return keyboard

@bot.message_handler(commands=["admin"])
@check_admin
def admin_panel(message):
    bot.send_message(message.chat.id, "أهلاً بك في لوحة تحكم الآدمن. اختر أحد الخيارات:", reply_markup=generate_admin_panel())

@bot.message_handler(commands=["cancel"])
@check_admin
def cancel_step(message):
    if message.chat.id in admin_steps:
        del admin_steps[message.chat.id]
        bot.send_message(message.chat.id, "✅ تم إلغاء العملية الحالية بنجاح.")
    else:
        bot.send_message(message.chat.id, "لا توجد عملية لإلغائها.")

# --- معالجات خطوات الآدمن ---

def check_cancel(message):
    """دالة للتحقق من أمر الإلغاء في أي خطوة."""
    if message.text == '/cancel':
        if message.chat.id in admin_steps:
            del admin_steps[message.chat.id]
        bot.send_message(message.chat.id, "✅ تم إلغاء العملية الحالية بنجاح.")
        return True
    return False

def handle_rich_broadcast(message):
    """معالج البث الغني (نص، صور، فيديوهات)."""
    if check_cancel(message): return
    
    user_ids = get_all_user_ids()
    sent_count = 0
    failed_count = 0
    
    bot.send_message(message.chat.id, f"بدء إرسال الرسالة إلى {len(user_ids)} مشترك. قد تستغرق هذه العملية بعض الوقت...")
    
    for user_id in user_ids:
        try:
            # إعادة توجيه الرسالة الأصلية من الآدمن
            bot.forward_message(chat_id=user_id, from_chat_id=message.chat.id, message_id=message.message_id)
            sent_count += 1
        except Exception as e:
            failed_count += 1
            print(f"Failed to send to {user_id}: {e}")
        time.sleep(0.1) # لتجنب تجاوز حدود تليجرام
        
    bot.send_message(message.chat.id, f"✅ اكتمل البث!\n\n- رسائل ناجحة: {sent_count}\n- رسائل فاشلة: {failed_count}")

def handle_add_new_category(message):
    if check_cancel(message): return
    category_name = message.text.strip()
    
    # يمكن تحسين هذا لاحقًا لدعم التصنيفات الفرعية
    success, result = add_category(category_name)
    if success:
        set_active_category_id(result)
        bot.reply_to(message, f"✅ تم إنشاء وتفعيل التصنيف الجديد بنجاح: '{category_name}'.")
    else:
        bot.reply_to(message, f"❌ خطأ في إنشاء التصنيف: {result}")

# --- معالجات القنوات المطلوبة ---

def handle_add_channel_step1(message):
    if check_cancel(message): return
    try:
        channel_id = int(message.text.strip())
        admin_steps[message.chat.id] = {"channel_id": channel_id}
        msg = bot.send_message(message.chat.id, "الآن أرسل اسم القناة (مثال: قناة الأفلام). (أو أرسل /cancel للإلغاء)")
        bot.register_next_step_handler(msg, handle_add_channel_step2)
    except ValueError:
        msg = bot.send_message(message.chat.id, "معرف القناة غير صالح. يرجى إرسال رقم صحيح. (أو أرسل /cancel للإلغاء)")
        bot.register_next_step_handler(msg, handle_add_channel_step1)

def handle_add_channel_step2(message):
    if check_cancel(message): return
    channel_name = message.text.strip()
    channel_id = admin_steps.pop(message.chat.id, {}).get("channel_id")
    if not channel_id: return

    if add_required_channel(channel_id, channel_name):
        bot.send_message(message.chat.id, f"✅ تم إضافة القناة '{channel_name}' (ID: {channel_id}) كقناة مطلوبة.")
    else:
        bot.send_message(message.chat.id, "❌ حدث خطأ أثناء إضافة القناة.")

def handle_remove_channel_step(message):
    if check_cancel(message): return
    try:
        channel_id = int(message.text.strip())
        if remove_required_channel(channel_id):
            bot.send_message(message.chat.id, f"✅ تم إزالة القناة (ID: {channel_id}) من القنوات المطلوبة.")
        else:
            bot.send_message(message.chat.id, "❌ حدث خطأ أثناء إزالة القناة أو أنها غير موجودة.")
    except ValueError:
        msg = bot.send_message(message.chat.id, "معرف القناة غير صالح. يرجى إرسال رقم صحيح. (أو أرسل /cancel للإلغاء)")
        bot.register_next_step_handler(msg, handle_remove_channel_step)

def handle_list_channels(message):
    channels = get_required_channels()
    if channels:
        response = "📋 *القنوات المطلوبة:*\n"
        for channel_id, channel_name in channels:
            response += f"- {channel_name} (ID: `{channel_id}`)\n"
        bot.send_message(message.chat.id, response, parse_mode="Markdown")
    else:
        bot.send_message(message.chat.id, "لا توجد قنوات مطلوبة حالياً.")

# --- معالجات الرسائل العامة ---

@bot.message_handler(content_types=['text'])
def handle_text_search(message):
    """يعرض خيارات البحث للمستخدم."""
    if message.text.startswith('/'): return
    query = message.text.strip()
    
    user_last_search[message.chat.id] = query
    
    categories = get_categories_tree()
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    keyboard.add(InlineKeyboardButton("بحث في كل التصنيفات", callback_data=f"search_scope::all"))
    
    for cat_id, cat_name, parent_id, full_path in categories:
        keyboard.add(InlineKeyboardButton(f"بحث في: {cat_name}", callback_data=f"search_scope::{cat_id}"))
        
    bot.reply_to(message, f"أين تريد البحث عن '{query}'؟", reply_markup=keyboard)

@bot.message_handler(content_types=['video'])
def handle_new_video(message):
    if str(message.chat.id) == CHANNEL_ID:
        active_category_id = get_active_category_id()
        print(f"New video detected. Assigning to active category ID: {active_category_id}. Message ID: {message.message_id}")
        success = add_video(
            message_id=message.message_id,
            caption=message.caption,
            chat_id=message.chat.id,
            file_name=message.video.file_name if message.video else "",
            category_id=active_category_id
        )
        if success:
            print("Video added successfully with metadata extraction.")
        else:
            print("Failed to add video.")

# --- معالج ضغطات الأزرار ---

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    """الاستجابة عند الضغط على الأزرار."""
    try:
        data = call.data.split(CALLBACK_DELIMITER)
        action = data[0]

        if action == "admin":
            sub_action = data[1]
            bot.answer_callback_query(call.id)
            
            if sub_action == "broadcast":
                msg = bot.send_message(call.message.chat.id, "أرسل الرسالة التي تريد بثها لجميع المشتركين (نص، صورة، أو فيديو). (أو أرسل /cancel للإلغاء)")
                bot.register_next_step_handler(msg, handle_rich_broadcast)

            elif sub_action == "sub_count":
                count = get_subscriber_count()
                bot.send_message(call.message.chat.id, f"👤 إجمالي عدد المشتركين في البوت: *{count}*", parse_mode='Markdown')
            
            elif sub_action == "stats":
                stats = get_bot_stats()
                popular = get_popular_videos()
                
                stats_text = f"📊 *إحصائيات المحتوى*\n\n"
                stats_text += f"- إجمالي الفيديوهات: *{stats['video_count']}*\n"
                stats_text += f"- إجمالي التصنيفات: *{stats['category_count']}*\n"
                stats_text += f"- إجمالي المشاهدات: *{stats['total_views']}*\n"
                stats_text += f"- إجمالي التقييمات: *{stats['total_ratings']}*\n\n"
                
                if popular['most_viewed']:
                    most_viewed = popular['most_viewed'][0]
                    stats_text += f"🔥 الأكثر مشاهدة: {most_viewed[2] or most_viewed[4] or 'فيديو'} ({most_viewed[7]} مشاهدة)\n"
                
                if popular['highest_rated']:
                    highest_rated = popular['highest_rated'][0]
                    stats_text += f"⭐ الأعلى تقييماً: {highest_rated[2] or highest_rated[4] or 'فيديو'} ({highest_rated[8]:.1f}/5)\n"
                
                bot.send_message(call.message.chat.id, stats_text, parse_mode='Markdown')
            
            elif sub_action == "add_new_cat":
                msg = bot.send_message(call.message.chat.id, "أرسل اسم التصنيف الجديد الذي تريد إنشاءه. (أو أرسل /cancel للإلغاء)")
                bot.register_next_step_handler(msg, handle_add_new_category)

            elif sub_action == "set_active":
                categories = get_categories_tree()
                if not categories:
                    bot.answer_callback_query(call.id, "لا توجد تصنيفات حالياً. قم بإنشاء واحد أولاً.")
                    return
                keyboard = InlineKeyboardMarkup(row_width=2)
                buttons = [InlineKeyboardButton(text=cat[1], callback_data=f"admin::setcat::{cat[0]}") for cat in categories]
                keyboard.add(*buttons)
                bot.edit_message_text("اختر التصنيف الذي تريد تفعيله:", call.message.chat.id, call.message.message_id, reply_markup=keyboard)

            elif sub_action == "setcat":
                category_id = int(data[2])
                if set_active_category_id(category_id):
                    category = get_category_by_id(category_id)
                    bot.edit_message_text(f"✅ تم تفعيل التصنيف '{category[1]}' بنجاح.", call.message.chat.id, call.message.message_id)
                
            elif sub_action == "help":
                help_text = "قائمة أوامر الإدارة:\n- إحصائيات البوت المتقدمة\n- تعيين التصنيف النشط\n- إضافة تصنيف جديد\n- إضافة قناة مطلوبة\n- إزالة قناة مطلوبة\n- عرض القنوات المطلوبة\n- معرف حسابي (/myid)\n- البث الغني (نص، صور، فيديوهات)\n- نظام التقييمات والإحصائيات"
                bot.send_message(call.message.chat.id, help_text)
                
            elif sub_action == "add_channel":
                msg = bot.send_message(call.message.chat.id, "أرسل معرف القناة (مثال: -1001234567890). (أو أرسل /cancel للإلغاء)")
                bot.register_next_step_handler(msg, handle_add_channel_step1)
                
            elif sub_action == "remove_channel":
                msg = bot.send_message(call.message.chat.id, "أرسل معرف القناة التي تريد إزالتها. (أو أرسل /cancel للإلغاء)")
                bot.register_next_step_handler(msg, handle_remove_channel_step)
                
            elif sub_action == "list_channels":
                handle_list_channels(call.message)
            return

        elif action == "popular":
            sub_action = data[1]
            popular = get_popular_videos()
            
            if sub_action == "most_viewed":
                videos = popular['most_viewed']
                if videos:
                    keyboard = create_paginated_keyboard(videos, len(videos), 0, "popular_page", "most_viewed")
                    bot.edit_message_text("📈 الفيديوهات الأكثر مشاهدة:", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                else:
                    bot.edit_message_text("لا توجد فيديوهات مشاهدة حالياً.", call.message.chat.id, call.message.message_id)
            
            elif sub_action == "highest_rated":
                videos = popular['highest_rated']
                if videos:
                    # تحويل البيانات لتتوافق مع create_paginated_keyboard
                    formatted_videos = []
                    for video in videos:
                        formatted_videos.append(video[:8])  # أخذ أول 8 عناصر فقط
                    keyboard = create_paginated_keyboard(formatted_videos, len(formatted_videos), 0, "popular_page", "highest_rated")
                    bot.edit_message_text("⭐ الفيديوهات الأعلى تقييماً:", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                else:
                    bot.edit_message_text("لا توجد فيديوهات مقيمة حالياً.", call.message.chat.id, call.message.message_id)
            
            bot.answer_callback_query(call.id)
            return

        elif action == "back_to_cats":
            list_videos(call.message, edit_message=call.message)
            bot.answer_callback_query(call.id)
            return

        elif action == "video":
            _, video_id, message_id, chat_id = data
            video_id = int(video_id)
            
            # زيادة عداد المشاهدات
            increment_video_view_count(video_id)
            
            # البحث عن الفيديو في قاعدة البيانات للحصول على metadata
            try:
                video = get_video_by_message_id(int(message_id))
                if video and video[4]:  # metadata موجود
                    metadata = json.loads(video[4]) if isinstance(video[4], str) else video[4]
                    qualities = metadata.get('qualities', [])
                    
                    if len(qualities) > 1:
                        # عرض خيارات الجودة
                        keyboard = InlineKeyboardMarkup()
                        for quality in qualities:
                            res = quality['resolution']
                            keyboard.add(InlineKeyboardButton(f"جودة {res}", callback_data=f"quality::{message_id}::{res}"))
                        
                        # إضافة أزرار التقييم
                        rating_keyboard = create_video_action_keyboard(video_id, call.from_user.id)
                        for row in rating_keyboard.keyboard:
                            keyboard.keyboard.append(row)
                        
                        bot.send_message(call.message.chat.id, "اختر الجودة المطلوبة:", reply_markup=keyboard)
                        bot.answer_callback_query(call.id)
                        return
                
                # إرسال الفيديو مباشرة إذا لم توجد خيارات جودة
                bot.copy_message(call.message.chat.id, chat_id, int(message_id))
                
                # إرسال أزرار التقييم
                rating_keyboard = create_video_action_keyboard(video_id, call.from_user.id)
                bot.send_message(call.message.chat.id, "قيم هذا الفيديو:", reply_markup=rating_keyboard)
                
                bot.answer_callback_query(call.id, "جاري إرسال الفيديو...")
                
            except Exception as e:
                print(f"Error handling video callback: {e}")
                bot.copy_message(call.message.chat.id, chat_id, int(message_id))
                bot.answer_callback_query(call.id, "جاري إرسال الفيديو...")
        
        elif action == "rate":
            _, video_id, rating = data
            video_id = int(video_id)
            rating = int(rating)
            
            if add_video_rating(video_id, call.from_user.id, rating):
                # تحديث لوحة المفاتيح لتظهر التقييم الجديد
                new_keyboard = create_video_action_keyboard(video_id, call.from_user.id)
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=new_keyboard)
                bot.answer_callback_query(call.id, f"تم تقييم الفيديو بـ {rating} نجوم!")
            else:
                bot.answer_callback_query(call.id, "حدث خطأ في التقييم.")
        
        elif action == "quality":
            _, original_message_id, resolution = data
            # هنا يجب أن نبحث عن message_id الخاص بالجودة المحددة
            # حاليًا سنرسل الفيديو الأصلي (يحتاج تحسين لاحقًا)
            try:
                video = get_video_by_message_id(int(original_message_id))
                if video:
                    bot.copy_message(call.message.chat.id, video[3], int(original_message_id))
                    bot.answer_callback_query(call.id, f"جاري إرسال الفيديو بجودة {resolution}...")
                else:
                    bot.answer_callback_query(call.id, "خطأ في العثور على الفيديو.")
            except Exception as e:
                print(f"Error handling quality callback: {e}")
                bot.answer_callback_query(call.id, "حدث خطأ.")
        
        elif action == "cat":
            _, category_id, page_str = data
            page = int(page_str)
            category_id = int(category_id)
            
            # التحقق من وجود تصنيفات فرعية
            child_categories = get_child_categories(category_id)
            if child_categories:
                # عرض التصنيفات الفرعية
                keyboard = create_categories_keyboard(category_id)
                category = get_category_by_id(category_id)
                bot.edit_message_text(f"التصنيفات الفرعية في '{category[1]}':", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
            else:
                # عرض الفيديوهات في هذا التصنيف
                videos, total_count = get_videos(category_id, page)
                if videos:
                    keyboard = create_paginated_keyboard(videos, total_count, page, "cat", category_id)
                    category = get_category_by_id(category_id)
                    bot.edit_message_text(f"الفيديوهات في فئة '{category[1]}':", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                else:
                    bot.edit_message_text("لا توجد فيديوهات في هذا التصنيف.", call.message.chat.id, call.message.message_id)
            
            bot.answer_callback_query(call.id)

        elif action == "search_scope":
            bot.answer_callback_query(call.id)
            query = user_last_search.get(call.message.chat.id)
            if not query:
                bot.edit_message_text("انتهت صلاحية البحث، يرجى البحث مرة أخرى.", call.message.chat.id, call.message.message_id)
                return

            scope = data[1]
            page = 0 
            
            if scope == "all":
                videos, total_count = search_videos(query, page=page)
                if not videos:
                    bot.edit_message_text(f"لم يتم العثور على نتائج للبحث الشامل عن '{query}'.", call.message.chat.id, call.message.message_id)
                    return
                keyboard = create_paginated_keyboard(videos, total_count, page, "search_all", "all")
                bot.edit_message_text(f"نتائج البحث الشامل عن '{query}':", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
            else: 
                category_id = int(scope)
                videos, total_count = search_videos(query, page=page, category_id=category_id)
                if not videos:
                    category = get_category_by_id(category_id)
                    bot.edit_message_text(f"لم يتم العثور على نتائج للبحث عن '{query}' في فئة '{category[1]}'.", call.message.chat.id, call.message.message_id)
                    return
                keyboard = create_paginated_keyboard(videos, total_count, page, "search_cat", category_id)
                category = get_category_by_id(category_id)
                bot.edit_message_text(f"نتائج البحث عن '{query}' في فئة '{category[1]}':", call.message.chat.id, call.message.message_id, reply_markup=keyboard)

        elif action == "search_all":
            _, context, page_str = data
            page = int(page_str)
            query = user_last_search.get(call.message.chat.id)
            if not query:
                bot.answer_callback_query(call.id, "انتهت صلاحية البحث، يرجى البحث مرة أخرى.")
                return
            videos, total_count = search_videos(query, page=page)
            keyboard = create_paginated_keyboard(videos, total_count, page, "search_all", "all")
            bot.edit_message_text(f"نتائج البحث الشامل عن '{query}':", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
            bot.answer_callback_query(call.id)

        elif action == "search_cat":
            _, category_id, page_str = data
            page = int(page_str)
            category_id = int(category_id)
            query = user_last_search.get(call.message.chat.id)
            if not query:
                bot.answer_callback_query(call.id, "انتهت صلاحية البحث، يرجى البحث مرة أخرى.")
                return
            videos, total_count = search_videos(query, page=page, category_id=category_id)
            keyboard = create_paginated_keyboard(videos, total_count, page, "search_cat", category_id)
            category = get_category_by_id(category_id)
            bot.edit_message_text(f"نتائج البحث عن '{query}' في فئة '{category[1]}':", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
            bot.answer_callback_query(call.id)
        
        elif action == "noop":
            bot.answer_callback_query(call.id)

    except Exception as e:
        print(f"Callback query error: {e}")
        bot.answer_callback_query(call.id, "حدث خطأ.")

# --- نقطة انطلاق البوت ---

if __name__ == "__main__":
    init_db()
    migrate_old_data()  # ترحيل البيانات القديمة إذا وجدت
    print("Enhanced bot with ratings is starting...")
    while True:
        try:
            bot.polling(non_stop=True)
        except Exception as e:
            print(f"An error occurred in the main loop: {e}")
            print("Restarting in 15 seconds...")
            time.sleep(15)

