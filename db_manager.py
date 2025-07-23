



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
                metadata JSONB DEFAULT \'{}'::jsonb,
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
            c.execute("ALTER TABLE video_archive ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT \'{}'::jsonb")
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
            WHERE table_name = \'video_archive\' AND column_name = \'category\'
        """)
        old_category_exists = c.fetchone()

        if old_category_exists:
            print("Found old category column in video_archive, starting migration...")
            
            # الحصول على جميع التصنيفات الفريدة من النظام القديم
            c.execute("SELECT DISTINCT category FROM video_archive WHERE category IS NOT NULL AND category != \'\'")
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
            c.execute("SELECT setting_value FROM bot_settings WHERE setting_key = \'active_category_id\'")
            if not c.fetchone():
                c.execute("""
                    INSERT INTO bot_settings (setting_key, setting_value)
                    VALUES (\'active_category_id\', %s)
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


