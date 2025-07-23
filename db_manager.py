import psycopg2
import os
import json
from psycopg2.extras import Json

# إعدادات قاعدة البيانات
DB_CONFIG = {
    'dbname': os.getenv('DATABASE_NAME', 'bot_database'),
    'user': os.getenv('DATABASE_USER', 'postgres'),
    'password': os.getenv('DATABASE_PASSWORD', ''),
    'host': os.getenv('DATABASE_HOST', 'localhost'),
    'port': os.getenv('DATABASE_PORT', '5432')
}

# متغيرات عامة
admin_steps = {}
user_last_search = {}
VIDEOS_PER_PAGE = 5
CALLBACK_DELIMITER = '::'

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
                metadata JSONB DEFAULT '{}',
                view_count INTEGER DEFAULT 0,
                file_id TEXT
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
    conn = None
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        print("Starting enhanced migration process...")
        
        # إضافة الأعمدة الجديدة إذا لم تكن موجودة
        try:
            c.execute("ALTER TABLE video_archive ADD COLUMN IF NOT EXISTS category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL")
            print("✓ Added category_id column if not exists")
        except Exception as e:
            print(f"Error adding category_id column: {e}")
        
        try:
            c.execute("ALTER TABLE video_archive ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'")
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

        # إنشاء تصنيف افتراضي
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

        # التحقق من وجود عمود category القديم
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

def add_category(category_name, parent_id=None):
    """إضافة تصنيف جديد إلى قاعدة البيانات."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        full_path = f"/{category_name}/" if parent_id is None else f"{get_category_by_id(parent_id)[3]}{category_name}/"
        c.execute(
            "INSERT INTO categories (name, parent_id, full_path) VALUES (%s, %s, %s) RETURNING id",
            (category_name, parent_id, full_path)
        )
        category_id = c.fetchone()[0]
        conn.commit()
        conn.close()
        return True, category_id
    except Exception as e:
        print(f"Error adding category: {e}")
        return False, str(e)

def get_categories_tree():
    """استرجاع كل التصنيفات مع المسارات الكاملة."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT id, name, parent_id, full_path FROM categories")
        categories = c.fetchall()
        conn.close()
        return categories
    except Exception as e:
        print(f"Error fetching categories: {e}")
        return []

def get_child_categories(parent_id=None):
    """استرجاع التصنيفات الفرعية لتصنيف معين."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        if parent_id is None:
            c.execute("SELECT id, name, parent_id, full_path FROM categories WHERE parent_id IS NULL")
        else:
            c.execute("SELECT id, name, parent_id, full_path FROM categories WHERE parent_id = %s", (parent_id,))
        categories = c.fetchall()
        conn.close()
        return categories
    except Exception as e:
        print(f"Error fetching child categories: {e}")
        return []

def get_category_by_id(category_id):
    """استرجاع تصنيف بناءً على معرفه."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT id, name, parent_id, full_path FROM categories WHERE id = %s", (category_id,))
        category = c.fetchone()
        conn.close()
        return category
    except Exception as e:
        print(f"Error fetching category: {e}")
        return None

def add_video(message_id, caption, chat_id, file_name, category_id, file_id, video_info):
    """إضافة فيديو جديد إلى قاعدة البيانات."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        video_info_json = Json(video_info) if video_info else Json({})
        c.execute(
            """
            INSERT INTO video_archive (message_id, caption, chat_id, file_name, category_id, file_id, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (message_id, caption, chat_id, file_name, category_id, file_id, video_info_json)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Error adding video: {e}")
        return False

def get_videos(category_id, page=0):
    """استرجاع الفيديوهات في تصنيف معين مع التصفح."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        offset = page * VIDEOS_PER_PAGE
        c.execute(
            """
            SELECT id, message_id, caption, chat_id, metadata, view_count, 
                   (SELECT AVG(rating) FROM video_ratings WHERE video_id = video_archive.id),
                   (SELECT COUNT(*) FROM video_ratings WHERE video_id = video_archive.id)
            FROM video_archive 
            WHERE category_id = %s 
            LIMIT %s OFFSET %s
            """,
            (category_id, VIDEOS_PER_PAGE, offset)
        )
        videos = c.fetchall()
        c.execute("SELECT COUNT(*) FROM video_archive WHERE category_id = %s", (category_id,))
        total_count = c.fetchone()[0]
        conn.close()
        return videos, total_count
    except Exception as e:
        print(f"Error fetching videos: {e}")
        return [], 0

def increment_video_view_count(video_id):
    """زيادة عدد مشاهدات الفيديو."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("UPDATE video_archive SET view_count = view_count + 1 WHERE id = %s", (video_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error incrementing view count: {e}")

