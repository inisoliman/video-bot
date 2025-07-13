# ==============================================================================
# ملف: bot.py
# الوصف: هذا هو البوت الرئيسي الذي يعمل بشكل دائم للرد على المستخدمين
# وحفظ الفيديوهات الجديدة تلقائياً، مع ميزات خاصة بالآدمن.
# ==============================================================================

import telebot
import psycopg2
import os
import time
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from urllib.parse import urlparse

# --- الإعدادات الأساسية (قراءة آمنة من متغيرات البيئة) ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
DATABASE_URL = os.getenv('DATABASE_URL')
CHANNEL_ID = os.getenv('CHANNEL_ID')
ADMIN_ID = os.getenv('ADMIN_ID') # معرف حساب الآدمن

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

# قاموس مؤقت لتخزين بيانات عملية التصنيف
admin_steps = {}

# --- دوال قاعدة البيانات ---

def init_db():
    """إنشاء جدول الفيديوهات إذا لم يكن موجوداً."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        # تم تغيير اسم الجدول إلى video_archive لإصلاح خطأ حجم البيانات
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

def get_videos(category=None):
    """استرداد الفيديوهات من قاعدة البيانات."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        if category:
            c.execute("SELECT message_id, caption, chat_id, file_name, category FROM video_archive WHERE category = %s ORDER BY id", (category,))
        else:
            c.execute("SELECT message_id, caption, chat_id, file_name, category FROM video_archive ORDER BY id")
        videos = c.fetchall()
        conn.close()
        return videos
    except Exception as e:
        print(f"Get videos error: {e}")
        return []

