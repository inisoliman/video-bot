import psycopg2
import os
import json
from urllib.parse import urlparse

# --- الإعدادات الأساسية (قراءة آمنة من متغيرات البيئة) ---
DATABASE_URL = os.getenv("DATABASE_URL")

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
    """ترحيل البيانات القديمة من النظام القديم إلى النظام الجديد - نسخة محسنة."""
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

        # إنشاء تصنيف افتراضي في بداية الترحيل لضمان وجوده
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
                # إذا كان التصنيف موجود بالفعل، احصل على معرفه
                c.execute("SELECT id FROM categories WHERE full_path = %s", (full_path,))
                default_category_id = c.fetchone()[0]
                print(f"✓ Found existing default category with ID: {default_category_id}")
        except Exception as e:
            print(f"Error creating default category: {e}")
            # في حالة فشل إنشاء التصنيف الافتراضي، توقف عن الترحيل
            conn.rollback()
            return

        # التحقق من وجود عمود category القديم في video_archive
        c.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'video_archive' AND column_name = 'category'
        """)
        old_category_exists = c.fetchone()

        if old_category_exists:
            print("Found old category column in video_archive, starting migration...")
            
            # الحصول على جميع التصنيفات الفريدة من النظام القديم
            c.execute("SELECT DISTINCT category FROM video_archive WHERE category IS NOT NULL AND category != ''")
            old_categories = [row[0] for row in c.fetchall()]
            print(f"Found {len(old_categories)} unique categories to migrate: {old_categories}")

            # إنشاء التصنيفات في الجدول الجديد مع معالجة الأخطاء لكل تصنيف
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
                    # لا نتوقف، نستمر مع التصنيفات الأخرى

            # تحديث جدول video_archive لاستخدام category_id
            try:
                c.execute("""
                    UPDATE video_archive 
                    SET category_id = categories.id
                    FROM categories
                    WHERE video_archive.category = categories.name
                    AND video_archive.category_id IS NULL
                """
                )
                
                # التحقق من عدد الفيديوهات التي تم ترحيلها
                c.execute("SELECT COUNT(*) FROM video_archive WHERE category_id IS NOT NULL")
                migrated_count = c.fetchone()[0]
                print(f"✓ Migrated {migrated_count} videos to new category system")
            except Exception as e:
                print(f"✗ Error updating video_archive with category_id: {e}")

            # حذف عمود category القديم بعد الترحيل
            try:
                c.execute("ALTER TABLE video_archive DROP COLUMN IF EXISTS category")
                print("✓ Successfully dropped old category column")
            except Exception as e:
                print(f"✗ Error dropping old category column: {e}")
        else:
            print("No old category column found in video_archive")
        
        # التحقق من الفيديوهات بدون category_id وتعيين التصنيف الافتراضي
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

        # التأكد من وجود active_category_id في bot_settings
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

def add_video(message_id, caption, chat_id, file_name=None, category_id=None, file_id=None, video_info=None):
    """إضافة فيديو جديد إلى قاعدة البيانات."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        # استخلاص البيانات الوصفية من الكابشن
        metadata = {} #extract_video_metadata(caption) # سيتم استخلاصها في utils
        
        # إضافة معلومات الفيديو المستخلصة من MediaInfo إذا كانت متوفرة
        if video_info:
            metadata["video_details"] = video_info
        
        # إذا لم يتم تحديد category_id، استخدم التصنيف النشط
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

def get_videos(category_id=None, page=0, VIDEOS_PER_PAGE=10):
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
                c.execute("SELECT id FROM categories WHERE full_path = %s", ('/Uncategorized/',))
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

def search_videos(query, page=0, category_id=None, VIDEOS_PER_PAGE=10):
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
