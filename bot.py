import telebot
import psycopg2
import os
import json
import re
from urllib.parse import urlparse
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
import math
from datetime import datetime

# --- الإعدادات الأساسية (قراءة آمنة من متغيرات البيئة) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_IDS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id]

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

# قواميس مؤقتة لتخزين بيانات العمليات
admin_steps = {}
user_last_search = {}
VIDEOS_PER_PAGE = 10
CALLBACK_DELIMITER = "::"

# --- دوال قاعدة البيانات ---

def init_db():
    """إنشاء الجداول اللازمة إذا لم تكن موجودة."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
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
                video_id INTEGER REFERENCES video_archive(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                rating INTEGER CHECK (rating >= 1 AND rating <= 5),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(video_id, user_id)
            )
        """)
        
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
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        print("Starting enhanced migration process...")
        
        try:
            c.execute("ALTER TABLE video_archive ADD COLUMN IF NOT EXISTS category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL")
            print("✓ Added category_id column if not exists")
        except Exception as e:
            print(f"Error adding category_id column: {e}")
        
        try:
            c.execute("ALTER TABLE video_archive ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb")
            print("✓ Added metadata column if not exists")
        except Exception as e:
            print(f"Error adding metadata column: {e}")
        
        try:
            c.execute("ALTER TABLE video_archive ADD COLUMN IF NOT EXISTS view_count INTEGER DEFAULT 0")
            print("✓ Added view_count column if not exists")
        except Exception as e:
            print(f"Error adding view_count column: {e}")

        try:
            c.execute("ALTER TABLE video_archive ADD COLUMN IF NOT EXISTS file_id TEXT")
            print("✓ Added file_id column if not exists")
        except Exception as e:
            print(f"Error adding file_id column: {e}")

        default_category_name = "Uncategorized"
        full_path = f"/{default_category_name}/"
        try:
            c.execute("""
                INSERT INTO categories (name, parent_id, full_path)
                VALUES (%s, NULL, %s)
                ON CONFLICT (full_path) DO NOTHING
                RETURNING id
            """, (default_category_name, full_path))
            
            result = c.fetchone()
            if result:
                default_category_id = result[0]
                print(f"✓ Created default category with ID: {default_category_id}")
            else:
                c.execute("SELECT id FROM categories WHERE full_path = %s", (full_path,))
                default_category_id = c.fetchone()[0]
                print(f"✓ Found existing default category with ID: {default_category_id}")
        except Exception as e:
            print(f"Error creating default category: {e}")
            conn.rollback()
            return

        c.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'video_archive' AND column_name = 'category'
        """)
        old_category_exists = c.fetchone()

        if old_category_exists:
            print("Found old category column in video_archive, starting migration...")
            
            c.execute("SELECT DISTINCT category FROM video_archive WHERE category IS NOT NULL AND category != ''")
            old_categories = [row[0] for row in c.fetchall()]
            print(f"Found {len(old_categories)} unique categories to migrate: {old_categories}")

            for cat_name in old_categories:
                full_path = f"/{cat_name}/"
                try:
                    c.execute("""
                        INSERT INTO categories (name, parent_id, full_path)
                        VALUES (%s, NULL, %s)
                        ON CONFLICT (full_path) DO NOTHING
                        RETURNING id
                    """, (cat_name, full_path))
                    result = c.fetchone()
                    if result:
                        print(f"✓ Created category: {cat_name} with ID: {result[0]}")
                    else:
                        print(f"✓ Category already exists: {cat_name}")
                except Exception as e:
                    print(f"✗ Error creating category {cat_name}: {e}")

            try:
                c.execute("""
                    UPDATE video_archive 
                    SET category_id = categories.id
                    FROM categories
                    WHERE video_archive.category = categories.name
                    AND video_archive.category_id IS NULL
                """)
                
                c.execute("SELECT COUNT(*) FROM video_archive WHERE category_id IS NOT NULL")
                migrated_count = c.fetchone()[0]
                print(f"✓ Migrated {migrated_count} videos to new category system")
            except Exception as e:
                print(f"✗ Error updating video_archive with category_id: {e}")

            try:
                c.execute("ALTER TABLE video_archive DROP COLUMN IF EXISTS category")
                print("✓ Successfully dropped old category column")
            except Exception as e:
                print(f"✗ Error dropping old category column: {e}")
        else:
            print("No old category column found in video_archive")
        
        c.execute("SELECT COUNT(*) FROM video_archive WHERE category_id IS NULL")
        orphaned_videos = c.fetchone()[0]
        if orphaned_videos > 0:
            print(f"Found {orphaned_videos} videos without category_id, assigning to default category...")
            try:
                c.execute("""
                    UPDATE video_archive 
                    SET category_id = %s 
                    WHERE category_id IS NULL
                """, (default_category_id,))
                print(f"✓ Assigned {orphaned_videos} orphaned videos to default category ID: {default_category_id}")
            except Exception as e:
                print(f"✗ Error assigning orphaned videos: {e}")

        try:
            c.execute("SELECT setting_value FROM bot_settings WHERE setting_key = 'active_category_id'")
            if not c.fetchone():
                c.execute("""
                    INSERT INTO bot_settings (setting_key, setting_value)
                    VALUES ('active_category_id', %s)
                    ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value
                """, (str(default_category_id),))
                print(f"✓ Set active_category_id to default category ID: {default_category_id}")
            else:
                print("✓ active_category_id already exists in bot_settings")
        except Exception as e:
            print(f"✗ Error setting active_category_id: {e}")

        conn.commit()
        print("✅ Migration completed successfully.")
        
    except Exception as e:
        print(f"✗ Migration error: {e}")
        if conn:
            conn.rollback()
            print("✓ Rolled back changes due to error")
    finally:
        if conn:
            conn.close()

def extract_video_metadata(caption, message_id):
    """استخلاص البيانات الوصفية من كابشن الفيديو مع ربط الجودة بـ message_id."""
    metadata = {"qualities": [], "is_translated": False, "is_dubbed": False}
    if not caption:
        return metadata

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
    
    for quality in found_qualities:
        metadata["qualities"].append({"resolution": quality, "message_id": message_id})

    if re.search(r"مترجم|sub|subbed|subtitle", caption, re.IGNORECASE):
        metadata["is_translated"] = True
    if re.search(r"مدبلج|dub|dubbed|arabic", caption, re.IGNORECASE):
        metadata["is_dubbed"] = True

    return metadata

def add_category(name, parent_id=None):
    """إضافة تصنيف جديد."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
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