def search_videos(query):
    """البحث عن فيديوهات في قاعدة البيانات."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        search_query = '%' + query + '%'
        c.execute("SELECT message_id, caption, chat_id, file_name, category FROM video_archive WHERE caption ILIKE %s OR file_name ILIKE %s ORDER BY id",
                  (search_query, search_query))
        results = c.fetchall()
        conn.close()
        return results
    except Exception as e:
        print(f"Search videos error: {e}")
        return []

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

# --- إعداد القائمة الرئيسية ---
def main_menu():
    """إنشاء القائمة الرئيسية للبوت."""
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    list_button = KeyboardButton('🎬 عرض كل الفيديوهات')
    markup.add(list_button)
    return markup

# --- أوامر البوت ---

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "أهلاً بك في بوت البحث عن الفيديوهات!", reply_markup=main_menu())

@bot.message_handler(func=lambda message: message.text == '🎬 عرض كل الفيديوهات')
def handle_list_videos_button(message):
    """معالجة الضغط على زر عرض الفيديوهات."""
    list_videos(message)

def list_videos(message):
    """عرض جميع الفئات المتاحة كأزرار."""
    all_videos = get_videos()
    if not all_videos:
        bot.reply_to(message, "لم يتم العثور على أي فيديوهات في قاعدة البيانات.")
        return
    
    categories = sorted(list(set(video[4] for video in all_videos)))
    if not categories:
        bot.reply_to(message, "لا توجد فئات محددة.")
        return

    keyboard = InlineKeyboardMarkup(row_width=2)
    buttons = [InlineKeyboardButton(text=cat, callback_data=f"cat_{cat}") for cat in categories]
    keyboard.add(*buttons)
    bot.reply_to(message, "اختر فئة لعرض فيديوهاتها:", reply_markup=keyboard)

# --- أوامر الآدمن ---

@bot.message_handler(commands=['add_category'])
def add_category_start(message):
    """بدء عملية إضافة تصنيف جديد (للآدمن فقط)."""
    if str(message.from_user.id) != ADMIN_ID:
        bot.reply_to(message, "هذا الأمر مخصص لصاحب البوت فقط.")
        return
    
    msg = bot.send_message(message.chat.id, "من فضلك، قم بإعادة توجيه (Forward) الفيديو الذي تريد تصنيفه.")
    bot.register_next_step_handler(msg, process_forwarded_video)

def process_forwarded_video(message):
    """معالجة الفيديو المعاد توجيهه."""
    if not message.forward_from_message_id:
        bot.send_message(message.chat.id, "خطأ: يرجى إعادة توجيه رسالة الفيديو الأصلية من القناة.")
        return

    original_message_id = message.forward_from_message_id
    admin_steps[message.chat.id] = {'video_message_id': original_message_id}
    
    msg = bot.send_message(message.chat.id, "ممتاز. الآن أرسل اسم التصنيف الجديد لهذا الفيديو.")
    bot.register_next_step_handler(msg, process_category_name)

def process_category_name(message):
    """معالجة اسم التصنيف وتحديث قاعدة البيانات."""
    category_name = message.text.strip()
    video_message_id = admin_steps.get(message.chat.id, {}).get('video_message_id')

    if not video_message_id:
        bot.send_message(message.chat.id, "حدث خطأ ما، يرجى المحاولة مرة أخرى باستخدام /add_category.")
        return

    if update_video_category(video_message_id, category_name):
        bot.send_message(message.chat.id, f"✅ تم تحديث تصنيف الفيديو بنجاح إلى '{category_name}'.")
    else:
        bot.send_message(message.chat.id, "حدث خطأ أثناء تحديث قاعدة البيانات.")
    
    if message.chat.id in admin_steps:
        del admin_steps[message.chat.id]

# --- معالجات الرسائل العامة ---

@bot.message_handler(content_types=['text'])
def handle_text_search(message):
    """البحث التلقائي عند إرسال أي نص."""
    query = message.text.strip()
    if not query or query.startswith('/'):
        return

    results = search_videos(query)
    if not results:
        bot.reply_to(message, f"لم يتم العثور على نتائج للبحث عن '{query}'.")
        return

    keyboard = InlineKeyboardMarkup()
    for video in results[:25]:
        message_id, caption, chat_id, file_name, category = video
        title = caption or file_name or "فيديو بدون عنوان"
        keyboard.add(InlineKeyboardButton(text=f"{title[:50]} ({category})", callback_data=f"video_{message_id}_{chat_id}"))
    
    bot.reply_to(message, f"نتائج البحث عن '{query}':", reply_markup=keyboard)

@bot.message_handler(content_types=['video'])
def handle_new_video(message):
    """حفظ أي فيديو جديد يتم إرساله إلى القناة المراقبة."""
    if str(message.chat.id) == CHANNEL_ID:
        print(f"New video detected in channel {CHANNEL_ID}. Message ID: {message.message_id}")
        add_video(
            message_id=message.message_id,
            caption=message.caption,
            chat_id=message.chat.id,
            file_name=message.video.file_name if message.video else ""
        )

# --- معالج ضغطات الأزرار ---

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    """الاستجابة عند الضغط على الأزرار."""
    if call.data.startswith("video_"):
        _, message_id, chat_id = call.data.split('_')
        try:
            bot.copy_message(call.message.chat.id, chat_id, int(message_id))
            bot.answer_callback_query(call.id, "جاري إرسال الفيديو...")
        except Exception as e:
            bot.answer_callback_query(call.id, "حدث خطأ أثناء إرسال الفيديو.")
            print(f"Error sending video: {str(e)}")

    elif call.data.startswith("cat_"):
        category = call.data.replace("cat_", "")
        videos_in_category = get_videos(category)
        if not videos_in_category:
            bot.answer_callback_query(call.id, f"لا توجد فيديوهات في فئة '{category}'.")
            return

        keyboard = InlineKeyboardMarkup(row_width=1)
        buttons = [InlineKeyboardButton(text=(v[1] or v[3] or "فيديو بدون عنوان")[:50], callback_data=f"video_{v[0]}_{v[2]}") for v in videos_in_category[:25]]
        keyboard.add(*buttons)
        
        try:
            bot.edit_message_text(f"الفيديوهات في فئة '{category}':", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
            bot.answer_callback_query(call.id)
        except Exception as e:
            print(f"Error editing message: {e}")
            bot.answer_callback_query(call.id, "حدث خطأ.")

# --- نقطة انطلاق البوت ---

if __name__ == "__main__":
    if not all([BOT_TOKEN, DATABASE_URL, CHANNEL_ID, ADMIN_ID]):
        print("Error: Missing one or more environment variables (BOT_TOKEN, DATABASE_URL, CHANNEL_ID, ADMIN_ID).")
    else:
        init_db()
        print("Bot is running...")
        while True:
            try:
                bot.polling(non_stop=True)
            except Exception as e:
                print(f"Bot polling error: {e}. Restarting in 15 seconds...")
                time.sleep(15)
