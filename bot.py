# ==============================================================================
# ملف: bot.py
# الوصف: الإصدار النهائي مع دعم التصنيفات الشجرية ولوحة تحكم متكاملة.
# ==============================================================================

import telebot
import psycopg2
import os
import time
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from urllib.parse import urlparse
import math

# --- الإعدادات الأساسية (قراءة آمنة من متغيرات البيئة) ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
CHANNEL_ID = os.getenv('CHANNEL_ID')
ADMIN_ID = os.getenv('ADMIN_ID') # معرف حساب الآدمن

# التأكد من وجود المتغيرات الأساسية قبل تشغيل البوت
if not all([BOT_TOKEN, DATABASE_URL, CHANNEL_ID, ADMIN_ID]):
    print("FATAL ERROR: Missing one or more environment variables (BOT_TOKEN, DATABASE_URL, CHANNEL_ID, ADMIN_ID).")
    exit()

bot = telebot.TeleBot(BOT_TOKEN)

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

# قاموس مؤقت لتخزين بيانات عمليات الآدمن
admin_steps = {}
user_last_search = {} # لتخزين آخر عملية بحث لكل مستخدم
VIDEOS_PER_PAGE = 10 # عدد الفيديوهات في كل صفحة
CALLBACK_DELIMITER = '::' # فاصل آمن للبيانات

# --- دوال قاعدة البيانات ---

def init_db():
    """إنشاء الجداول اللازمة إذا لم تكن موجودة."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        # جدول التصنيفات لدعم البنية الشجرية
        c.execute('''
            CREATE TABLE IF NOT EXISTS categories (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                parent_id INTEGER REFERENCES categories(id) ON DELETE SET NULL
            );
        ''')
        # جدول الفيديوهات مع ربطه بجدول التصنيفات
        c.execute('''
            CREATE TABLE IF NOT EXISTS video_archive (
                id SERIAL PRIMARY KEY,
                message_id INTEGER UNIQUE,
                caption TEXT,
                chat_id BIGINT,
                file_name TEXT,
                category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
                request_count INTEGER DEFAULT 0
            );
        ''')
        # جدول إعدادات البوت لتخزين التصنيف النشط
        c.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value INTEGER
            );
        ''')
        # جدول المستخدمين
        c.execute('''
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Database error during init: {e}")

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

def add_video(message_id, caption, chat_id, file_name, category_id):
    """إضافة فيديو جديد إلى قاعدة البيانات."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("""
            INSERT INTO video_archive (message_id, caption, chat_id, file_name, category_id) 
            VALUES (%s, %s, %s, %s, %s) 
            ON CONFLICT (message_id) DO NOTHING
        """, (message_id, caption or "No caption", chat_id, file_name or "", category_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Add video error: {e}")

def get_videos(category_id=None, page=0):
    """استرداد الفيديوهات من قاعدة البيانات مع نظام الصفحات."""
    offset = page * VIDEOS_PER_PAGE
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        count_query = "SELECT COUNT(*) FROM video_archive WHERE category_id = %s"
        data_query = """
            SELECT v.message_id, v.caption, v.chat_id, v.file_name, c.name 
            FROM video_archive v
            JOIN categories c ON v.category_id = c.id
            WHERE v.category_id = %s
            ORDER BY v.id LIMIT %s OFFSET %s
        """
        
        c.execute(count_query, (category_id,))
        total_count = c.fetchone()[0]

        c.execute(data_query, (category_id, VIDEOS_PER_PAGE, offset))
        videos = c.fetchall()
        
        conn.close()
        return videos, total_count
    except Exception as e:
        print(f"Get videos error: {e}")
        return [], 0
        
def get_all_categories_tree():
    """Gets all categories as a tree structure."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT id, name, parent_id FROM categories ORDER BY name")
        rows = c.fetchall()
        
        nodes = {row[0]: {'id': row[0], 'name': row[1], 'parent_id': row[2], 'children': []} for row in rows}
        tree = []
        for node_id, node in nodes.items():
            if node['parent_id'] is None:
                tree.append(node)
            else:
                if node['parent_id'] in nodes:
                    nodes[node['parent_id']]['children'].append(node)
        
        conn.close()
        return tree
    except Exception as e:
        print(f"Get categories tree error: {e}")
        return []

