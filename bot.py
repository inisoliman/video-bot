# ==============================================================================
# ملف: bot.py
# الوصف: الإصدار النهائي مع نظام التصنيفات المتداخلة (الشجرية) ولوحة تحكم للآدمن.
# ==============================================================================

import telebot
import psycopg2
import os
import time
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from urllib.parse import urlparse
import math

# --- الإعدادات الأساسية ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
CHANNEL_ID = os.getenv('CHANNEL_ID')
ADMIN_ID = os.getenv('ADMIN_ID')

if not all([BOT_TOKEN, DATABASE_URL, CHANNEL_ID, ADMIN_ID]):
    print("FATAL ERROR: Missing one or more environment variables.")
    exit()

bot = telebot.TeleBot(BOT_TOKEN)

DB_CONFIG = {}
if DATABASE_URL:
    url = urlparse(DATABASE_URL)
    DB_CONFIG = {
        'dbname': url.path[1:], 'user': url.username, 'password': url.password,
        'host': url.hostname, 'port': url.port
    }

# --- متغيرات الحالة ---
admin_steps = {}
user_last_search = {}
VIDEOS_PER_PAGE = 10
CALLBACK_DELIMITER = '::'
PATH_DELIMITER = ' / '

# --- دوال قاعدة البيانات ---

def init_db():
    """إنشاء الجداول اللازمة إذا لم تكن موجودة."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS categories (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                parent_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
                UNIQUE (name, parent_id)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS video_archive (
                id SERIAL PRIMARY KEY,
                message_id INTEGER UNIQUE,
                caption TEXT,
                chat_id BIGINT,
                file_name TEXT,
                category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT
            )
        ''')
        c.execute("INSERT INTO categories (id, name, parent_id) VALUES (0, 'الرئيسية', NULL) ON CONFLICT (id) DO NOTHING")
        conn.commit()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Database error during init: {e}")

def add_video(message_id, caption, chat_id, file_name=None, category_id=None):
    """إضافة فيديو جديد إلى قاعدة البيانات."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("""
            INSERT INTO video_archive (message_id, caption, chat_id, file_name, category_id) 
            VALUES (%s, %s, %s, %s, %s) ON CONFLICT (message_id) DO NOTHING
        """, (message_id, caption or "No caption", chat_id, file_name or "", category_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Add video error: {e}")

def get_subcategories(parent_id):
    """الحصول على التصنيفات الفرعية لتصنيف معين."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        if parent_id is None or parent_id == 0:
            c.execute("SELECT id, name FROM categories WHERE parent_id IS NULL AND id != 0 ORDER BY name")
        else:
            c.execute("SELECT id, name FROM categories WHERE parent_id = %s ORDER BY name", (parent_id,))
        subcategories = c.fetchall()
        conn.close()
        return subcategories
    except Exception as e:
        print(f"Get subcategories error: {e}")
        return []

def get_videos_in_category(category_id, page=0):
    """الحصول على الفيديوهات داخل تصنيف معين مع نظام الصفحات."""
    offset = page * VIDEOS_PER_PAGE
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM video_archive WHERE category_id = %s", (category_id,))
        total_count = c.fetchone()[0]
        c.execute("""
            SELECT v.message_id, v.caption, v.chat_id, v.file_name 
            FROM video_archive v
            WHERE v.category_id = %s ORDER BY v.id DESC LIMIT %s OFFSET %s
        """, (category_id, VIDEOS_PER_PAGE, offset))
        videos = c.fetchall()
        conn.close()
        return videos, total_count
    except Exception as e:
        print(f"Get videos in category error: {e}")
        return [], 0
        
def get_category_path(category_id):
    """الحصول على المسار الكامل لتصنيف معين."""
    path = []
    current_id = category_id
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        while current_id is not None and current_id != 0:
            c.execute("SELECT name, parent_id FROM categories WHERE id = %s", (current_id,))
            result = c.fetchone()
            if not result: break
            name, parent_id = result
            path.insert(0, name)
            current_id = parent_id
        conn.close()
        return PATH_DELIMITER.join(path)
    except Exception as e:
        print(f"Get category path error: {e}")
        return ""