def get_video_by_message_id(message_id):
    """استرجاع فيديو بناءً على message_id."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute(
            """
            SELECT id, message_id, caption, chat_id, metadata, view_count, file_id
            FROM video_archive 
            WHERE message_id = %s
            """,
            (message_id,)
        )
        video = c.fetchone()
        conn.close()
        return video
    except Exception as e:
        print(f"Error fetching video: {e}")
        return None

def get_active_category_id():
    """استرجاع معرف التصنيف النشط."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT setting_value FROM bot_settings WHERE setting_key = 'active_category_id'")
        result = c.fetchone()
        conn.close()
        return int(result[0]) if result else None
    except Exception as e:
        print(f"Error fetching active category: {e}")
        return None

def set_active_category_id(category_id):
    """تعيين التصنيف النشط."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO bot_settings (setting_key, setting_value)
            VALUES ('active_category_id', %s)
            ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value
            """,
            (str(category_id),)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Error setting active category: {e}")
        return False

def add_video_rating(video_id, user_id, rating):
    """إضافة تقييم لفيديو."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO video_ratings (video_id, user_id, rating)
            VALUES (%s, %s, %s)
            ON CONFLICT (video_id, user_id) DO UPDATE SET rating = EXCLUDED.rating
            """,
            (video_id, user_id, rating)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Error adding rating: {e}")
        return False

def get_video_rating_stats(video_id):
    """استرجاع إحصائيات تقييم الفيديو."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT AVG(rating), COUNT(*) FROM video_ratings WHERE video_id = %s", (video_id,))
        stats = c.fetchone()
        conn.close()
        return stats
    except Exception as e:
        print(f"Error fetching rating stats: {e}")
        return None

def get_user_video_rating(video_id, user_id):
    """استرجاع تقييم المستخدم لفيديو معين."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT rating FROM video_ratings WHERE video_id = %s AND user_id = %s", (video_id, user_id))
        rating = c.fetchone()
        conn.close()
        return rating[0] if rating else None
    except Exception as e:
        print(f"Error fetching user rating: {e}")
        return None

def get_popular_videos():
    """استرجاع الفيديوهات الشائعة (الأكثر مشاهدة والأعلى تقييمًا)."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute(
            """
            SELECT id, message_id, caption, chat_id, metadata, view_count, file_id,
                   (SELECT AVG(rating) FROM video_ratings WHERE video_id = video_archive.id),
                   (SELECT COUNT(*) FROM video_ratings WHERE video_id = video_archive.id)
            FROM video_archive 
            ORDER BY view_count DESC LIMIT 5
            """
        )
        most_viewed = c.fetchall()
        c.execute(
            """
            SELECT id, message_id, caption, chat_id, metadata, view_count, file_id,
                   (SELECT AVG(rating) FROM video_ratings WHERE video_id = video_archive.id),
                   (SELECT COUNT(*) FROM video_ratings WHERE video_id = video_archive.id)
            FROM video_archive 
            WHERE (SELECT AVG(rating) FROM video_ratings WHERE video_id = video_archive.id) IS NOT NULL
            ORDER BY (SELECT AVG(rating) FROM video_ratings WHERE video_id = video_archive.id) DESC LIMIT 5
            """
        )
        highest_rated = c.fetchall()
        conn.close()
        return {"most_viewed": most_viewed, "highest_rated": highest_rated}
    except Exception as e:
        print(f"Error fetching popular videos: {e}")
        return {"most_viewed": [], "highest_rated": []}

def add_bot_user(user_id, username, first_name):
    """إضافة مستخدم جديد للبوت."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO bot_users (user_id, username, first_name)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id) DO NOTHING
            """,
            (user_id, username, first_name)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error adding bot user: {e}")

def get_all_user_ids():
    """استرجاع معرفات كل المستخدمين."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT user_id FROM bot_users")
        user_ids = [row[0] for row in c.fetchall()]
        conn.close()
        return user_ids
    except Exception as e:
        print(f"Error fetching user IDs: {e}")
        return []

