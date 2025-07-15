# ==============================================================================
# ملف: bot_v3_complete.py
# الوصف: النسخة الكاملة والمدمجة مع نظام تقييم، حذف فيديوهات، وبحث مباشر.
# ==============================================================================

import telebot
import psycopg2
import os
import time
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, InlineQueryResultVideo
from urllib.parse import urlparse
import math
import uuid

# --- الإعدادات الأساسية (قراءة آمنة من متغيرات البيئة) ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
CHANNEL_ID = os.getenv('CHANNEL_ID')
ADMIN_ID = os.getenv('ADMIN_ID')
DEFAULT_THUMB_URL = "https://via.placeholder.com/150.jpg?text=Video"

if not all([BOT_TOKEN, DATABASE_URL, CHANNEL_ID, ADMIN_ID]):
    print("FATAL ERROR: Missing environment variables.")
    exit()

bot = telebot.TeleBot(BOT_TOKEN)

url = urlparse(DATABASE_URL)
DB_CONFIG = {
    'dbname': url.path[1:],
    'user': url.username,
    'password': url.password,
    'host': url.hostname,
    'port': url.port
}

admin_steps = {}
user_last_search = {}
VIDEOS_PER_PAGE = 10
CALLBACK_DELIMITER = '::'

# --- دوال قاعدة البيانات ---

