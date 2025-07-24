# ==============================================================================
# ملف: db_manager.py (النسخة المحسنة مع نظام فحص وإصلاح تلقائي)
# الوصف: إدارة شاملة لقاعدة البيانات مع التحقق من سلامة الهيكل،
#        نظام تسجيل محسن، ومعالجة قوية للأخطاء.
# ==============================================================================

import psycopg2
from psycopg2 import sql
from psycopg2.extras import DictCursor
import os
from urllib.parse import urlparse
import logging
import json
from datetime import datetime

# --- إعداد نظام التسجيل (Logging) ---
# سيقوم بتسجيل الأحداث في ملف bot.log وفي الطرفية
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- إعدادات قاعدة البيانات (قراءة آمنة من متغيرات البيئة) ---
try:
    DATABASE_URL = os.environ.get('DATABASE_URL')
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable not set.")
        
    result = urlparse(DATABASE_URL)
    DB_CONFIG = {
        'user': result.username,
        'password': result.password,
        'host': result.hostname,
        'port': result.port,
        'dbname': result.path[1:]
    }
except Exception as e:
    logger.critical(f"FATAL: Could not parse DATABASE_URL. Error: {e}")
    # في حالة عدم وجود رابط قاعدة البيانات، لا يمكن للبوت العمل
    exit()

# --- متغيرات عامة ---
VIDEOS_PER_PAGE = 10
CALLBACK_DELIMITER = "::"
admin_steps = {}
user_last_search = {}