def set_active_category(category_id):
    """تعيين التصنيف النشط."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("""
            INSERT INTO bot_settings (setting_key, setting_value) VALUES ('active_category_id', %s)
            ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value
        """, (str(category_id),))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Set active category error: {e}")
        return False

def get_active_category_id():
    """الحصول على معرف التصنيف النشط."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT setting_value FROM bot_settings WHERE setting_key = 'active_category_id'")
        result = c.fetchone()
        conn.close()
        return int(result[0]) if result else 0
    except Exception as e:
        print(f"Get active category error: {e}")
        return 0
        
def add_category_db(name, parent_id):
    """إضافة تصنيف جديد."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("INSERT INTO categories (name, parent_id) VALUES (%s, %s) RETURNING id", (name, parent_id if parent_id != 0 else None))
        new_id = c.fetchone()[0]
        conn.commit()
        conn.close()
        return new_id
    except Exception as e:
        print(f"Add category error: {e}")
        return None

def delete_category_db(category_id):
    """حذف تصنيف ونقل محتوياته للأصل."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT parent_id FROM categories WHERE id = %s", (category_id,))
        result = c.fetchone()
        parent_id = result[0] if result and result[0] is not None else 0

        c.execute("UPDATE categories SET parent_id = %s WHERE parent_id = %s", (parent_id, category_id))
        c.execute("UPDATE video_archive SET category_id = %s WHERE category_id = %s", (parent_id, category_id))
        c.execute("DELETE FROM categories WHERE id = %s", (category_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Delete category error: {e}")
        return False
        
def update_category_name_db(category_id, new_name):
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

def update_video_category_id(message_id, new_category_id):
    """نقل فيديو إلى تصنيف آخر."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("UPDATE video_archive SET category_id = %s WHERE message_id = %s", (new_category_id, message_id))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Move video error: {e}")
        return False

# --- واجهة المستخدم والمنطق ---

@bot.message_handler(commands=['start'])
def start(message):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(KeyboardButton('🗂️ تصفح التصنيفات'))
    bot.reply_to(message, "أهلاً بك!", reply_markup=markup)

@bot.message_handler(func=lambda msg: msg.text == '🗂️ تصفح التصنيفات')
def browse_categories_start(message):
    browse_categories(message, 0)

def browse_categories(message, category_id, page=0, edit_message=None):
    """دالة رئيسية لعرض محتويات أي تصنيف."""
    subcategories = get_subcategories(category_id)
    videos, total_videos = get_videos_in_category(category_id, page)

    keyboard = InlineKeyboardMarkup(row_width=2)
    for cat_id, cat_name in subcategories:
        keyboard.add(InlineKeyboardButton(f"📁 {cat_name}", callback_data=f"browse::{cat_id}::0"))
    
    for video_id, caption, chat_id, file_name in videos:
         title = caption or file_name or "فيديو بدون عنوان"
         keyboard.add(InlineKeyboardButton(f"🎬 {title[:40]}", callback_data=f"video::{video_id}::{chat_id}"))

    total_pages = math.ceil(total_videos / VIDEOS_PER_PAGE)
    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"browse::{category_id}::{page - 1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("التالي ➡️", callback_data=f"browse::{category_id}::{page + 1}"))
        keyboard.row(*nav_buttons)

    if category_id != 0:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT parent_id FROM categories WHERE id = %s", (category_id,))
        parent_id = c.fetchone()[0]
        conn.close()
        if parent_id is None: parent_id = 0
        keyboard.add(InlineKeyboardButton("⬅️ رجوع", callback_data=f"browse::{parent_id}::0"))

    path = get_category_path(category_id) or "الرئيسية"
    text = f"المسار الحالي: *{path}*"
    
    try:
        if edit_message:
            bot.edit_message_text(text, message.chat.id, message.message_id, reply_markup=keyboard, parse_mode='Markdown')
        else:
            bot.send_message(message.chat.id, text, reply_markup=keyboard, parse_mode='Markdown')
    except telebot.apihelper.ApiTelegramException as e:
        if "message is not modified" not in e.description:
            raise e

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if str(message.from_user.id) != ADMIN_ID: return
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("➕ إضافة تصنيف", callback_data="admin::add_cat"),
        InlineKeyboardButton("🔘 تعيين النشط", callback_data="admin::set_active"),
        InlineKeyboardButton("🗑️ حذف تصنيف", callback_data="admin::delete_cat"),
        InlineKeyboardButton("✏️ إعادة تسمية", callback_data="admin::rename_cat"),
        InlineKeyboardButton("↔️ نقل فيديو", callback_data="admin::move_video")
    )
    bot.send_message(message.chat.id, "لوحة تحكم الآدمن:", reply_markup=keyboard)

@bot.message_handler(content_types=['video'])
def handle_new_video(message):
    if str(message.chat.id) == CHANNEL_ID:
        active_category_id = get_active_category_id()
        add_video(message.message_id, message.caption, message.chat.id, 
                  message.video.file_name if message.video else "", active_category_id)

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    """المعالج الرئيسي لجميع ضغطات الأزرار."""
    try:
        action, *params = call.data.split(CALLBACK_DELIMITER)

        if action == "browse":
            category_id = int(params[0])
            page = int(params[1])
            browse_categories(call.message, category_id, page, edit_message=True)
        elif action == "video":
            message_id, chat_id = params
            bot.copy_message(call.message.chat.id, chat_id, int(message_id))
        elif action == "admin":
            handle_admin_callbacks(call, params)
        elif action == "admin_browse":
            handle_admin_browse(call, params)
        
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"Callback query error: {e}")
        bot.answer_callback_query(call.id, "حدث خطأ.")

def handle_admin_callbacks(call, params):
    """معالجة ضغطات أزرار لوحة تحكم الآدمن."""
    sub_action = params[0]
    
    if sub_action == "add_cat":
        msg = bot.send_message(call.message.chat.id, "أرسل اسم التصنيف الجديد. (للإلغاء أرسل /cancel)")
        bot.register_next_step_handler(msg, ask_for_parent_category, "add_cat_parent")
    elif sub_action == "set_active":
        bot.edit_message_text("اختر التصنيف لجعله نشطاً:", call.message.chat.id, call.message.message_id,
                              reply_markup=create_category_selection_keyboard("admin::setcat_confirm"))
    elif sub_action == "setcat_confirm":
        category_id = int(params[1])
        if set_active_category(category_id):
            path = get_category_path(category_id) or "الرئيسية"
            bot.edit_message_text(f"✅ تم تفعيل التصنيف '{path}' بنجاح.", call.message.chat.id, call.message.message_id)
    elif sub_action == "delete_cat":
        bot.edit_message_text("اختر التصنيف الذي تريد حذفه (لن يتم حذف 'الرئيسية'):", call.message.chat.id, call.message.message_id,
                              reply_markup=create_category_selection_keyboard("admin::delete_confirm"))
    elif sub_action == "delete_confirm":
        category_id = int(params[1])
        if category_id == 0:
            bot.edit_message_text("لا يمكن حذف التصنيف الرئيسي.", call.message.chat.id, call.message.message_id)
            return
        path = get_category_path(category_id)
        if delete_category_db(category_id):
            bot.edit_message_text(f"✅ تم حذف التصنيف '{path}' بنجاح.", call.message.chat.id, call.message.message_id)
    elif sub_action == "rename_cat":
        bot.edit_message_text("اختر التصنيف الذي تريد إعادة تسميته:", call.message.chat.id, call.message.message_id,
                              reply_markup=create_category_selection_keyboard("admin::rename_select"))
    elif sub_action == "rename_select":
        category_id = int(params[1])
        admin_steps[call.message.chat.id] = {'category_to_rename_id': category_id}
        msg = bot.send_message(call.message.chat.id, "أرسل الاسم الجديد للتصنيف. (للإلغاء أرسل /cancel)")
        bot.register_next_step_handler(msg, process_rename_category_new_name)
    elif sub_action == "move_video":
        msg = bot.send_message(call.message.chat.id, "قم بإعادة توجيه الفيديو الذي تريد نقله. (للإلغاء أرسل /cancel)")
        bot.register_next_step_handler(msg, process_move_video_forward)

def ask_for_parent_category(message, action_prefix):
    if message.text == '/cancel':
        bot.send_message(message.chat.id, "تم الإلغاء.")
        return
    new_name = message.text.strip()
    admin_steps[message.chat.id] = {'new_name': new_name}
    bot.send_message(message.chat.id, "الآن، اختر التصنيف الأب.", 
                     reply_markup=create_category_selection_keyboard(f"admin::{action_prefix}"))

def create_category_selection_keyboard(action_prefix, current_id=0):
    """إنشاء لوحة مفاتيح لتصفح واختيار التصنيفات."""
    keyboard = InlineKeyboardMarkup(row_width=2)
    subcategories = get_subcategories(current_id)
    
    keyboard.add(InlineKeyboardButton("✅ اختر هذا المستوى", callback_data=f"{action_prefix}::{current_id}"))
    for cat_id, cat_name in subcategories:
        keyboard.add(InlineKeyboardButton(f"📁 {cat_name}", callback_data=f"admin_browse::{action_prefix}::{cat_id}"))
    
    if current_id != 0:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT parent_id FROM categories WHERE id = %s", (current_id,))
        parent_id = c.fetchone()[0]
        conn.close()
        if parent_id is None: parent_id = 0
        keyboard.add(InlineKeyboardButton("⬅️ رجوع", callback_data=f"admin_browse::{action_prefix}::{parent_id}"))
    return keyboard

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_browse::"))
def handle_admin_browse(call):
    """معالجة تصفح الآدمن للتصنيفات."""
    try:
        _, action_prefix, category_id_str = call.data.split(CALLBACK_DELIMITER)
        category_id = int(category_id_str)
        path = get_category_path(category_id) or "الرئيسية"
        text = f"اختر التصنيف. المسار الحالي: {path}"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, 
                              reply_markup=create_category_selection_keyboard(action_prefix, category_id))
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"Admin browse error: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin::add_cat_parent"))
def handle_add_cat_parent_selection(call):
    try:
        _, _, parent_id_str = call.data.split(CALLBACK_DELIMITER)
        parent_id = int(parent_id_str)
        new_cat_name = admin_steps.pop(call.message.chat.id, {}).get('new_name')
        if not new_cat_name:
            bot.edit_message_text("انتهت صلاحية العملية، يرجى المحاولة مرة أخرى.", call.message.chat.id, call.message.message_id)
            return
        new_id = add_category_db(new_cat_name, parent_id)
        if new_id:
            bot.edit_message_text(f"✅ تم إنشاء التصنيف '{new_cat_name}' بنجاح.", call.message.chat.id, call.message.message_id)
        else:
            bot.edit_message_text("❌ فشل إنشاء التصنيف. قد يكون الاسم مكرراً في نفس المستوى.", call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"Add cat parent selection error: {e}")

def process_rename_category_new_name(message):
    if message.text == '/cancel':
        bot.send_message(message.chat.id, "تم الإلغاء.")
        return
    new_name = message.text.strip()
    category_id = admin_steps.pop(message.chat.id, {}).get('category_to_rename_id')
    if not category_id: return
    if update_category_name_db(category_id, new_name):
        bot.send_message(message.chat.id, f"✅ تم تغيير اسم التصنيف بنجاح إلى '{new_name}'.")

def process_move_video_forward(message):
    if message.text == '/cancel':
        bot.send_message(message.chat.id, "تم الإلغاء.")
        return
    if not message.forward_from_message_id:
        msg = bot.send_message(message.chat.id, "خطأ: يرجى إعادة توجيه الرسالة. (أو أرسل /cancel للإلغاء)")
        bot.register_next_step_handler(msg, process_move_video_forward)
        return
    original_message_id = message.forward_from_message_id
    admin_steps[message.chat.id] = {'video_to_move_id': original_message_id}
    bot.send_message(message.chat.id, "الآن، اختر التصنيف الجديد الذي تريد نقل الفيديو إليه.",
                     reply_markup=create_category_selection_keyboard("admin::move_video_confirm"))

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin::move_video_confirm"))
def handle_move_video_confirm(call):
    try:
        _, _, category_id_str = call.data.split(CALLBACK_DELIMITER)
        new_category_id = int(category_id_str)
        message_id = admin_steps.pop(call.message.chat.id, {}).get('video_to_move_id')
        if not message_id:
            bot.edit_message_text("انتهت صلاحية العملية، يرجى المحاولة مرة أخرى.", call.message.chat.id, call.message.message_id)
            return
        if update_video_category_id(message_id, new_category_id):
            path = get_category_path(new_category_id)
            bot.edit_message_text(f"✅ تم نقل الفيديو بنجاح إلى التصنيف '{path}'.", call.message.chat.id, call.message.message_id)
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"Move video confirm error: {e}")

# --- Main Loop ---
if __name__ == "__main__":
    init_db()
    print("Bot is starting...")
    while True:
        try:
            bot.polling(non_stop=True)
        except Exception as e:
            print(f"Polling error: {e}")
            time.sleep(15)