def get_subscriber_count():
    """استرجاع عدد المشتركين."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM bot_users")
        count = c.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        print(f"Error fetching subscriber count: {e}")
        return 0

def get_bot_stats():
    """استرجاع إحصائيات البوت."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM video_archive")
        video_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM categories")
        category_count = c.fetchone()[0]
        c.execute("SELECT SUM(view_count) FROM video_archive")
        total_views = c.fetchone()[0] or 0
        c.execute("SELECT SUM((SELECT COUNT(*) FROM video_ratings WHERE video_id = video_archive.id)) FROM video_archive")
        total_ratings = c.fetchone()[0] or 0
        conn.close()
        return {
            "video_count": video_count,
            "category_count": category_count,
            "total_views": total_views,
            "total_ratings": total_ratings
        }
    except Exception as e:
        print(f"Error fetching bot stats: {e}")
        return {
            "video_count": 0,
            "category_count": 0,
            "total_views": 0,
            "total_ratings": 0
        }

def search_videos(query, page=0, category_id=None):
    """البحث عن فيديوهات بناءً على استعلام."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        offset = page * VIDEOS_PER_PAGE
        query = f"%{query}%"
        if category_id:
            c.execute(
                """
                SELECT id, message_id, caption, chat_id, metadata, view_count, 
                       (SELECT AVG(rating) FROM video_ratings WHERE video_id = video_archive.id),
                       (SELECT COUNT(*) FROM video_ratings WHERE video_id = video_archive.id)
                FROM video_archive 
                WHERE category_id = %s AND caption ILIKE %s 
                LIMIT %s OFFSET %s
                """,
                (category_id, query, VIDEOS_PER_PAGE, offset)
            )
            videos = c.fetchall()
            c.execute("SELECT COUNT(*) FROM video_archive WHERE category_id = %s AND caption ILIKE %s", (category_id, query))
        else:
            c.execute(
                """
                SELECT id, message_id, caption, chat_id, metadata, view_count,
                       (SELECT AVG(rating) FROM video_ratings WHERE video_id = video_archive.id),
                       (SELECT COUNT(*) FROM video_ratings WHERE video_id = video_archive.id)
                FROM video_archive 
                WHERE caption ILIKE %s 
                LIMIT %s OFFSET %s
                """,
                (query, VIDEOS_PER_PAGE, offset)
            )
            videos = c.fetchall()
            c.execute("SELECT COUNT(*) FROM video_archive WHERE caption ILIKE %s", (query,))
        total_count = c.fetchone()[0]
        conn.close()
        return videos, total_count
    except Exception as e:
        print(f"Error searching videos: {e}")
        return [], 0

def add_required_channel(channel_id, channel_name):
    """إضافة قناة مطلوبة."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO required_channels (channel_id, channel_name)
            VALUES (%s, %s)
            ON CONFLICT (channel_id) DO UPDATE SET channel_name = EXCLUDED.channel_name
            """,
            (channel_id, channel_name)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Error adding required channel: {e}")
        return False

def remove_required_channel(channel_id):
    """إزالة قناة مطلوبة."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("DELETE FROM required_channels WHERE channel_id = %s", (channel_id,))
        affected = c.rowcount
        conn.commit()
        conn.close()
        return affected > 0
    except Exception as e:
        print(f"Error removing required channel: {e}")
        return False

def get_required_channels():
    """استرجاع القنوات المطلوبة."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT channel_id, channel_name FROM required_channels")
        channels = c.fetchall()
        conn.close()
        return channels
    except Exception as e:
        print(f"Error fetching required channels: {e}")
        return []
