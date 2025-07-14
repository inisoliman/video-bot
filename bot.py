# ==============================================================================
# ملف: bot.py
# الوصف: الإصدار النهائي مع نظام التصنيف النشط والبحث الذكي.
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
        conn.commit()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Database error during init: {e}")

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

def search_videos(query, page=0):
    """البحث عن فيديوهات في قاعدة البيانات مع تطبيع الحروف العربية (البحث الذكي)."""
    offset = page * VIDEOS_PER_PAGE
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        
        search_param = '%' + query + '%'

        def normalize_sql(column_name):
            step1 = f"REPLACE({column_name}, 'أ', 'ا')"
            step2 = f"REPLACE({step1}, 'إ', 'ا')"
            step3 = f"REPLACE({step2}, 'آ', 'ا')"
            step4 = f"REPLACE({step3}, 'ة', 'ه')"
            step5 = f"REPLACE({step4}, 'ى', 'ي')"
            return step5

        normalized_caption = normalize_sql("caption")
        normalized_file_name = normalize_sql("file_name")
        normalized_search_param = normalize_sql("%s")

        count_query = f"SELECT COUNT(*) FROM video_archive WHERE {normalized_caption} ILIKE {normalized_search_param} OR {normalized_file_name} ILIKE {normalized_search_param}"
        c.execute(count_query, (search_param, search_param))
        total_count = c.fetchone()[0]

        data_query = f"SELECT message_id, caption, chat_id, file_name, category FROM video_archive WHERE {normalized_caption} ILIKE {normalized_search_param} OR {normalized_file_name} ILIKE {normalized_search_param} ORDER BY id LIMIT %s OFFSET %s"
        c.execute(data_query, (search_param, search_param, VIDEOS_PER_PAGE, offset))
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

# --- دوال مساعدة ---
def create_paginated_keyboard(items, total_items, page, prefix, query_or_cat):
    """إنشاء لوحة مفاتيح مع أزرار الصفحات وزر الرجوع."""
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    for item in items:
        message_id, caption, chat_id, file_name, category = item
        title = caption or file_name or "فيديو بدون عنوان"
        keyboard.add(InlineKeyboardButton(text=f"{title[:50]} ({category})", callback_data=f"video_{message_id}_{chat_id}"))

    total_pages = math.ceil(total_items / VIDEOS_PER_PAGE)
    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"{prefix}_{query_or_cat}_{page - 1}"))
        
        nav_buttons.append(InlineKeyboardButton(f"صفحة {page + 1}/{total_pages}", callback_data="noop"))

        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("التالي ➡️", callback_data=f"{prefix}_{query_or_cat}_{page + 1}"))
        
        keyboard.row(*nav_buttons)
    
    # إضافة زر الرجوع إلى قائمة التصنيفات الرئيسية
    keyboard.row(InlineKeyboardButton("الرجوع إلى التصنيفات", callback_data="back_to_cats"))
        
    return keyboard

# --- أوامر البوت ---

@bot.message_handler(commands=['start'])
def start(message):
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
    """عرض جميع الفئات المتاحة كأزرار، مع إمكانية تعديل رسالة موجودة."""
    existing_categories = get_all_distinct_categories()
    active_category = get_active_category()
    all_possible_categories = set(existing_categories)
    all_possible_categories.add(active_category)
    
    if len(all_possible_categories) > 1 and 'Uncategorized' not in existing_categories and active_category != 'Uncategorized':
        all_possible_categories.discard('Uncategorized')

    sorted_categories = sorted(list(all_possible_categories))

    if not sorted_categories:
        if edit_message:
            bot.answer_callback_query(edit_message.id, "لا توجد أي تصنيفات متاحة حالياً.")
        else:
            bot.reply_to(message, "لا توجد أي تصنيفات متاحة حالياً.")
        return

    keyboard = InlineKeyboardMarkup(row_width=2)
    buttons = [InlineKeyboardButton(text=cat, callback_data=f"cat_{cat}_0") for cat in sorted_categories]
    keyboard.add(*buttons)
    
    text = "اختر فئة لعرض فيديوهاتها:"
    if edit_message:
        bot.edit_message_text(text, edit_message.chat.id, edit_message.message_id, reply_markup=keyboard)
    else:
        bot.reply_to(message, text, reply_markup=keyboard)

# --- أوامر الآدمن ---
@bot.message_handler(commands=['help_admin'])
def help_admin(message):
    if str(message.from_user.id) != ADMIN_ID: return
    help_text = "قائمة أوامر الإدارة:\n/set_category <اسم>\n/current_category\n/rename_category\n/bulk_move\n/move_video\n/add_category"
    bot.send_message(message.chat.id, help_text)

