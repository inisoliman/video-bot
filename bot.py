# ==============================================================================
# ملف: bot.py
# الوصف: الإصدار النهائي مع لوحة تحكم متكاملة للآدمن وميزة البث.
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
        c.execute('''
            CREATE TABLE IF NOT EXISTS video_archive (
                id SERIAL PRIMARY KEY,
                message_id INTEGER UNIQUE,
                caption TEXT,
                chat_id BIGINT,
                file_name TEXT,
                category TEXT DEFAULT 'Uncategorized'
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT
            )
        ''')
        # جدول جديد لتسجيل المستخدمين
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

def add_video(message_id, caption, chat_id, file_name=None, category='Uncategorized'):
    """إضافة فيديو جديد إلى قاعدة البيانات."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("""
            INSERT INTO video_archive (message_id, caption, chat_id, file_name, category) 
            VALUES (%s, %s, %s, %s, %s) 
            ON CONFLICT (message_id) DO NOTHING
        """, (message_id, caption or "No caption", chat_id, file_name or "", category))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Add video error: {e}")

def get_videos(category=None, page=0):
    """استرداد الفيديوهات من قاعدة البيانات مع نظام الصفحات."""
    offset = page * VIDEOS_PER_PAGE
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        count_query = "SELECT COUNT(*) FROM video_archive"
        data_query = "SELECT message_id, caption, chat_id, file_name, category FROM video_archive"
        params = []

        if category:
            where_clause = " WHERE category = %s"
            count_query += where_clause
            data_query += where_clause
            params.append(category)

        c.execute(count_query, params)
        total_count = c.fetchone()[0]

        data_query += " ORDER BY id LIMIT %s OFFSET %s"
        params.extend([VIDEOS_PER_PAGE, offset])
        c.execute(data_query, params)
        videos = c.fetchall()
        
        conn.close()
        return videos, total_count
    except Exception as e:
        print(f"Get videos error: {e}")
        return [], 0
        
def get_all_distinct_categories():
    """Gets all unique category names from the video archive."""
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
    """الحصول على إحصائيات المحتوى."""
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
    """البحث عن فيديوهات في قاعدة البيانات مع تطبيع الحروف العربية (البحث الذكي)."""
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

        where_clause = f"({normalize_sql('caption')} ILIKE %s OR {normalize_sql('file_name')} ILIKE %s)"
        params = [search_param, search_param]

        if category:
            where_clause += " AND category = %s"
            params.append(category)

        count_query = f"SELECT COUNT(*) FROM video_archive WHERE {where_clause}"
        c.execute(count_query, tuple(params))
        total_count = c.fetchone()[0]

        data_query = f"SELECT message_id, caption, chat_id, file_name, category FROM video_archive WHERE {where_clause} ORDER BY id LIMIT %s OFFSET %s"
        params.extend([VIDEOS_PER_PAGE, offset])
        c.execute(data_query, tuple(params))
        results = c.fetchall()
        
        conn.close()
        return results, total_count
    except Exception as e:
        print(f"Search videos error: {e}")
        return [], 0

def update_video_category(message_id, category):
    """تحديث تصنيف فيديو معين."""
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
    """حفظ أو تحديث التصنيف النشط في قاعدة البيانات."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("""
            INSERT INTO bot_settings (setting_key, setting_value) 
            VALUES ('active_category', %s)
            ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value
        """, (category_name,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Set active category error: {e}")
        return False

def get_active_category():
    """الحصول على التصنيف النشط حالياً."""
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
    """إعادة تسمية أو نقل جماعي لتصنيف."""
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
        
def delete_category_db(category_to_delete, target_category='أفلام'):
    """حذف تصنيف ونقل فيديوهاته إلى تصنيف آخر."""
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

# --- دوال مساعدة ---
def create_paginated_keyboard(items, total_items, page, prefix, context):
    """إنشاء لوحة مفاتيح مع أزرار الصفحات وزر الرجوع."""
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    for item in items:
        message_id, caption, chat_id, file_name, category = item
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
    """عرض جميع الفئات المتاحة كأزرار، بما في ذلك التصنيف النشط."""
    existing_categories = get_all_distinct_categories()
    active_category = get_active_category()
    all_possible_categories = set(existing_categories)
    all_possible_categories.add(active_category)
    
    if len(all_possible_categories) > 1 and 'Uncategorized' not in existing_categories and active_category != 'Uncategorized':
        all_possible_categories.discard('Uncategorized')

    sorted_categories = sorted(list(all_possible_categories))

    if not sorted_categories:
        text = "لا توجد أي تصنيفات متاحة حالياً."
        if edit_message: bot.answer_callback_query(edit_message.id, text)
        else: bot.reply_to(message, text)
        return

    keyboard = InlineKeyboardMarkup(row_width=2)
    buttons = [InlineKeyboardButton(text=cat, callback_data=f"cat::{cat}::0") for cat in sorted_categories]
    keyboard.add(*buttons)
    
    text = "اختر فئة لعرض فيديوهاتها:"
    if edit_message: bot.edit_message_text(text, edit_message.chat.id, edit_message.message_id, reply_markup=keyboard)
    else: bot.reply_to(message, text, reply_markup=keyboard)

# --- أوامر الآدمن ---
def generate_admin_panel():
    """إنشاء لوحة تحكم الآدمن."""
    keyboard = InlineKeyboardMarkup(row_width=2)
    btn_broadcast = InlineKeyboardButton("📢 إرسال رسالة للجميع", callback_data="admin::broadcast")
    btn_subs = InlineKeyboardButton("👤 عدد المشتركين", callback_data="admin::sub_count")
    btn_stats = InlineKeyboardButton("📊 إحصائيات المحتوى", callback_data="admin::stats")
    btn_add = InlineKeyboardButton("➕ إضافة تصنيف جديد", callback_data="admin::add_new_cat")
    btn_set_active = InlineKeyboardButton("🔘 تعيين التصنيف النشط", callback_data="admin::set_active")
    btn_rename = InlineKeyboardButton("✏️ إعادة تسمية / نقل جماعي", callback_data="admin::rename")
    btn_delete = InlineKeyboardButton("🗑️ حذف تصنيف", callback_data="admin::delete")
    btn_move_video = InlineKeyboardButton("↔️ نقل فيديو فردي", callback_data="admin::move_video")
    btn_help = InlineKeyboardButton("ℹ️ عرض المساعدة", callback_data="admin::help")
    keyboard.add(btn_broadcast, btn_subs, btn_stats, btn_add, btn_set_active, btn_rename, btn_delete, btn_move_video, btn_help)
    return keyboard

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if str(message.from_user.id) != ADMIN_ID:
        bot.reply_to(message, "هذا الأمر مخصص لصاحب البوت فقط.")
        return
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
    """دالة للتحقق من أمر الإلغاء في أي خطوة."""
    if message.text == '/cancel':
        if message.chat.id in admin_steps:
            del admin_steps[message.chat.id]
        bot.send_message(message.chat.id, "✅ تم إلغاء العملية الحالية بنجاح.")
        return True
    return False

def handle_broadcast_message(message):
    if check_cancel(message): return
    broadcast_text = message.text
    user_ids = get_all_user_ids()
    sent_count = 0
    failed_count = 0
    
    bot.send_message(message.chat.id, f"بدء إرسال الرسالة إلى {len(user_ids)} مشترك. قد تستغرق هذه العملية بعض الوقت...")
    
    for user_id in user_ids:
        try:
            bot.send_message(user_id, broadcast_text)
            sent_count += 1
        except Exception as e:
            failed_count += 1
            print(f"Failed to send to {user_id}: {e}")
        time.sleep(0.1) # لتجنب تجاوز حدود تليجرام
        
    bot.send_message(message.chat.id, f"✅ اكتمل البث!\n\n- رسائل ناجحة: {sent_count}\n- رسائل فاشلة: {failed_count}")

def handle_add_new_category(message):
    if check_cancel(message): return
    category_name = message.text.strip()
    if set_active_category(category_name):
        bot.reply_to(message, f"✅ تم إنشاء وتفعيل التصنيف الجديد بنجاح: '{category_name}'.")

def handle_rename_old(message):
    if check_cancel(message): return
    old_name = message.text.strip()
    admin_steps[message.chat.id] = {'old_category_name': old_name}
    msg = bot.send_message(message.chat.id, f"حسناً. الآن أرسل الاسم الجديد. (أو أرسل /cancel للإلغاء)")
    bot.register_next_step_handler(msg, handle_rename_new)

def handle_rename_new(message):
    if check_cancel(message): return
    new_name = message.text.strip()
    old_name = admin_steps.pop(message.chat.id, {}).get('old_category_name')
    if not old_name: return
    if rename_category_in_db(old_name, new_name):
        bot.send_message(message.chat.id, f"✅ تم تغيير اسم التصنيف من '{old_name}' إلى '{new_name}'.")

def handle_delete_category(message):
    if check_cancel(message): return
    category_to_delete = message.text.strip()
    if delete_category_db(category_to_delete):
        bot.send_message(message.chat.id, f"✅ تم حذف التصنيف '{category_to_delete}' بنجاح ونقل محتوياته إلى 'أفلام'.")

def handle_move_video_forward(message):
    if check_cancel(message): return
    if not message.forward_from_message_id:
        bot.send_message(message.chat.id, "خطأ: يرجى إعادة توجيه الرسالة. (أو أرسل /cancel للإلغاء)")
        bot.register_next_step_handler(message, handle_move_video_forward)
        return
    original_message_id = message.forward_from_message_id
    admin_steps[message.chat.id] = {'video_to_move_id': original_message_id}
    msg = bot.send_message(message.chat.id, "ممتاز. الآن أرسل اسم التصنيف الجديد. (أو أرسل /cancel للإلغاء)")
    bot.register_next_step_handler(msg, handle_move_video_new_cat)

def handle_move_video_new_cat(message):
    if check_cancel(message): return
    new_category_name = message.text.strip()
    video_message_id = admin_steps.pop(message.chat.id, {}).get('video_to_move_id')
    if not video_message_id: return
    if update_video_category(video_message_id, new_category_name):
        bot.send_message(message.chat.id, f"✅ تم نقل الفيديو بنجاح إلى '{new_category_name}'.")

# --- معالجات الرسائل العامة ---

@bot.message_handler(content_types=['text'])
def handle_text_search(message):
    """يعرض خيارات البحث للمستخدم."""
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
        print(f"New video detected. Assigning to active category: '{active_category}'. Message ID: {message.message_id}")
        add_video(
            message_id=message.message_id,
            caption=message.caption,
            chat_id=message.chat.id,
            file_name=message.video.file_name if message.video else "",
            category=active_category
        )

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
                msg = bot.send_message(call.message.chat.id, "أرسل الرسالة التي تريد بثها لجميع المشتركين. (أو أرسل /cancel للإلغاء)")
                bot.register_next_step_handler(msg, handle_broadcast_message)

            elif sub_action == "sub_count":
                count = get_subscriber_count()
                bot.send_message(call.message.chat.id, f"👤 إجمالي عدد المشتركين في البوت: *{count}*", parse_mode='Markdown')
            
            elif sub_action == "stats":
                video_count, category_count = get_bot_stats()
                stats_text = f"📊 *إحصائيات المحتوى*\n\n- إجمالي الفيديوهات: *{video_count}*\n- إجمالي التصنيفات: *{category_count}*"
                bot.send_message(call.message.chat.id, stats_text, parse_mode='Markdown')
            
            elif sub_action == "add_new_cat":
                msg = bot.send_message(call.message.chat.id, "أرسل اسم التصنيف الجديد الذي تريد إنشاءه. (أو أرسل /cancel للإلغاء)")
                bot.register_next_step_handler(msg, handle_add_new_category)

            elif sub_action == "set_active":
                categories = get_all_distinct_categories()
                if not categories:
                    bot.answer_callback_query(call.id, "لا توجد تصنيفات حالياً. قم بإنشاء واحد أولاً.")
                    return
                keyboard = InlineKeyboardMarkup(row_width=2)
                buttons = [InlineKeyboardButton(text=cat, callback_data=f"admin::setcat::{cat}") for cat in categories]
                keyboard.add(*buttons)
                bot.edit_message_text("اختر التصنيف الذي تريد تفعيله:", call.message.chat.id, call.message.message_id, reply_markup=keyboard)

            elif sub_action == "setcat":
                category_name = data[2]
                if set_active_category(category_name):
                    bot.edit_message_text(f"✅ تم تفعيل التصنيف '{category_name}' بنجاح.", call.message.chat.id, call.message.message_id)
                
            elif sub_action == "rename":
                msg = bot.send_message(call.message.chat.id, "أرسل اسم التصنيف المصدر (القديم). (أو أرسل /cancel للإلغاء)")
                bot.register_next_step_handler(msg, handle_rename_old)
            elif sub_action == "delete":
                msg = bot.send_message(call.message.chat.id, "أرسل اسم التصنيف الذي تريد حذفه. (أو أرسل /cancel للإلغاء)")
                bot.register_next_step_handler(msg, handle_delete_category)
            elif sub_action == "move_video":
                msg = bot.send_message(call.message.chat.id, "قم بإعادة توجيه الفيديو الذي تريد نقله. (أو أرسل /cancel للإلغاء)")
                bot.register_next_step_handler(msg, handle_move_video_forward)
            elif sub_action == "help":
                help_text = "قائمة أوامر الإدارة:\n- إحصائيات البوت\n- تعيين التصنيف النشط\n- إعادة تسمية / نقل جماعي\n- حذف تصنيف\n- نقل فيديو فردي"
                bot.send_message(call.message.chat.id, help_text)
            return

        if action == "back_to_cats":
            list_videos(call.message, edit_message=call.message)
            bot.answer_callback_query(call.id)
            return

        if action == "video":
            _, message_id, chat_id = data
            bot.copy_message(call.message.chat.id, chat_id, int(message_id))
            bot.answer_callback_query(call.id, "جاري إرسال الفيديو...")
        
        elif action == "cat":
            _, category, page_str = data
            page = int(page_str)
            videos, total_count = get_videos(category, page)
            keyboard = create_paginated_keyboard(videos, total_count, page, "cat", category)
            bot.edit_message_text(f"الفيديوهات في فئة '{category}':", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
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
                category = scope
                videos, total_count = search_videos(query, page=page, category=category)
                if not videos:
                    bot.edit_message_text(f"لم يتم العثور على نتائج للبحث عن '{query}' في فئة '{category}'.", call.message.chat.id, call.message.message_id)
                    return
                keyboard = create_paginated_keyboard(videos, total_count, page, "search_cat", category)
                bot.edit_message_text(f"نتائج البحث عن '{query}' في فئة '{category}':", call.message.chat.id, call.message.message_id, reply_markup=keyboard)

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
            _, category, page_str = data
            page = int(page_str)
            query = user_last_search.get(call.message.chat.id)
            if not query:
                bot.answer_callback_query(call.id, "انتهت صلاحية البحث، يرجى البحث مرة أخرى.")
                return
            videos, total_count = search_videos(query, page=page, category=category)
            keyboard = create_paginated_keyboard(videos, total_count, page, "search_cat", category)
            bot.edit_message_text(f"نتائج البحث عن '{query}' في فئة '{category}':", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
            bot.answer_callback_query(call.id)
        
        elif action == "noop":
            bot.answer_callback_query(call.id)

    except Exception as e:
        print(f"Callback query error: {e}")
        bot.answer_callback_query(call.id, "حدث خطأ.")

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