def delete_category(category_id, target_category_id):
    """حذف تصنيف مع نقل الفيديوهات إلى تصنيف آخر."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        c.execute("SELECT COUNT(*) FROM video_archive WHERE category_id = %s", (category_id,))
        video_count = c.fetchone()[0]
        
        if video_count > 0:
            c.execute("""
                UPDATE video_archive 
                SET category_id = %s 
                WHERE category_id = %s
            """, (target_category_id, category_id))
        
        c.execute("DELETE FROM categories WHERE id = %s", (category_id,))
        conn.commit()
        conn.close()
        return True, f"تم حذف التصنيف ونقل {video_count} فيديو إلى التصنيف الجديد."
    except Exception as e:
        print(f"Delete category error: {e}")
        return False, str(e)

def add_video(message_id, caption, chat_id, file_name=None, category_id=None, file_id=None):
    """إضافة فيديو جديد إلى قاعدة البيانات."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        metadata = extract_video_metadata(caption, message_id)
        
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
        conn.close()
        return True
    except Exception as e:
        print(f"Add video error: {e}")
        return False

def delete_video(message_id):
    """حذف فيديو واحد بناءً على message_id."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("DELETE FROM video_archive WHERE message_id = %s", (message_id,))
        affected_rows = c.rowcount
        conn.commit()
        conn.close()
        return affected_rows > 0, "تم حذف الفيديو بنجاح." if affected_rows > 0 else "الفيديو غير موجود."
    except Exception as e:
        print(f"Delete video error: {e}")
        return False, str(e)

def get_videos(category_id=None, page=0):
    """استرداد الفيديوهات من قاعدة البيانات مع نظام الصفحات."""
    offset = page * VIDEOS_PER_PAGE
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        if category_id:
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
                c.execute("SELECT id FROM categories WHERE full_path = %s", ('/Uncategorized/',))
                category_id = c.fetchone()[0]
            
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

def get_videos_by_title_and_quality(title, quality):
    """البحث عن فيديو بنفس العنوان والجودة المحددة."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        def normalize_text(text):
            if not text:
                return ""
            text = text.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا')
            text = text.replace('ة', 'ه').replace('ى', 'ي')
            return text

        normalized_title = normalize_text(title)
        search_param = '%' + normalized_title + '%'
        
        c.execute("""
            SELECT va.id, va.message_id, va.caption, va.chat_id, va.file_name, va.metadata, 
                   COALESCE(cat.name, 'Uncategorized') as category_name, va.view_count
            FROM video_archive va
            LEFT JOIN categories cat ON va.category_id = cat.id
            WHERE (REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(va.caption, 'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ة', 'ه'), 'ى', 'ي') ILIKE %s
                   OR REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(va.file_name, 'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ة', 'ه'), 'ى', 'ي') ILIKE %s)
                   AND va.metadata->'qualities' @> %s::jsonb
            LIMIT 1
        """, (search_param, search_param, json.dumps([{"resolution": quality}])))
        
        video = c.fetchone()
        conn.close()
        return video
    except Exception as e:
        print(f"Get videos by title and quality error: {e}")
        return None

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
        
        c.execute("""
            SELECT va.id, va.message_id, va.caption, va.chat_id, va.file_name, va.metadata, 
                   COALESCE(cat.name, 'Uncategorized') as category_name, va.view_count
            FROM video_archive va
            LEFT JOIN categories cat ON va.category_id = cat.id
            ORDER BY va.view_count DESC
            LIMIT 10
        """)
        most_viewed = c.fetchall()
        
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
        
        indicators = []
        qualities = []
        if metadata:
            try:
                meta_dict = json.loads(metadata) if isinstance(metadata, str) else metadata
                if meta_dict.get('is_translated'):
                    indicators.append("مترجم")
                if meta_dict.get('is_dubbed'):
                    indicators.append("مدبلج")
                qualities = meta_dict.get('qualities', [])
            except:
                pass
        
        indicator_text = f" ({''.join(indicators)})" if indicators else ""
        button_text = f"{title[:35]}{indicator_text} - {category_name} 👁 {view_count}"
        
        callback_data = f"video::{video_id}::{message_id}::{chat_id}"
        if len(qualities) > 1:
            callback_data += f"::{title}"  # أضف العنوان لاستخدامه في البحث عن الجودة
        keyboard.add(InlineKeyboardButton(text=button_text, callback_data=callback_data))

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
        if parent_category and parent_category[2] is not None:
            keyboard.row(InlineKeyboardButton("⬅️ الرجوع", callback_data=f"cat::{parent_category[2]}::0"))
        else:
            keyboard.row(InlineKeyboardButton("⬅️ الرجوع للرئيسية", callback_data="back_to_cats"))
    
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
    list_button = KeyboardButton('🎬 عرض كل الفيديوهات')
    popular_button = KeyboardButton('🔥 الفيديوهات الشائعة')
    markup.add(list_button, popular_button)
    return markup