@bot.message_handler(commands=['set_category', 'current_category', 'add_category', 'rename_category', 'bulk_move', 'move_video'])
def admin_commands(message):
    if str(message.from_user.id) != ADMIN_ID:
        bot.reply_to(message, "هذا الأمر مخصص لصاحب البوت فقط.")
        return
    
    command = message.text.split()[0]
    if command == '/set_category':
        try:
            category_name = message.text.split(maxsplit=1)[1].strip()
            if set_active_category(category_name):
                bot.reply_to(message, f"✅ تم تعيين التصنيف النشط إلى '{category_name}'.")
        except IndexError:
            bot.reply_to(message, "الاستخدام: /set_category <اسم التصنيف>")
    elif command == '/current_category':
        active_category = get_active_category()
        bot.reply_to(message, f"ℹ️ التصنيف النشط حالياً هو: '{active_category}'.")
    elif command in ['/add_category', '/move_video']:
        action = "تصنيفه يدوياً" if command == '/add_category' else "نقل تصنيفه"
        msg = bot.send_message(message.chat.id, f"من فضلك، قم بإعادة توجيه الفيديو الذي تريد {action}.")
        if command == '/add_category':
            bot.register_next_step_handler(msg, process_forwarded_video)
        else:
            bot.register_next_step_handler(msg, process_video_to_move)
    elif command in ['/rename_category', '/bulk_move']:
        msg = bot.send_message(message.chat.id, "من فضلك, أرسل اسم التصنيف المصدر (الذي تريد نقله أو تغيير اسمه).")
        bot.register_next_step_handler(msg, process_old_category_name)

def process_forwarded_video(message):
    if not message.forward_from_message_id:
        bot.send_message(message.chat.id, "خطأ: يرجى إعادة توجيه الرسالة.")
        return
    original_message_id = message.forward_from_message_id
    admin_steps[message.chat.id] = {'video_message_id': original_message_id}
    msg = bot.send_message(message.chat.id, "ممتاز. الآن أرسل اسم التصنيف الجديد.")
    bot.register_next_step_handler(msg, process_category_name)

def process_category_name(message):
    category_name = message.text.strip()
    video_message_id = admin_steps.pop(message.chat.id, {}).get('video_message_id')
    if not video_message_id: return
    if update_video_category(video_message_id, category_name):
        bot.send_message(message.chat.id, f"✅ تم تحديث تصنيف الفيديو بنجاح إلى '{category_name}'.")

def process_video_to_move(message):
    if not message.forward_from_message_id:
        bot.send_message(message.chat.id, "خطأ: يرجى إعادة توجيه الرسالة.")
        return
    original_message_id = message.forward_from_message_id
    admin_steps[message.chat.id] = {'video_to_move_id': original_message_id}
    msg = bot.send_message(message.chat.id, "ممتاز. الآن أرسل اسم التصنيف الجديد.")
    bot.register_next_step_handler(msg, process_new_category_for_move)

def process_new_category_for_move(message):
    new_category_name = message.text.strip()
    video_message_id = admin_steps.pop(message.chat.id, {}).get('video_to_move_id')
    if not video_message_id: return
    if update_video_category(video_message_id, new_category_name):
        bot.send_message(message.chat.id, f"✅ تم نقل الفيديو بنجاح إلى '{new_category_name}'.")

def process_old_category_name(message):
    old_name = message.text.strip()
    admin_steps[message.chat.id] = {'old_category_name': old_name}
    msg = bot.send_message(message.chat.id, f"حسناً. الآن أرسل الاسم الجديد.")
    bot.register_next_step_handler(msg, process_new_category_name)

def process_new_category_name(message):
    new_name = message.text.strip()
    old_name = admin_steps.pop(message.chat.id, {}).get('old_category_name')
    if not old_name: return
    if rename_category_in_db(old_name, new_name):
        bot.send_message(message.chat.id, f"✅ تم تغيير اسم التصنيف من '{old_name}' إلى '{new_name}'.")

# --- معالجات الرسائل العامة ---

@bot.message_handler(content_types=['text'])
def handle_text_search(message):
    """البحث التلقائي عند إرسال أي نص."""
    query = message.text.strip()
    if not query or query.startswith('/'):
        return
    
    user_last_search[message.chat.id] = query
    videos, total_count = search_videos(query, page=0)

    if not videos:
        bot.reply_to(message, f"لم يتم العثور على نتائج للبحث عن '{query}'.")
        return

    keyboard = create_paginated_keyboard(videos, total_count, 0, "search", "query")
    bot.reply_to(message, f"نتائج البحث عن '{query}':", reply_markup=keyboard)

@bot.message_handler(content_types=['video'])
def handle_new_video(message):
    """حفظ أي فيديو جديد يتم إرساله إلى القناة المراقبة بالتصنيف النشط."""
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
        if call.data == "back_to_cats":
            list_videos(call.message, edit_message=call.message)
            bot.answer_callback_query(call.id)
            return

        data = call.data.split('_')
        action = data[0]

        if action == "video":
            _, message_id, chat_id = data
            bot.copy_message(call.message.chat.id, chat_id, int(message_id))
            bot.answer_callback_query(call.id, "جاري إرسال الفيديو...")
        
        elif action == "cat":
            # The category name might contain underscores, so we join them back
            category = "_".join(data[1:-1])
            page_str = data[-1]
            page = int(page_str)
            videos, total_count = get_videos(category, page)
            keyboard = create_paginated_keyboard(videos, total_count, page, "cat", category)
            bot.edit_message_text(f"الفيديوهات في فئة '{category}':", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
            bot.answer_callback_query(call.id)

        elif action == "search":
            _, _, page_str = data
            page = int(page_str)
            query = user_last_search.get(call.message.chat.id)
            if not query:
                bot.answer_callback_query(call.id, "انتهت صلاحية البحث، يرجى البحث مرة أخرى.")
                return

            videos, total_count = search_videos(query, page)
            keyboard = create_paginated_keyboard(videos, total_count, page, "search", "query")
            bot.edit_message_text(f"نتائج البحث عن '{query}':", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
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