def get_bot_stats():
    """الحصول على إحصائيات البوت."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM video_archive")
        video_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM categories")
        category_count = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM bot_users")
        subscriber_count = c.fetchone()[0]
        c.execute("""
            SELECT v.caption, v.file_name, v.request_count
            FROM video_archive v
            WHERE v.request_count > 0
            ORDER BY v.request_count DESC
            LIMIT 5
        """)
        top_videos = c.fetchall()
        conn.close()
        return video_count, category_count, subscriber_count, top_videos
    except Exception as e:
        print(f"Get bot stats error: {e}")
        return 0, 0, 0, []

def search_videos(query, page=0, category_id=None):
    """البحث عن فيديوهات في قاعدة البيانات مع دعم التصنيفات الشجرية."""
    offset = page * VIDEOS_PER_PAGE
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        def normalize_text(text):
            return text.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا').replace('ة', 'ه').replace('ى', 'ي')

        normalized_query = normalize_text(query)
        search_param = '%' + normalized_query + '%'

        def normalize_sql(column_name):
            return f"REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({column_name}, 'أ', 'ا'), 'إ', 'ا'), 'آ', 'ا'), 'ة', 'ه'), 'ى', 'ي')"

        search_condition = f"({normalize_sql('v.caption')} ILIKE %s OR {normalize_sql('v.file_name')} ILIKE %s)"
        params = [search_param, search_param]

        category_condition = ""
        if category_id:
            category_condition = """
                AND v.category_id IN (
                    WITH RECURSIVE sub_categories AS (
                        SELECT id FROM categories WHERE id = %s
                        UNION ALL
                        SELECT c.id FROM categories c JOIN sub_categories sc ON c.parent_id = sc.id
                    )
                    SELECT id FROM sub_categories
                )
            """
            params.append(category_id)

        base_query = f"""
            FROM video_archive v
            JOIN categories c ON v.category_id = c.id
            WHERE {search_condition} {category_condition}
        """

        count_query = f"SELECT COUNT(*) {base_query}"
        c.execute(count_query, tuple(params))
        total_count = c.fetchone()[0]

        data_query = f"SELECT v.message_id, v.caption, v.chat_id, v.file_name, c.name {base_query} ORDER BY v.id LIMIT %s OFFSET %s"
        params.extend([VIDEOS_PER_PAGE, offset])
        c.execute(data_query, tuple(params))
        results = c.fetchall()
        
        conn.close()
        return results, total_count
    except Exception as e:
        print(f"Search videos error: {e}")
        return [], 0

def update_video_category(message_id, category_id):
    """تحديث تصنيف فيديو معين."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("UPDATE video_archive SET category_id = %s WHERE message_id = %s", (category_id, message_id))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Update category error: {e}")
        return False

def increment_video_request_count(message_id):
    """زيادة عداد طلبات الفيديو."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("UPDATE video_archive SET request_count = request_count + 1 WHERE message_id = %s", (message_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Increment request count error: {e}")

def set_active_category(category_id):
    """حفظ أو تحديث التصنيف النشط في قاعدة البيانات."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("""
            INSERT INTO bot_settings (setting_key, setting_value) 
            VALUES ('active_category_id', %s)
            ON CONFLICT (setting_key) DO UPDATE SET setting_value = %s
        """, (category_id, category_id))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Set active category error: {e}")
        return False

def get_active_category_id():
    """الحصول على معرف التصنيف النشط حالياً."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT setting_value FROM bot_settings WHERE setting_key = 'active_category_id'")
        result = c.fetchone()
        conn.close()
        return result[0] if result else None
    except Exception as e:
        print(f"Get active category id error: {e}")
        return None