def search_videos(query, page=0):
    """البحث عن الفيديوهات بناءً على النص."""
    offset = page * VIDEOS_PER_PAGE
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        def normalize_text(text):
            if not text:
                return ""
            text = text.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا')
            text = text.replace('ة', 'ه').replace('ى', 'ي')
            return text

        normalized_query = normalize_text(query)
        search_param = '%' + normalized_query + '%'
        
        c.execute("""
            SELECT COUNT(*) 
            FROM video_archive va
            LEFT JOIN categories cat ON va.category_id = cat.id
            WHERE REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(va.caption, 'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ة', 'ه'), 'ى', 'ي') ILIKE %s
               OR REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(va.file_name, 'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ة', 'ه'), 'ى', 'ي') ILIKE %s
        """, (search_param, search_param))
        total_count = c.fetchone()[0]
        
        c.execute("""
            SELECT va.id, va.message_id, va.caption, va.chat_id, va.file_name, va.metadata, 
                   COALESCE(cat.name, 'Uncategorized') as category_name, va.view_count
            FROM video_archive va
            LEFT JOIN categories cat ON va.category_id = cat.id
            WHERE REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(va.caption, 'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ة', 'ه'), 'ى', 'ي') ILIKE %s
               OR REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(va.file_name, 'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ة', 'ه'), 'ى', 'ي') ILIKE %s
            ORDER BY va.id
            LIMIT %s OFFSET %s
        """, (search_param, search_param, VIDEOS_PER_PAGE, offset))
        
        videos = c.fetchall()
        conn.close()
        return videos, total_count
    except Exception as e:
        print(f"Search videos error: {e}")
        return [], 0

def check_cancel(message):
    """التحقق من إلغاء العملية."""
    if message.text == "/cancel":
        bot.reply_to(message, "تم إلغاء العملية.", reply_markup=main_menu())
        if message.from_user.id in admin_steps:
            del admin_steps[message.from_user.id]
        return True
    return False

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

@bot.message_handler(commands=["delete_video"])
@check_admin
def delete_video_command(message):
    msg = bot.reply_to(message, "أرسل معرف الرسالة (message_id) للفيديو الذي تريد حذفه. (أو أرسل /cancel للإلغاء)")
    bot.register_next_step_handler(msg, handle_delete_video)