# --- المخطط المرجعي لقاعدة البيانات (Source of Truth) ---
# هذا التعريف يمثل الهيكل المثالي للجداول والأعمدة.
# سيتم استخدامه لفحص وإصلاح قاعدة البيانات تلقائياً.
EXPECTED_SCHEMA = {
    "categories": {
        "columns": {
            "id": "SERIAL PRIMARY KEY",
            "name": "TEXT NOT NULL",
            "parent_id": "INTEGER REFERENCES categories(id) ON DELETE CASCADE",
            "full_path": "TEXT NOT NULL UNIQUE"
        }
    },
    "video_archive": {
        "columns": {
            "id": "SERIAL PRIMARY KEY",
            "message_id": "BIGINT UNIQUE",
            "caption": "TEXT",
            "chat_id": "BIGINT",
            "file_name": "TEXT",
            "category_id": "INTEGER REFERENCES categories(id) ON DELETE SET NULL",
            "metadata": "JSONB DEFAULT '{}'::jsonb",
            "view_count": "INTEGER DEFAULT 0",
            "file_id": "TEXT"
        }
    },
    "video_ratings": {
        "columns": {
            "id": "SERIAL PRIMARY KEY",
            "video_id": "INTEGER REFERENCES video_archive(id) ON DELETE CASCADE",
            "user_id": "BIGINT NOT NULL",
            "rating": "INTEGER CHECK (rating >= 1 AND rating <= 5)",
            "created_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        },
        "constraints": {
            "video_ratings_video_id_user_id_key": "UNIQUE (video_id, user_id)"
        }
    },
    "bot_settings": {
        "columns": {
            "setting_key": "TEXT PRIMARY KEY",
            "setting_value": "TEXT"
        }
    },
    "bot_users": {
        "columns": {
            "user_id": "BIGINT PRIMARY KEY",
            "username": "TEXT",
            "first_name": "TEXT",
            "join_date": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        }
    },
    "required_channels": {
        "columns": {
            "channel_id": "BIGINT PRIMARY KEY",
            "channel_name": "TEXT"
        }
    }
}

def get_db_connection():
    """إنشاء وإرجاع اتصال بقاعدة البيانات."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        return conn
    except psycopg2.OperationalError as e:
        logger.error(f"Database connection failed: {e}")
        return None

def verify_and_repair_schema():
    """
    يفحص هيكل قاعدة البيانات الحالي ويقارنه بالهيكل المرجعي (EXPECTED_SCHEMA).
    يقوم بإصلاح المشاكل تلقائياً مثل إنشاء الجداول أو إضافة الأعمدة المفقودة.
    """
    logger.info("Starting database schema verification and repair process...")
    conn = get_db_connection()
    if not conn:
        logger.critical("Cannot verify schema without a database connection.")
        return

    try:
        with conn.cursor() as c:
            for table_name, schema in EXPECTED_SCHEMA.items():
                # 1. التحقق من وجود الجدول
                c.execute("SELECT to_regclass(%s)", (table_name,))
                if c.fetchone()[0] is None:
                    logger.warning(f"Table '{table_name}' not found. Creating it now.")
                    
                    # إنشاء استعلام CREATE TABLE
                    columns_sql = ", ".join([f'"{col_name}" {col_type}' for col_name, col_type in schema["columns"].items()])
                    constraints_sql = ""
                    if "constraints" in schema:
                        constraints_sql = ", " + ", ".join([f'CONSTRAINT "{con_name}" {con_def}' for con_name, con_def in schema["constraints"].items()])
                    
                    create_sql = sql.SQL("CREATE TABLE {table} ({columns}{constraints})").format(
                        table=sql.Identifier(table_name),
                        columns=sql.SQL(columns_sql),
                        constraints=sql.SQL(constraints_sql)
                    )
                    c.execute(create_sql)
                    logger.info(f"Table '{table_name}' created successfully.")
                else:
                    # 2. إذا كان الجدول موجوداً، تحقق من وجود الأعمدة
                    logger.info(f"Table '{table_name}' exists. Verifying columns...")
                    for col_name, col_type in schema["columns"].items():
                        c.execute("""
                            SELECT 1 FROM information_schema.columns 
                            WHERE table_name = %s AND column_name = %s
                        """, (table_name, col_name))
                        
                        if c.fetchone() is None:
                            logger.warning(f"Column '{col_name}' not found in table '{table_name}'. Adding it now.")
                            # تجاهل القيود مثل PRIMARY KEY عند إضافة عمود
                            base_col_type = col_type.split(" ")[0]
                            alter_sql = sql.SQL("ALTER TABLE {table} ADD COLUMN {column} {type}").format(
                                table=sql.Identifier(table_name),
                                column=sql.Identifier(col_name),
                                type=sql.SQL(col_type) # Use full type for defaults etc.
                            )
                            c.execute(alter_sql)
                            logger.info(f"Column '{col_name}' added to table '{table_name}'.")
                        
            conn.commit()
            logger.info("✅ Database schema verification and repair completed successfully.")
    except Exception as e:
        conn.rollback()
        logger.error(f"An error occurred during schema verification: {e}", exc_info=True)
    finally:
        if conn:
            conn.close()

# --- وظائف إدارة البيانات (مع معالجة أخطاء محسنة) ---

def execute_query(query, params=None, fetch=None):
    """دالة مركزية لتنفيذ الاستعلامات بأمان."""
    conn = get_db_connection()
    if not conn: return None
    
    try:
        with conn.cursor(cursor_factory=DictCursor) as c:
            c.execute(query, params)
            if fetch == "one":
                result = c.fetchone()
            elif fetch == "all":
                result = c.fetchall()
            else:
                result = None # لعمليات INSERT, UPDATE, DELETE
            conn.commit()
            return result
    except Exception as e:
        logger.error(f"Database query failed. Query: {query}, Params: {params}, Error: {e}", exc_info=True)
        if conn: conn.rollback()
        return None
    finally:
        if conn: conn.close()

def add_category(name, parent_id=None):
    """إضافة تصنيف جديد مع حساب المسار الكامل."""
    parent_path = ""
    if parent_id:
        parent = get_category_by_id(parent_id)
        if not parent:
            return False, "Parent category not found."
        parent_path = parent['full_path']

    full_path = f"{parent_path}{name}/"
    
    query = "INSERT INTO categories (name, parent_id, full_path) VALUES (%s, %s, %s) ON CONFLICT (full_path) DO NOTHING RETURNING id"
    result = execute_query(query, (name, parent_id, full_path), fetch="one")
    
    if result:
        return True, result['id']
    else:
        # قد يكون السبب أن التصنيف موجود بالفعل
        existing = execute_query("SELECT id FROM categories WHERE full_path = %s", (full_path,), fetch="one")
        if existing:
            return False, f"Category '{name}' already exists with this path."
        return False, "Failed to create category."

def get_category_by_id(category_id):
    return execute_query("SELECT * FROM categories WHERE id = %s", (category_id,), fetch="one")

def get_child_categories(parent_id=None):
    if parent_id is None:
        query = "SELECT id, name, parent_id, full_path FROM categories WHERE parent_id IS NULL ORDER BY name"
        return execute_query(query, fetch="all")
    else:
        query = "SELECT id, name, parent_id, full_path FROM categories WHERE parent_id = %s ORDER BY name"
        return execute_query(query, (parent_id,), fetch="all")

def get_categories_tree():
    return execute_query("SELECT id, name, parent_id, full_path FROM categories ORDER BY full_path", fetch="all")

def add_video(message_id, caption, chat_id, file_name, category_id, file_id, video_info=None):
    metadata = video_info if video_info else {}
    query = """
        INSERT INTO video_archive (message_id, caption, chat_id, file_name, category_id, file_id, metadata)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (message_id) DO UPDATE SET
        caption = EXCLUDED.caption,
        category_id = EXCLUDED.category_id,
        metadata = EXCLUDED.metadata
    """
    execute_query(query, (message_id, caption, chat_id, file_name, category_id, file_id, json.dumps(metadata)))
    return True

def get_videos(category_id, page=0):
    offset = page * VIDEOS_PER_PAGE
    query = """
        SELECT v.*, COALESCE(r.avg_rating, 0) as avg_rating, COALESCE(r.total_ratings, 0) as total_ratings
        FROM video_archive v
        LEFT JOIN (
            SELECT video_id, AVG(rating) as avg_rating, COUNT(id) as total_ratings
            FROM video_ratings
            GROUP BY video_id
        ) r ON v.id = r.video_id
        WHERE v.category_id = %s
        ORDER BY v.message_id DESC
        LIMIT %s OFFSET %s
    """
    videos = execute_query(query, (category_id, VIDEOS_PER_PAGE, offset), fetch="all")
    
    count_query = "SELECT COUNT(*) FROM video_archive WHERE category_id = %s"
    total_count = execute_query(count_query, (category_id,), fetch="one")[0]
    
    return videos, total_count

def search_videos(search_query, page=0, category_id=None):
    offset = page * VIDEOS_PER_PAGE
    search_term = f"%{search_query}%"
    
    base_query = """
        FROM video_archive v
        LEFT JOIN (
            SELECT video_id, AVG(rating) as avg_rating, COUNT(id) as total_ratings
            FROM video_ratings
            GROUP BY video_id
        ) r ON v.id = r.video_id
        WHERE v.caption ILIKE %s
    """
    params = [search_term]
    
    if category_id:
        base_query += " AND v.category_id = %s"
        params.append(category_id)
        
    videos_query = "SELECT v.*, COALESCE(r.avg_rating, 0) as avg_rating, COALESCE(r.total_ratings, 0) as total_ratings " + base_query + " ORDER BY v.message_id DESC LIMIT %s OFFSET %s"
    count_query = "SELECT COUNT(*) " + base_query

    videos = execute_query(videos_query, params + [VIDEOS_PER_PAGE, offset], fetch="all")
    total_count = execute_query(count_query, params, fetch="one")[0]
    
    return videos, total_count

def increment_video_view_count(video_id):
    execute_query("UPDATE video_archive SET view_count = view_count + 1 WHERE id = %s", (video_id,))

def get_video_by_message_id(message_id):
    return execute_query("SELECT * FROM video_archive WHERE message_id = %s", (message_id,), fetch="one")

def get_active_category_id():
    result = execute_query("SELECT setting_value FROM bot_settings WHERE setting_key = 'active_category_id'", fetch="one")
    return int(result[0]) if result else None

def set_active_category_id(category_id):
    query = """
        INSERT INTO bot_settings (setting_key, setting_value) VALUES ('active_category_id', %s)
        ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value
    """
    execute_query(query, (str(category_id),))
    return True

# --- دوال التقييمات والإحصائيات ---

def add_video_rating(video_id, user_id, rating):
    query = """
        INSERT INTO video_ratings (video_id, user_id, rating) VALUES (%s, %s, %s)
        ON CONFLICT (video_id, user_id) DO UPDATE SET rating = EXCLUDED.rating, created_at = CURRENT_TIMESTAMP
    """
    execute_query(query, (video_id, user_id, rating))
    return True

def get_video_rating_stats(video_id):
    query = "SELECT AVG(rating) as avg, COUNT(id) as count FROM video_ratings WHERE video_id = %s"
    return execute_query(query, (video_id,), fetch="one")

def get_user_video_rating(video_id, user_id):
    result = execute_query("SELECT rating FROM video_ratings WHERE video_id = %s AND user_id = %s", (video_id, user_id), fetch="one")
    return result[0] if result else None

def get_popular_videos():
    """
    دالة محسنة لجلب الفيديوهات الشائعة مع شروط منطقية أكثر.
    """
    # جلب الفيديوهات التي تمت مشاهدتها مرة واحدة على الأقل
    most_viewed_query = """
        SELECT v.*, COALESCE(r.avg_rating, 0) as avg_rating, COALESCE(r.total_ratings, 0) as total_ratings
        FROM video_archive v
        LEFT JOIN (
            SELECT video_id, AVG(rating) as avg_rating, COUNT(id) as total_ratings
            FROM video_ratings GROUP BY video_id
        ) r ON v.id = r.video_id
        WHERE v.view_count > 0
        ORDER BY v.view_count DESC NULLS LAST, v.id DESC LIMIT 10
    """
    
    # جلب الفيديوهات التي تم تقييمها مرة واحدة على الأقل
    highest_rated_query = """
        SELECT v.*, r.avg_rating, r.total_ratings
        FROM video_archive v
        JOIN (
            SELECT video_id, AVG(rating) as avg_rating, COUNT(id) as total_ratings
            FROM video_ratings GROUP BY video_id
        ) r ON v.id = r.video_id
        WHERE r.total_ratings >= 1
        ORDER BY r.avg_rating DESC, r.total_ratings DESC, v.id DESC LIMIT 10
    """
    return {
        "most_viewed": execute_query(most_viewed_query, fetch="all"),
        "highest_rated": execute_query(highest_rated_query, fetch="all")
    }

# --- دوال المستخدمين والإعدادات ---

def add_bot_user(user_id, username, first_name):
    query = "INSERT INTO bot_users (user_id, username, first_name) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING"
    execute_query(query, (user_id, username, first_name))

def get_all_user_ids():
    results = execute_query("SELECT user_id FROM bot_users", fetch="all")
    return [row[0] for row in results] if results else []

def get_subscriber_count():
    result = execute_query("SELECT COUNT(*) FROM bot_users", fetch="one")
    return result[0] if result else 0

def get_bot_stats():
    stats = {
        "video_count": 0, "category_count": 0,
        "total_views": 0, "total_ratings": 0
    }
    stats["video_count"] = execute_query("SELECT COUNT(*) FROM video_archive", fetch="one")[0] or 0
    stats["category_count"] = execute_query("SELECT COUNT(*) FROM categories", fetch="one")[0] or 0
    stats["total_views"] = execute_query("SELECT SUM(view_count) FROM video_archive", fetch="one")[0] or 0
    stats["total_ratings"] = execute_query("SELECT COUNT(*) FROM video_ratings", fetch="one")[0] or 0
    return stats

# --- دوال القنوات المطلوبة ---

def add_required_channel(channel_id, channel_name):
    query = "INSERT INTO required_channels (channel_id, channel_name) VALUES (%s, %s) ON CONFLICT (channel_id) DO UPDATE SET channel_name = EXCLUDED.channel_name"
    execute_query(query, (channel_id, channel_name))
    return True

def remove_required_channel(channel_id):
    query = "DELETE FROM required_channels WHERE channel_id = %s"
    execute_query(query, (channel_id,))
    return True

def get_required_channels():
    return execute_query("SELECT channel_id, channel_name FROM required_channels", fetch="all")