def init_db():
    """إنشاء وتحديث الجداول اللازمة في قاعدة البيانات."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS video_archive (
                id SERIAL PRIMARY KEY,
                message_id INTEGER UNIQUE,
                caption TEXT,
                chat_id BIGINT,
                file_name TEXT,
                category TEXT DEFAULT 'Uncategorized',
                file_id TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                join_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS video_ratings (
                rating_id SERIAL PRIMARY KEY,
                message_id INTEGER,
                user_id BIGINT,
                rating INTEGER,
                UNIQUE (message_id, user_id)
            )
        ''')
        try:
            c.execute("ALTER TABLE video_archive ADD COLUMN file_id TEXT;")
            print("Database schema updated: Added 'file_id' column.")
        except psycopg2.errors.DuplicateColumn:
            pass
        conn.commit()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Database error during init: {e}")

def add_bot_user(user_id, username, first_name):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("INSERT INTO bot_users (user_id, username, first_name) VALUES (%s, %s, %s) ON CONFLICT (user_id) DO NOTHING", (user_id, username, first_name))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Add bot user error: {e}")

def get_all_user_ids():
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

def add_video(message_id, caption, chat_id, file_id, file_name=None, category='Uncategorized'):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("INSERT INTO video_archive (message_id, caption, chat_id, file_name, category, file_id) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (message_id) DO NOTHING", (message_id, caption or "No caption", chat_id, file_name or "", category, file_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Add video error: {e}")

def get_videos(category=None, page=0):
    offset = page * VIDEOS_PER_PAGE
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        count_query = "SELECT COUNT(*) FROM video_archive"
        data_query = "SELECT message_id, caption, chat_id, file_name, category, file_id FROM video_archive"
        params = []
        if category:
            where_clause = " WHERE category = %s"
            count_query += where_clause
            data_query += where_clause
            params.append(category)
        c.execute(count_query, params)
        total_count = c.fetchone()[0]
        data_query += " ORDER BY id DESC LIMIT %s OFFSET %s"
        params.extend([VIDEOS_PER_PAGE, offset])
        c.execute(data_query, params)
        videos = c.fetchall()
        conn.close()
        return videos, total_count
    except Exception as e:
        print(f"Get videos error: {e}")
        return [], 0

def get_all_distinct_categories():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT DISTINCT category FROM video_archive ORDER BY category")
        categories = [row[0] for row in c.fetchall()]
        conn.close()
        return categories
    except Exception as e:
        print(f"Get distinct categories error: {e}")
        return []

def get_bot_stats():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM video_archive")
        video_count = c.fetchone()[0]
        c.execute("SELECT COUNT(DISTINCT category) FROM video_archive")
        category_count = c.fetchone()[0]
        conn.close()
        return video_count, category_count
    except Exception as e:
        print(f"Get bot stats error: {e}")
        return 0, 0

def search_videos(query, page=0, category=None):
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
        where_clause = f"({normalize_sql('caption')} ILIKE %s OR {normalize_sql('file_name')} ILIKE %s)"
        params = [search_param, search_param]
        if category:
            where_clause += " AND category = %s"
            params.append(category)
        count_query = f"SELECT COUNT(*) FROM video_archive WHERE {where_clause}"
        c.execute(count_query, tuple(params))
        total_count = c.fetchone()[0]
        data_query = f"SELECT message_id, caption, chat_id, file_name, category, file_id FROM video_archive WHERE {where_clause} ORDER BY id DESC LIMIT %s OFFSET %s"
        params.extend([VIDEOS_PER_PAGE, offset])
        c.execute(data_query, tuple(params))
        results = c.fetchall()
        conn.close()
        return results, total_count
    except Exception as e:
        print(f"Search videos error: {e}")
        return [], 0

def update_video_category(message_id, category):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("UPDATE video_archive SET category = %s WHERE message_id = %s", (category, message_id))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Update category error: {e}")
        return False

def set_active_category(category_name):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("INSERT INTO bot_settings (setting_key, setting_value) VALUES ('active_category', %s) ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value", (category_name,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Set active category error: {e}")
        return False

def get_active_category():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT setting_value FROM bot_settings WHERE setting_key = 'active_category'")
        result = c.fetchone()
        conn.close()
        return result[0] if result else 'Uncategorized'
    except Exception as e:
        print(f"Get active category error: {e}")
        return 'Uncategorized'

def rename_category_in_db(old_name, new_name):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("UPDATE video_archive SET category = %s WHERE category = %s", (new_name, old_name))
        c.execute("UPDATE bot_settings SET setting_value = %s WHERE setting_key = 'active_category' AND setting_value = %s", (new_name, old_name))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Rename category error: {e}")
        return False

def delete_category_db(category_to_delete, target_category='Uncategorized'):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("UPDATE video_archive SET category = %s WHERE category = %s", (target_category, category_to_delete))
        c.execute("UPDATE bot_settings SET setting_value = %s WHERE setting_key = 'active_category' AND setting_value = %s", (target_category, category_to_delete))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Delete category error: {e}")
        return False

def delete_video_db(message_id):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("DELETE FROM video_archive WHERE message_id = %s", (message_id,))
        c.execute("DELETE FROM video_ratings WHERE message_id = %s", (message_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Delete video from DB error: {e}")
        return False

def add_or_update_rating(message_id, user_id, rating):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("INSERT INTO video_ratings (message_id, user_id, rating) VALUES (%s, %s, %s) ON CONFLICT (message_id, user_id) DO UPDATE SET rating = EXCLUDED.rating;", (message_id, user_id, rating))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Rating error: {e}")

def get_video_ratings(message_id):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("SELECT SUM(rating) FROM video_ratings WHERE message_id = %s", (message_id,))
        score = c.fetchone()[0]
        conn.close()
        return score if score is not None else 0
    except Exception as e:
        print(f"Get ratings error: {e}")
        return 0

# --- دوال مساعدة ---

def create_rating_keyboard(message_id, chat_id):
    score = get_video_ratings(message_id)
    keyboard = InlineKeyboardMarkup()
    like_button = InlineKeyboardButton(f"�", callback_data=f"rate::{message_id}::{chat_id}::1")
    dislike_button = InlineKeyboardButton(f"👎", callback_data=f"rate::{message_id}::{chat_id}::-1")
    score_button = InlineKeyboardButton(f"التقييم: {score}", callback_data="noop")
    keyboard.row(like_button, score_button, dislike_button)
    return keyboard

def create_paginated_keyboard(items, total_items, page, prefix, context):
    keyboard = InlineKeyboardMarkup(row_width=1)
    for item in items:
        message_id, caption, chat_id, file_name, category, file_id = item
        title = caption or file_name or "فيديو بدون عنوان"
        keyboard.add(InlineKeyboardButton(text=f"{title[:50]} ({category})", callback_data=f"video::{message_id}::{chat_id}"))
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

def main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    list_button = KeyboardButton('🎬 عرض كل الفيديوهات')
    markup.add(list_button)
    return markup

def generate_admin_panel():
    keyboard = InlineKeyboardMarkup(row_width=2)
    buttons = [
        InlineKeyboardButton("📢 بث رسالة", callback_data="admin::broadcast"),
        InlineKeyboardButton("👤 عدد المشتركين", callback_data="admin::sub_count"),
        InlineKeyboardButton("📊 إحصائيات المحتوى", callback_data="admin::stats"),
        InlineKeyboardButton("➕ إضافة تصنيف", callback_data="admin::add_new_cat"),
        InlineKeyboardButton("🔘 تعيين النشط", callback_data="admin::set_active"),
        InlineKeyboardButton("✏️ إعادة تسمية", callback_data="admin::rename"),
        InlineKeyboardButton("🗑️ حذف تصنيف", callback_data="admin::delete_cat"),
        InlineKeyboardButton("↔️ نقل فيديو", callback_data="admin::move_video"),
        InlineKeyboardButton("❌ حذف فيديو محدد", callback_data="admin::delete_video")
    ]
    keyboard.add(*buttons)
    return keyboard

# --- معالجات الأوامر والرسائل ---

@bot.message_handler(commands=['start'])
def start(message):
    add_bot_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    bot.reply_to(message, "أهلاً بك في بوت البحث عن الفيديوهات!", reply_markup=main_menu())

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if str(message.from_user.id) != ADMIN_ID:
        bot.reply_to(message, "هذا الأمر مخصص لصاحب البوت فقط.")
        return
    bot.send_message(message.chat.id, "أهلاً بك في لوحة تحكم الآدمن:", reply_markup=generate_admin_panel())

@bot.message_handler(commands=['cancel'])
def cancel_step(message):
    if str(message.from_user.id) != ADMIN_ID: return
    if message.chat.id in admin_steps:
        del admin_steps[message.chat.id]
        bot.send_message(message.chat.id, "✅ تم إلغاء العملية الحالية.")
    else:
        bot.send_message(message.chat.id, "لا توجد عملية لإلغائها.")

def check_cancel(message):
    if message.text == '/cancel':
        if message.chat.id in admin_steps:
            del admin_steps[message.chat.id]
        bot.send_message(message.chat.id, "✅ تم إلغاء العملية الحالية.")
        return True
    return False

# --- معالجات خطوات الآدمن ---

def handle_broadcast_message(message):
    if check_cancel(message): return
    user_ids = get_all_user_ids()
    sent_count, failed_count = 0, 0
    bot.send_message(message.chat.id, f"بدء إرسال الرسالة إلى {len(user_ids)} مشترك...")
    for user_id in user_ids:
        try:
            bot.copy_message(user_id, message.chat.id, message.message_id)
            sent_count += 1
        except Exception as e:
            failed_count += 1
            print(f"Failed to send to {user_id}: {e}")
        time.sleep(0.1)
    bot.send_message(message.chat.id, f"✅ اكتمل البث!\n- ناجح: {sent_count}\n- فاشل: {failed_count}")
    del admin_steps[message.chat.id]

def handle_add_new_category(message):
    if check_cancel(message): return
    if set_active_category(message.text.strip()):
        bot.reply_to(message, f"✅ تم إنشاء وتفعيل التصنيف: '{message.text.strip()}'.")
    del admin_steps[message.chat.id]

def handle_rename_old(message):
    if check_cancel(message): return
    admin_steps[message.chat.id]['old_name'] = message.text.strip()
    msg = bot.send_message(message.chat.id, "حسناً. الآن أرسل الاسم الجديد.")
    bot.register_next_step_handler(msg, handle_rename_new)

def handle_rename_new(message):
    if check_cancel(message): return
    old_name = admin_steps[message.chat.id].get('old_name')
    if rename_category_in_db(old_name, message.text.strip()):
        bot.send_message(message.chat.id, f"✅ تم تغيير '{old_name}' إلى '{message.text.strip()}'.")
    del admin_steps[message.chat.id]

def handle_delete_category(message):
    if check_cancel(message): return
    if delete_category_db(message.text.strip()):
        bot.send_message(message.chat.id, f"✅ تم حذف '{message.text.strip()}' ونقل محتوياته.")
    del admin_steps[message.chat.id]

def handle_move_video_forward(message):
    if check_cancel(message): return
    if not message.forward_from_message_id:
        msg = bot.send_message(message.chat.id, "خطأ: يرجى إعادة توجيه الرسالة.")
        bot.register_next_step_handler(msg, handle_move_video_forward)
        return
    admin_steps[message.chat.id]['video_id'] = message.forward_from_message_id
    msg = bot.send_message(message.chat.id, "ممتاز. الآن أرسل اسم التصنيف الجديد.")
    bot.register_next_step_handler(msg, handle_move_video_new_cat)

def handle_move_video_new_cat(message):
    if check_cancel(message): return
    video_id = admin_steps[message.chat.id].get('video_id')
    if update_video_category(video_id, message.text.strip()):
        bot.send_message(message.chat.id, f"✅ تم نقل الفيديو إلى '{message.text.strip()}'.")
    del admin_steps[message.chat.id]

def handle_delete_video_forward(message):
    if check_cancel(message): return
    if not message.forward_from_message_id:
        msg = bot.send_message(message.chat.id, "خطأ: يرجى إعادة توجيه الرسالة الأصلية.")
        bot.register_next_step_handler(msg, handle_delete_video_forward)
        return
    if delete_video_db(message.forward_from_message_id):
        bot.send_message(message.chat.id, f"✅ تم حذف الفيديو بنجاح من قاعدة البيانات.")
    else:
        bot.send_message(message.chat.id, "حدث خطأ أثناء الحذف.")
    del admin_steps[message.chat.id]

# --- معالجات الرسائل العامة ---

@bot.message_handler(func=lambda message: message.text == '🎬 عرض كل الفيديوهات')
def handle_list_videos_button(message):
    list_videos(message)

def list_videos(message, edit_message=None):
    categories = get_all_distinct_categories()
    if not categories:
        text = "لا توجد أي تصنيفات متاحة حالياً."
        if edit_message: bot.answer_callback_query(edit_message.id, text)
        else: bot.reply_to(message, text)
        return
    keyboard = InlineKeyboardMarkup(row_width=2)
    buttons = [InlineKeyboardButton(text=cat, callback_data=f"cat::{cat}::0") for cat in categories]
    keyboard.add(*buttons)
    text = "اختر فئة لعرض فيديوهاتها:"
    if edit_message: bot.edit_message_text(text, edit_message.chat.id, edit_message.message_id, reply_markup=keyboard)
    else: bot.reply_to(message, text, reply_markup=keyboard)

@bot.message_handler(content_types=['text'])
def handle_text_search(message):
    if message.text.startswith('/'): return
    query = message.text.strip()
    user_last_search[message.chat.id] = query
    categories = get_all_distinct_categories()
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(InlineKeyboardButton("بحث في كل التصنيفات", callback_data=f"search_scope::all"))
    for cat in categories:
        keyboard.add(InlineKeyboardButton(f"بحث في: {cat}", callback_data=f"search_scope::{cat}"))
    bot.reply_to(message, f"أين تريد البحث عن '{query}'؟", reply_markup=keyboard)

@bot.message_handler(content_types=['video'])
def handle_new_video(message):
    if str(message.chat.id) == CHANNEL_ID:
        active_category = get_active_category()
        add_video(
            message_id=message.message_id,
            caption=message.caption,
            chat_id=message.chat.id,
            file_name=message.video.file_name if message.video else "",
            category=active_category,
            file_id=message.video.file_id
        )

# --- معالج ضغطات الأزرار ---

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    try:
        data = call.data.split(CALLBACK_DELIMITER)
        action = data[0]
        user_id = call.from_user.id

        if action == "admin":
            if str(user_id) != ADMIN_ID: return
            bot.answer_callback_query(call.id)
            sub_action = data[1]
            admin_steps[user_id] = {} # Reset steps
            
            if sub_action == "broadcast":
                msg = bot.send_message(user_id, "أرسل الرسالة التي تريد بثها. (أو /cancel)")
                bot.register_next_step_handler(msg, handle_broadcast_message)
            elif sub_action == "sub_count":
                bot.send_message(user_id, f"👤 إجمالي المشتركين: *{get_subscriber_count()}*", parse_mode='Markdown')
            elif sub_action == "stats":
                video_count, category_count = get_bot_stats()
                bot.send_message(user_id, f"📊 *إحصائيات المحتوى*\n\n- فيديوهات: *{video_count}*\n- تصنيفات: *{category_count}*", parse_mode='Markdown')
            elif sub_action == "add_new_cat":
                msg = bot.send_message(user_id, "أرسل اسم التصنيف الجديد. (أو /cancel)")
                bot.register_next_step_handler(msg, handle_add_new_category)
            elif sub_action == "set_active":
                categories = get_all_distinct_categories()
                if not categories:
                    bot.answer_callback_query(call.id, "لا توجد تصنيفات حالياً.")
                    return
                keyboard = InlineKeyboardMarkup(row_width=2)
                buttons = [InlineKeyboardButton(text=cat, callback_data=f"admin_setcat::{cat}") for cat in categories]
                keyboard.add(*buttons)
                bot.edit_message_text("اختر التصنيف النشط:", user_id, call.message.message_id, reply_markup=keyboard)
            elif sub_action == "rename":
                msg = bot.send_message(user_id, "أرسل اسم التصنيف القديم. (أو /cancel)")
                bot.register_next_step_handler(msg, handle_rename_old)
            elif sub_action == "delete_cat":
                msg = bot.send_message(user_id, "أرسل اسم التصنيف للحذف. (أو /cancel)")
                bot.register_next_step_handler(msg, handle_delete_category)
            elif sub_action == "move_video":
                msg = bot.send_message(user_id, "أعد توجيه الفيديو للنقل. (أو /cancel)")
                bot.register_next_step_handler(msg, handle_move_video_forward)
            elif sub_action == "delete_video":
                msg = bot.send_message(user_id, "أعد توجيه الفيديو للحذف. (أو /cancel)")
                bot.register_next_step_handler(msg, handle_delete_video_forward)

        elif action == "admin_setcat":
            if str(user_id) != ADMIN_ID: return
            category_name = data[1]
            if set_active_category(category_name):
                bot.edit_message_text(f"✅ تم تفعيل التصنيف '{category_name}'.", user_id, call.message.message_id)

        elif action == "rate":
            _, message_id, chat_id, rating = data
            add_or_update_rating(int(message_id), user_id, int(rating))
            bot.answer_callback_query(call.id, "شكراً لتقييمك!")
            new_keyboard = create_rating_keyboard(int(message_id), int(chat_id))
            try:
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=new_keyboard)
            except Exception:
                pass
        
        elif action == "video":
            _, message_id, chat_id = data
            keyboard = create_rating_keyboard(int(message_id), int(chat_id))
            bot.copy_message(call.message.chat.id, chat_id, int(message_id), reply_markup=keyboard)
            bot.answer_callback_query(call.id, "جاري إرسال الفيديو...")

        elif action == "cat" or action.startswith("search_"):
            bot.answer_callback_query(call.id)
            context, page = None, 0
            if action == "cat":
                _, context, page_str = data
                page = int(page_str)
                videos, total_count = get_videos(context, page)
                text = f"الفيديوهات في فئة '{context}':"
                prefix = "cat"
            else: # search
                action_type, scope = data[0].split('_')
                if len(data) > 1: # paginating
                    _, context, page_str = data
                    page = int(page_str)
                else: # first search
                    context = data[1] if len(data) > 1 else 'all'
                
                query = user_last_search.get(user_id)
                if not query:
                    bot.edit_message_text("انتهت صلاحية البحث، يرجى البحث مرة أخرى.", call.message.chat.id, call.message.message_id)
                    return
                
                search_cat = context if context != 'all' else None
                videos, total_count = search_videos(query, page=page, category=search_cat)
                
                if not videos:
                    bot.edit_message_text(f"لم يتم العثور على نتائج للبحث عن '{query}'.", call.message.chat.id, call.message.message_id)
                    return
                
                text = f"نتائج البحث عن '{query}':"
                prefix = f"search_{context}"

            keyboard = create_paginated_keyboard(videos, total_count, page, prefix, context)
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=keyboard)

        elif action == "back_to_cats":
            list_videos(call.message, edit_message=call.message)
            bot.answer_callback_query(call.id)
        
        elif action == "noop":
            bot.answer_callback_query(call.id)

    except Exception as e:
        print(f"Callback query error: {e}")
        bot.answer_callback_query(call.id, "حدث خطأ.")

# --- معالج البحث المباشر ---

@bot.inline_handler(lambda query: len(query.query) > 2)
def inline_query_handler(inline_query):
    try:
        query = inline_query.query
        results, _ = search_videos(query, page=0)
        inline_results = []
        for item in results:
            message_id, caption, chat_id, file_name, category, file_id = item
            if not file_id: continue
            title = caption or file_name or "فيديو بدون عنوان"
            r = InlineQueryResultVideo(
                id=str(uuid.uuid4()),
                video_file_id=file_id,
                title=title,
                caption=f"{title}\n\nالتصنيف: {category}",
                mime_type='video/mp4',
                thumb_url=DEFAULT_THUMB_URL,
                reply_markup=create_rating_keyboard(message_id, chat_id)
            )
            inline_results.append(r)
        bot.answer_inline_query(inline_query.id, inline_results, cache_time=1)
    except Exception as e:
        print(f"Inline query error: {e}")

# --- نقطة انطلاق البوت ---

if __name__ == "__main__":
    init_db()
    print("Bot is starting (Complete Version)...")
    while True:
        try:
            bot.polling(non_stop=True)
        except Exception as e:
            print(f"An error occurred in the main loop: {e}")
            print("Restarting in 15 seconds...")
            time.sleep(15)