def handle_delete_video(message):
    if check_cancel(message): return
    try:
        message_id = int(message.text.strip())
        success, result = delete_video(message_id)
        bot.reply_to(message, result)
    except ValueError:
        msg = bot.reply_to(message, "معرف الرسالة غير صالح. يرجى إرسال رقم صحيح. (أو أرسل /cancel للإلغاء)")
        bot.register_next_step_handler(msg, handle_delete_video)

@bot.message_handler(commands=["delete_category"])
@check_admin
def delete_category_command(message):
    categories = get_categories_tree()
    if not categories:
        bot.reply_to(message, "لا توجد تصنيفات للحذف.")
        return
    
    keyboard = InlineKeyboardMarkup(row_width=2)
    for cat_id, cat_name, _, _ in categories:
        keyboard.add(InlineKeyboardButton(cat_name, callback_data=f"delete_cat::{cat_id}"))
    keyboard.add(InlineKeyboardButton("إلغاء", callback_data="cancel_admin"))
    
    bot.reply_to(message, "اختر التصنيف الذي تريد حذفه:", reply_markup=keyboard)

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
    
    if keyboard.keyboard:
        text = "اختر تصنيفًا لعرض محتوياته:"
        if edit_message:
            bot.edit_message_text(text, edit_message.chat.id, edit_message.message_id, reply_markup=keyboard)
        else:
            bot.reply_to(message, text, reply_markup=keyboard)
    else:
        text = "لا توجد تصنيفات متاحة حاليًا."
        if edit_message:
            bot.edit_message_text(text, edit_message.chat.id, edit_message.message_id)
        else:
            bot.reply_to(message, text)

@bot.message_handler(content_types=['text'])
def handle_search(message):
    query = message.text.strip()
    if query in ['🎬 عرض كل الفيديوهات', '🔥 الفيديوهات الشائعة']:
        return
    
    results, total_count = search_videos(query)
    user_last_search[message.from_user.id] = {'query': query, 'category_id': None}
    
    if not results:
        bot.reply_to(message, "لم يتم العثور على فيديوهات مطابقة.")
        return
    
    keyboard = create_paginated_keyboard(results, total_count, 0, "search", query)
    bot.reply_to(message, f"نتائج البحث عن: {query}", reply_markup=keyboard)

@bot.message_handler(content_types=['video'])
def handle_forwarded_video(message):
    if message.forward_from_chat and str(message.forward_from_chat.id) == CHANNEL_ID:
        add_video(
            message_id=message.forward_from_message_id,
            caption=message.caption,
            chat_id=message.forward_from_chat.id,
            file_name=message.video.file_name,
            file_id=message.video.file_id
        )
        bot.reply_to(message, "تم إضافة الفيديو إلى الأرشيف بنجاح!")