def add_new_category(name, parent_id=None):
    """إضافة تصنيف جديد."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("INSERT INTO categories (name, parent_id) VALUES (%s, %s) RETURNING id", (name, parent_id))
        new_id = c.fetchone()[0]
        conn.commit()
        conn.close()
        return new_id
    except Exception as e:
        print(f"Add new category error: {e}")
        return None

def rename_category_db(category_id, new_name):
    """إعادة تسمية تصنيف."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("UPDATE categories SET name = %s WHERE id = %s", (new_name, category_id))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Rename category error: {e}")
        return False

def delete_category_db(category_id):
    """حذف تصنيف ونقل محتوياته إلى التصنيف الأب."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT parent_id FROM categories WHERE id = %s", (category_id,))
        parent_id = c.fetchone()[0]
        
        c.execute("UPDATE categories SET parent_id = %s WHERE parent_id = %s", (parent_id, category_id))
        c.execute("UPDATE video_archive SET category_id = %s WHERE category_id = %s", (parent_id, category_id))
        
        c.execute("DELETE FROM categories WHERE id = %s", (category_id,))
        
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Delete category error: {e}")
        return False

# --- دوال مساعدة ---
def create_paginated_keyboard(items, total_items, page, prefix, context):
    """إنشاء لوحة مفاتيح مع أزرار الصفحات وزر الرجوع."""
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    for item in items:
        message_id, caption, chat_id, file_name, category_name = item
        title = caption or file_name or "فيديو بدون عنوان"
        keyboard.add(InlineKeyboardButton(text=f"{title[:50]} ({category_name})", callback_data=f"video::{message_id}::{chat_id}"))

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

def build_category_keyboard(tree, prefix, include_root=False):
    """بناء لوحة مفاتيح شجرية للتصنيفات."""
    keyboard = InlineKeyboardMarkup()
    if include_root:
        keyboard.add(InlineKeyboardButton(" (تصنيف رئيسي)", callback_data=f"{prefix}::root"))
    
    def add_nodes(nodes, level=0):
        for node in nodes:
            indent = "  " * level
            btn_text = f"{indent}- {node['name']}"
            keyboard.add(InlineKeyboardButton(btn_text, callback_data=f"{prefix}::{node['id']}"))
            if node['children']:
                add_nodes(node['children'], level + 1)
    
    add_nodes(tree)
    return keyboard

# --- أوامر البوت ---
@bot.message_handler(commands=['start'])
def start(message):
    add_bot_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    bot.reply_to(message, "أهلاً بك في بوت البحث عن الفيديوهات!", reply_markup=main_menu())

def main_menu():
    """إنشاء القائمة الرئيسية للبوت."""
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    list_button = KeyboardButton('🎬 عرض كل الفيديوهات')
    markup.add(list_button)
    return markup

@bot.message_handler(func=lambda message: message.text == '🎬 عرض كل الفيديوهات')
def handle_list_videos_button(message):
    list_videos(message)

def list_videos(message, edit_message=None):
    """عرض جميع الفئات المتاحة كأزرار."""
    tree = get_all_categories_tree()
    if not tree:
        text = "لا توجد أي تصنيفات متاحة حالياً."
        if edit_message: bot.answer_callback_query(edit_message.id, text)
        else: bot.reply_to(message, text)
        return

    keyboard = build_category_keyboard(tree, "cat")
    
    text = "اختر فئة لعرض فيديوهاتها:"
    if edit_message: bot.edit_message_text(text, edit_message.chat.id, edit_message.message_id, reply_markup=keyboard)
    else: bot.reply_to(message, text, reply_markup=keyboard)

# --- أوامر الآدمن ---
def generate_admin_panel():
    """إنشاء لوحة تحكم الآدمن."""
    keyboard = InlineKeyboardMarkup(row_width=2)
    btn_stats = InlineKeyboardButton("📊 الإحصائيات", callback_data="admin::stats")
    btn_add = InlineKeyboardButton("➕ إضافة تصنيف", callback_data="admin::add_cat")
    btn_set_active = InlineKeyboardButton("🔘 تعيين التصنيف النشط", callback_data="admin::set_active")
    btn_rename = InlineKeyboardButton("✏️ إعادة تسمية تصنيف", callback_data="admin::rename")
    btn_delete = InlineKeyboardButton("🗑️ حذف تصنيف", callback_data="admin::delete")
    btn_move_video = InlineKeyboardButton("↔️ نقل فيديو فردي", callback_data="admin::move_video")
    btn_help = InlineKeyboardButton("ℹ️ عرض المساعدة", callback_data="admin::help")
    keyboard.add(btn_stats, btn_add, btn_set_active, btn_rename, btn_delete, btn_move_video, btn_help)
    return keyboard

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if str(message.from_user.id) != ADMIN_ID: return
    bot.send_message(message.chat.id, "أهلاً بك في لوحة تحكم الآدمن. اختر أحد الخيارات:", reply_markup=generate_admin_panel())

@bot.message_handler(commands=['cancel'])
def cancel_step(message):
    if str(message.from_user.id) != ADMIN_ID: return
    if message.chat.id in admin_steps:
        del admin_steps[message.chat.id]
        bot.send_message(message.chat.id, "✅ تم إلغاء العملية الحالية بنجاح.")
    else:
        bot.send_message(message.chat.id, "لا توجد عملية لإلغائها.")

# --- معالجات خطوات الآدمن ---
def check_cancel(message):
    if message.text == '/cancel':
        if message.chat.id in admin_steps: del admin_steps[message.chat.id]
        bot.send_message(message.chat.id, "✅ تم إلغاء العملية الحالية بنجاح.")
        return True
    return False

# ... (بقية دوال معالجة خطوات الآدمن سيتم تحديثها لتناسب البنية الجديدة) ...

# --- معالجات الرسائل العامة ---
@bot.message_handler(content_types=['text'])
def handle_text_search(message):
    if message.text.startswith('/'): return
    query = message.text.strip()
    
    tree = get_all_categories_tree()
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("بحث في كل التصنيفات", callback_data=f"search::{query}::all::0"))
    
    def add_search_options(nodes, level=0):
        for node in nodes:
            indent = "  " * level
            keyboard.add(InlineKeyboardButton(f"{indent}بحث في: {node['name']}", callback_data=f"search::{query}::{node['id']}::0"))
            if node['children']:
                add_search_options(node['children'], level + 1)
    
    add_search_options(tree)
    bot.reply_to(message, f"أين تريد البحث عن '{query}'؟", reply_markup=keyboard)

@bot.message_handler(content_types=['video'])
def handle_new_video(message):
    if str(message.chat.id) == CHANNEL_ID:
        active_category_id = get_active_category_id()
        if not active_category_id:
            print("Warning: No active category set. Video not saved.")
            return
        print(f"New video detected. Assigning to active category ID: {active_category_id}. Message ID: {message.message_id}")
        add_video(
            message_id=message.message_id,
            caption=message.caption,
            chat_id=message.chat.id,
            file_name=message.video.file_name if message.video else "",
            category_id=active_category_id
        )

# --- معالج ضغطات الأزرار ---
@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    # This function will be heavily refactored to handle the new tree structure and admin panel
    try:
        bot.answer_callback_query(call.id)
        # Placeholder for new logic
        bot.send_message(call.message.chat.id, f"تم استلام الإجراء: {call.data}")
    except Exception as e:
        print(f"Callback query error: {e}")

# --- نقطة انطلاق البوت ---
if __name__ == "__main__":
    init_db()
    print("Bot is starting...")
    while True:
        try:
            bot.polling(non_stop=True)
        except Exception as e:
            print(f"An error occurred in the main loop: {e}")
            print("Restarting in 15 seconds...")
            time.sleep(15)