@bot.callback_query_handler(func=lambda call: True)
def handle_callback_query(call):
    data = call.data.split(CALLBACK_DELIMITER)
    user_id = call.from_user.id
    
    if data[0] == "cat":
        category_id, page = map(int, data[1:3])
        category = get_category_by_id(category_id)
        if not category:
            bot.answer_callback_query(call.id, "التصنيف غير موجود!")
            return
        
        videos, total_count = get_videos(category_id, page)
        if videos:
            keyboard = create_paginated_keyboard(videos, total_count, page, "cat", category_id)
            bot.edit_message_text(
                f"الفيديوهات في: {category[1]}",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard
            )
        else:
            keyboard = create_categories_keyboard(category_id)
            bot.edit_message_text(
                f"لا توجد فيديوهات في: {category[1]}",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard
            )
    
    elif data[0] == "video":
        video_id, message_id, chat_id = map(int, data[1:4])
        increment_video_view_count(video_id)
        
        video = get_video_by_message_id(message_id)
        if not video:
            bot.answer_callback_query(call.id, "الفيديو غير موجود!")
            return
        
        qualities = []
        try:
            metadata = json.loads(video[4]) if isinstance(video[4], str) else video[4]
            qualities = metadata.get('qualities', [])
        except:
            pass
        
        if len(qualities) > 1 and len(data) > 4:
            title = data[4]
            keyboard = InlineKeyboardMarkup(row_width=3)
            for q in qualities:
                keyboard.add(InlineKeyboardButton(
                    q['resolution'],
                    callback_data=f"quality::{video_id}::{message_id}::{chat_id}::{q['resolution']}::{title}"
                ))
            bot.edit_message_text(
                "اختر الجودة المطلوبة:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard
            )
            return
        
        try:
            bot.forward_message(
                call.message.chat.id,
                chat_id,
                message_id
            )
            keyboard = create_video_action_keyboard(video_id, user_id)
            bot.send_message(
                call.message.chat.id,
                "قيّم الفيديو:",
                reply_markup=keyboard
            )
        except Exception as e:
            bot.answer_callback_query(call.id, f"خطأ في جلب الفيديو: {str(e)}")
    
    elif data[0] == "quality":
        video_id, message_id, chat_id, quality, title = data[1:6]
        video_id, message_id, chat_id = map(int, [video_id, message_id, chat_id])
        
        video = get_videos_by_title_and_quality(title, quality)
        if video:
            increment_video_view_count(video[0])
            try:
                bot.forward_message(
                    call.message.chat.id,
                    video[3],
                    video[1]
                )
                keyboard = create_video_action_keyboard(video[0], user_id)
                bot.send_message(
                    call.message.chat.id,
                    f"قيّم الفيديو ({quality}):",
                    reply_markup=keyboard
                )
            except Exception as e:
                bot.answer_callback_query(call.id, f"خطأ في جلب الفيديو: {str(e)}")
        else:
            bot.answer_callback_query(call.id, "الفيديو بهذه الجودة غير متوفر!")
    
    elif data[0] == "rate":
        video_id, rating = map(int, data[1:3])
        if add_video_rating(video_id, user_id, rating):
            keyboard = create_video_action_keyboard(video_id, user_id)
            bot.edit_message_reply_markup(
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard
            )
            bot.answer_callback_query(call.id, "تم تسجيل تقييمك!")
        else:
            bot.answer_callback_query(call.id, "خطأ في تسجيل التقييم!")
    
    elif data[0] == "search":
        query, page = data[1], int(data[2])
        results, total_count = search_videos(query, page)
        if results:
            keyboard = create_paginated_keyboard(results, total_count, page, "search", query)
            bot.edit_message_text(
                f"نتائج البحث عن: {query}",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard
            )
        else:
            bot.edit_message_text(
                f"لا توجد نتائج للبحث: {query}",
                call.message.chat.id,
                call.message.message_id
            )
    
    elif data[0] == "popular":
        mode = data[1]
        popular = get_popular_videos()
        videos = popular.get(mode, [])
        total_count = len(videos)
        
        if videos:
            keyboard = create_paginated_keyboard(videos, total_count, 0, f"popular_{mode}", mode)
            bot.edit_message_text(
                "الفيديوهات الأكثر شعبية:" if mode == "most_viewed" else "الفيديوهات الأعلى تقييماً:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard
            )
        else:
            bot.edit_message_text(
                "لا توجد فيديوهات متاحة.",
                call.message.chat.id,
                call.message.message_id
            )
    
    elif data[0] == "popular_most_viewed" or data[0] == "popular_highest_rated":
        mode = data[0].split("_")[1]
        page = int(data[2])
        popular = get_popular_videos()
        videos = popular.get(mode, [])
        total_count = len(videos)
        
        if videos:
            keyboard = create_paginated_keyboard(videos, total_count, page, f"popular_{mode}", mode)
            bot.edit_message_text(
                "الفيديوهات الأكثر شعبية:" if mode == "most_viewed" else "الفيديوهات الأعلى تقييماً:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard
            )
        else:
            bot.edit_message_text(
                "لا توجد فيديوهات متاحة.",
                call.message.chat.id,
                call.message.message_id
            )
    
    elif data[0] == "back_to_cats":
        list_videos(call.message, edit_message=call.message)
    
    elif data[0] == "delete_cat":
        category_id = int(data[1])
        categories = get_categories_tree()
        keyboard = InlineKeyboardMarkup(row_width=2)
        for cat_id, cat_name, _, _ in categories:
            if cat_id != category_id:
                keyboard.add(InlineKeyboardButton(cat_name, callback_data=f"transfer_cat::{category_id}::{cat_id}"))
        keyboard.add(InlineKeyboardButton("إلغاء", callback_data="cancel_admin"))
        
        bot.edit_message_text(
            "اختر التصنيف الذي ستنقل إليه الفيديوهات:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=keyboard
        )
    
    elif data[0] == "transfer_cat":
        category_id, target_category_id = map(int, data[1:3])
        success, result = delete_category(category_id, target_category_id)
        bot.edit_message_text(
            result,
            call.message.chat.id,
            call.message.message_id
        )
    
    elif data[0] == "cancel_admin":
        bot.edit_message_text(
            "تم إلغاء العملية.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=main_menu()
        )
        if user_id in admin_steps:
            del admin_steps[user_id]
    
    elif data[0] == "noop":
        bot.answer_callback_query(call.id)

# --- تشغيل البوت ---
if __name__ == "__main__":
    init_db()
    migrate_old_data()
    print("Bot is running...")
    bot.infinity_polling()
