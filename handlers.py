import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
import os
import time
import re
import json
from urllib.parse import urlparse
import math
from datetime import datetime

from db_manager import (
    init_db, migrate_old_data, add_category, get_categories_tree, get_child_categories,
    get_category_by_id, add_video, get_videos, increment_video_view_count,
    get_video_by_message_id, get_active_category_id, set_active_category_id,
    add_video_rating, get_video_rating_stats, get_user_video_rating,
    get_popular_videos, add_bot_user, get_all_user_ids, get_subscriber_count,
    get_bot_stats, search_videos, add_required_channel, remove_required_channel,
    get_required_channels, DB_CONFIG, admin_steps, user_last_search, VIDEOS_PER_PAGE, CALLBACK_DELIMITER
)
from utils import extract_video_metadata, get_video_info

# تعريف المتغيرات العامة التي سيتم تمريرها من bot.py
bot = None
CHANNEL_ID = None
ADMIN_IDS = []

def register_handlers(telebot_instance, channel_id, admin_ids):
    global bot, CHANNEL_ID, ADMIN_IDS
    bot = telebot_instance
    CHANNEL_ID = channel_id
    ADMIN_IDS = admin_ids

    # --- وظائف مساعدة للوحة المفاتيح ---

    def main_menu():
        markup = ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(KeyboardButton("🎬 عرض كل الفيديوهات"))
        markup.add(KeyboardButton("🔥 الفيديوهات الشائعة"))
        markup.add(KeyboardButton("🔍 بحث"))
        return markup

    def create_categories_keyboard(parent_id=None):
        keyboard = InlineKeyboardMarkup(row_width=2)
        categories = get_child_categories(parent_id)
        buttons = []
        for cat_id, cat_name, _, _ in categories:
            buttons.append(InlineKeyboardButton(cat_name, callback_data=f"cat::{cat_id}::0"))
        keyboard.add(*buttons)
        if parent_id:
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data=f"cat::{get_category_by_id(parent_id)[2]}::0"))
        return keyboard

    def create_paginated_keyboard(videos, total_count, current_page, action_prefix, context_id):
        keyboard = InlineKeyboardMarkup(row_width=1)
        for video in videos:
            video_id, message_id, caption, chat_id, video_info_json, views, avg_rating, total_ratings = video
            video_info = json.loads(video_info_json) if video_info_json else {}
            
            title = caption.split('\n')[0] if caption else f"فيديو {video_id}"
            
            info_text = ""
            if video_info:
                if video_info.get("quality_resolution") and video_info.get("duration"):
                    info_text = f" ({video_info['quality_resolution']} | {video_info['duration']})"
                elif video_info.get("quality_resolution"):
                    info_text = f" ({video_info['quality_resolution']})"
                elif video_info.get("duration"):
                    info_text = f" ({video_info['duration']})"
            
            rating_text = ""
            if avg_rating is not None:
                rating_text = f" ⭐ {avg_rating:.1f}/5"
            
            views_text = f" 👁️ {views}"

            keyboard.add(InlineKeyboardButton(
                f"{title}{info_text}{rating_text}{views_text}", 
                callback_data=f"video::{video_id}::{message_id}::{chat_id}"
            ))

        # أزرار التنقل
        nav_buttons = []
        if current_page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"{action_prefix}::{context_id}::{current_page - 1}"))
        
        total_pages = math.ceil(total_count / VIDEOS_PER_PAGE) - 1
        if current_page < total_pages:
            nav_buttons.append(InlineKeyboardButton("التالي ➡️", callback_data=f"{action_prefix}::{context_id}::{current_page + 1}"))
        
        if nav_buttons:
            keyboard.add(*nav_buttons)
        
        keyboard.add(InlineKeyboardButton("🔙 رجوع للتصنيفات", callback_data="back_to_cats"))
        return keyboard

    def create_video_action_keyboard(video_id, user_id):
        keyboard = InlineKeyboardMarkup(row_width=5)
        user_rating = get_user_video_rating(video_id, user_id)
        
        buttons = []
        for i in range(1, 6):
            star = "⭐" if user_rating == i else "☆"
            buttons.append(InlineKeyboardButton(star, callback_data=f"rate::{video_id}::{i}"))
        keyboard.add(*buttons)
        
        stats = get_video_rating_stats(video_id)
        if stats:
            avg_rating = stats[0]
            total_ratings = stats[1]
            keyboard.add(InlineKeyboardButton(f"متوسط التقييم: {avg_rating:.1f} ({total_ratings} تقييم)", callback_data="noop"))
        
        return keyboard

    def check_admin(func):
        def wrapper(message):
            if message.from_user.id in ADMIN_IDS:
                return func(message)
            else:
                bot.reply_to(message, "ليس لديك صلاحية الوصول إلى هذا الأمر.")
        return wrapper

    def check_subscription(user_id, channel_id):
        try:
            member = bot.get_chat_member(channel_id, user_id)
            return member.status in ['member', 'administrator', 'creator']
        except telebot.apihelper.ApiTelegramException as e:
            if e.result_json['description'] == 'Bad Request: chat not found':
                print(f"Channel {channel_id} not found. Please remove it from required channels.")
                return True # Treat as subscribed if channel not found to avoid blocking users
            return False

    # --- أوامر البوت ---

    @bot.message_handler(commands=["start"])
    def start(message):
        add_bot_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
        
        required_channels = get_required_channels()
        if required_channels:
            not_subscribed_channels = []
            for channel_id, channel_name in required_channels:
                if not check_subscription(message.from_user.id, channel_id):
                    not_subscribed_channels.append((channel_id, channel_name))
            
            if not_subscribed_channels:
                markup = InlineKeyboardMarkup()
                for channel_id, channel_name in not_subscribed_channels:
                    markup.add(InlineKeyboardButton(f"اشترك في {channel_name}", url=f"https://t.me/c/{str(channel_id).replace("-100", ")}"))
                bot.reply_to(message, "يرجى الاشتراك في القنوات التالية لاستخدام البوت:", reply_markup=markup)
                return

        bot.reply_to(message, "أهلاً بك في بوت البحث عن الفيديوهات!", reply_markup=main_menu())

    @bot.message_handler(commands=["myid"])
    def get_my_id(message):
        bot.reply_to(message, f"معرف حسابك هو: `{message.from_user.id}`", parse_mode="Markdown")

    @bot.message_handler(func=lambda message: message.text == "🎬 عرض كل الفيديوهات")
    def handle_list_videos_button(message):
        list_videos(message)

    @bot.message_handler(func=lambda message: message.text == "🔥 الفيديوهات الشائعة")
    def handle_popular_videos_button(message):
        show_popular_videos(message)

    @bot.message_handler(func=lambda message: message.text == "🔍 بحث")
    def handle_search_button(message):
        msg = bot.reply_to(message, "أرسل الكلمة المفتاحية للبحث عن الفيديوهات:")
        bot.register_next_step_handler(msg, handle_text_search)

    def show_popular_videos(message):
        """عرض الفيديوهات الشائعة."""
        popular = get_popular_videos()
        
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.add(InlineKeyboardButton("📈 الأكثر مشاهدة", callback_data="popular::most_viewed"))
        keyboard.add(InlineKeyboardButton("⭐ الأعلى تقييماً", callback_data="popular::highest_rated"))
        
        bot.reply_to(message, "اختر نوع الفيديوهات الشائعة:", reply_markup=keyboard)

    def list_videos(message, edit_message=None, parent_id=None):
        """عرض التصنيفات المتاحة."""
        keyboard = create_categories_keyboard(parent_id)
        
        if keyboard.keyboard:  # إذا كان هناك تصنيفات
            text = "اختر تصنيفًا لعرض محتوياته:"
            if edit_message:
                bot.edit_message_text(text, edit_message.chat.id, edit_message.message_id, reply_markup=keyboard)
            else:
                bot.reply_to(message, text, reply_markup=keyboard)
        else:
            text = "لا توجد تصنيفات متاحة حالياً."
            if edit_message:
                bot.answer_callback_query(edit_message.id, text)
            else:
                bot.reply_to(message, text)

    # --- لوحة تحكم الآدمن ---

    def generate_admin_panel():
        """إنشاء لوحة تحكم الآدمن."""
        keyboard = InlineKeyboardMarkup(row_width=2)
        btn_broadcast = InlineKeyboardButton("📢 إرسال رسالة للجميع", callback_data="admin::broadcast")
        btn_subs = InlineKeyboardButton("👤 عدد المشتركين", callback_data="admin::sub_count")
        btn_stats = InlineKeyboardButton("📊 إحصائيات المحتوى", callback_data="admin::stats")
        btn_add_cat = InlineKeyboardButton("➕ إضافة تصنيف جديد", callback_data="admin::add_new_cat")
        btn_set_active = InlineKeyboardButton("🔘 تعيين التصنيف النشط", callback_data="admin::set_active")
        btn_help = InlineKeyboardButton("ℹ️ عرض المساعدة", callback_data="admin::help")
        btn_add_channel = InlineKeyboardButton("➕ إضافة قناة مطلوبة", callback_data="admin::add_channel")
        btn_remove_channel = InlineKeyboardButton("➖ إزالة قناة مطلوبة", callback_data="admin::remove_channel")
        btn_list_channels = InlineKeyboardButton("📋 عرض القنوات المطلوبة", callback_data="admin::list_channels")
        
        keyboard.add(btn_broadcast, btn_subs, btn_stats, btn_add_cat, btn_set_active, btn_help, btn_add_channel, btn_remove_channel, btn_list_channels)
        return keyboard

    @bot.message_handler(commands=["admin"])
    @check_admin
    def admin_panel(message):
        bot.send_message(message.chat.id, "أهلاً بك في لوحة تحكم الآدمن. اختر أحد الخيارات:", reply_markup=generate_admin_panel())

    @bot.message_handler(commands=["cancel"])
    @check_admin
    def cancel_step(message):
        if message.chat.id in admin_steps:
            del admin_steps[message.chat.id]
            bot.send_message(message.chat.id, "✅ تم إلغاء العملية الحالية بنجاح.")
        else:
            bot.send_message(message.chat.id, "لا توجد عملية لإلغائها.")

    # --- معالجات خطوات الآدمن ---

    def check_cancel(message):
        """دالة للتحقق من أمر الإلغاء."""
        if message.text == "/cancel":
            if message.chat.id in admin_steps:
                del admin_steps[message.chat.id]
            bot.send_message(message.chat.id, "تم إلغاء العملية.")
            return True
        return False

    def handle_rich_broadcast(message):
        """معالج البث الغني (نص، صور، فيديوهات)."""
        if check_cancel(message): return
        
        user_ids = get_all_user_ids()
        sent_count = 0
        failed_count = 0
        
        bot.send_message(message.chat.id, f"بدء إرسال الرسالة إلى {len(user_ids)} مشترك. قد تستغرق هذه العملية بعض الوقت...")
        
        for user_id in user_ids:
            try:
                if message.text:
                    bot.send_message(user_id, message.text, parse_mode=message.parse_mode, entities=message.entities)
                elif message.photo:
                    bot.send_photo(user_id, message.photo[-1].file_id, caption=message.caption, parse_mode=message.parse_mode, caption_entities=message.caption_entities)
                elif message.video:
                    bot.send_video(user_id, message.video.file_id, caption=message.caption, parse_mode=message.parse_mode, caption_entities=message.caption_entities)
                elif message.document:
                    bot.send_document(user_id, message.document.file_id, caption=message.caption, parse_mode=message.parse_mode, caption_entities=message.caption_entities)
                elif message.audio:
                    bot.send_audio(user_id, message.audio.file_id, caption=message.caption, parse_mode=message.parse_mode, caption_entities=message.caption_entities)
                elif message.voice:
                    bot.send_voice(user_id, message.voice.file_id, caption=message.caption, parse_mode=message.parse_mode, caption_entities=message.caption_entities)
                else:
                    # Fallback for unsupported message types
                    bot.send_message(message.chat.id, f"نوع الرسالة غير مدعوم للبث: {message.content_type}")
                    failed_count += 1
                    continue
                sent_count += 1
            except Exception as e:
                failed_count += 1
                print(f"Failed to send to {user_id}: {e}")
            time.sleep(0.1) # لتجنب تجاوز حدود تليجرام
            
        bot.send_message(message.chat.id, f"✅ اكتمل البث!\n\n- رسائل ناجحة: {sent_count}\n- رسائل فاشلة: {failed_count}")

    def handle_add_new_category(message):
        if check_cancel(message): return
        category_name = message.text.strip()
        
        # يمكن تحسين هذا لاحقًا لدعم التصنيفات الفرعية
        success, result = add_category(category_name)
        if success:
            set_active_category_id(result)
            bot.reply_to(message, f"✅ تم إنشاء وتفعيل التصنيف الجديد بنجاح: \"{category_name}\".")
        else:
            bot.reply_to(message, f"❌ خطأ في إنشاء التصنيف: {result}")

    # --- معالجات القنوات المطلوبة ---

    def handle_add_channel_step1(message):
        if check_cancel(message): return
        try:
            channel_id = int(message.text.strip())
            admin_steps[message.chat.id] = {"channel_id": channel_id}
            msg = bot.send_message(message.chat.id, "الآن أرسل اسم القناة (مثال: قناة الأفلام). (أو أرسل /cancel للإلغاء)")
            bot.register_next_step_handler(msg, handle_add_channel_step2)
        except ValueError:
            msg = bot.send_message(message.chat.id, "معرف القناة غير صالح. يرجى إرسال رقم صحيح. (أو أرسل /cancel للإلغاء)")
            bot.register_next_step_handler(msg, handle_add_channel_step1)

    def handle_add_channel_step2(message):
        if check_cancel(message): return
        channel_name = message.text.strip()
        channel_id = admin_steps.pop(message.chat.id, {}).get("channel_id")
        if not channel_id: return

        if add_required_channel(channel_id, channel_name):
            bot.send_message(message.chat.id, f"✅ تم إضافة القناة \"{channel_name}\" (ID: {channel_id}) كقناة مطلوبة.")
        else:
            bot.send_message(message.chat.id, "❌ حدث خطأ أثناء إضافة القناة.")

    def handle_remove_channel_step(message):
        if check_cancel(message): return
        try:
            channel_id = int(message.text.strip())
            if remove_required_channel(channel_id):
                bot.send_message(message.chat.id, f"✅ تم إزالة القناة (ID: {channel_id}) من القنوات المطلوبة.")
            else:
                bot.send_message(message.chat.id, "❌ حدث خطأ أثناء إزالة القناة أو أنها غير موجودة.")
        except ValueError:
            msg = bot.send_message(message.chat.id, "معرف القناة غير صالح. يرجى إرسال رقم صحيح. (أو أرسل /cancel للإلغاء)")
            bot.register_next_step_handler(msg, handle_remove_channel_step)

    def handle_list_channels(message):
        channels = get_required_channels()
        if channels:
            response = "📋 *القنوات المطلوبة:*\n"
            for channel_id, channel_name in channels:
                response += f"- {channel_name} (ID: `{channel_id}`)\n"
            bot.send_message(message.chat.id, response, parse_mode="Markdown")
        else:
            bot.send_message(message.chat.id, "لا توجد قنوات مطلوبة حالياً.")

    # --- معالجات الرسائل العامة ---

    @bot.message_handler(content_types=["text"])
    def handle_text_search(message):
        """يعرض خيارات البحث للمستخدم."""
        if message.text.startswith("/"): return
        query = message.text.strip()
        
        user_last_search[message.chat.id] = query
        
        categories = get_categories_tree()
        keyboard = InlineKeyboardMarkup(row_width=1)
        
        keyboard.add(InlineKeyboardButton("بحث في كل التصنيفات", callback_data=f"search_scope::all"))
        
        for cat_id, cat_name, parent_id, full_path in categories:
            keyboard.add(InlineKeyboardButton(f"بحث في: {cat_name}", callback_data=f"search_scope::{cat_id}"))
            
        bot.reply_to(message, f"أين تريد البحث عن \"{query}\"؟", reply_markup=keyboard)

    @bot.message_handler(content_types=["video"])
    def handle_new_video(message):
        if str(message.chat.id) == CHANNEL_ID:
            active_category_id = get_active_category_id()
            file_id = message.video.file_id if message.video else None
            
            video_info_data = None
            if message.video and file_id:
                try:
                    file_info = bot.get_file(file_id)
                    downloaded_file = bot.download_file(file_info.file_path)
                    
                    # حفظ الملف مؤقتاً
                    temp_file_path = f"/tmp/{file_info.file_path.split("/")[-1]}"
                    with open(temp_file_path, "wb") as f:
                        f.write(downloaded_file)
                    
                    video_info_data = get_video_info(temp_file_path)
                    os.remove(temp_file_path) # حذف الملف المؤقت
                    
                except Exception as e:
                    print(f"Error downloading or processing video file: {e}")

            print(f"New video detected. Assigning to active category ID: {active_category_id}. Message ID: {message.message_id}")
            success = add_video(
                message_id=message.message_id,
                caption=message.caption,
                chat_id=message.chat.id,
                file_name=message.video.file_name if message.video else "",
                category_id=active_category_id,
                file_id=file_id,
                video_info=video_info_data
            )
            if success:
                print("Video added successfully with metadata extraction.")
            else:
                print("Failed to add video.")

    # --- معالج ضغطات الأزرار المحسن ---

    @bot.callback_query_handler(func=lambda call: True)
    def callback_query(call):
        """الاستجابة عند الضغط على الأزرار - نسخة محسنة مع معالجة أخطاء أفضل."""
        try:
            data = call.data.split(CALLBACK_DELIMITER)
            action = data[0]

            if action == "admin":
                sub_action = data[1]
                bot.answer_callback_query(call.id)
                
                if sub_action == "broadcast":
                    msg = bot.send_message(call.message.chat.id, "أرسل الرسالة التي تريد بثها لجميع المشتركين (نص، صورة، أو فيديو). (أو أرسل /cancel للإلغاء)")
                    bot.register_next_step_handler(msg, handle_rich_broadcast)
                elif sub_action == "sub_count":
                    count = get_subscriber_count()
                    bot.send_message(call.message.chat.id, f"👤 إجمالي عدد المشتركين في البوت: *{count}*", parse_mode="Markdown")
                
                elif sub_action == "stats":
                    stats = get_bot_stats()
                    popular = get_popular_videos()
                    
                    stats_text = f"📊 *إحصائيات المحتوى*\n\n"
                    stats_text += f"- إجمالي الفيديوهات: *{stats[\"video_count\"]}*\n"
                    stats_text += f"- إجمالي التصنيفات: *{stats[\"category_count\"]}*\n"
                    stats_text += f"- إجمالي المشاهدات: *{stats[\"total_views\"]}*\n"
                    stats_text += f"- إجمالي التقييمات: *{stats[\"total_ratings\"]}*\n\n"
                    
                    if popular[\"most_viewed\"]:
                        most_viewed = popular[\"most_viewed\"][0]
                        stats_text += f"🔥 الأكثر مشاهدة: {most_viewed[2] or most_viewed[4] or \"فيديو\"} ({most_viewed[7]} مشاهدة)\n"
                    
                    if popular[\"highest_rated\"]:
                        highest_rated = popular[\"highest_rated\"][0]
                        stats_text += f"⭐ الأعلى تقييماً: {highest_rated[2] or highest_rated[4] or \"فيديو\"} ({highest_rated[8]:.1f}/5)\n"
                    
                    bot.send_message(call.message.chat.id, stats_text, parse_mode="Markdown")
                
                elif sub_action == "add_new_cat":
                    msg = bot.send_message(call.message.chat.id, "أرسل اسم التصنيف الجديد الذي تريد إنشاءه. (أو أرسل /cancel للإلغاء)")
                    bot.register_next_step_handler(msg, handle_add_new_category)

                elif sub_action == "set_active":
                    categories = get_categories_tree()
                    if not categories:
                        bot.answer_callback_query(call.id, "لا توجد تصنيفات حالياً. قم بإنشاء واحد أولاً.")
                        return
                    keyboard = InlineKeyboardMarkup(row_width=2)
                    buttons = [InlineKeyboardButton(text=cat[1], callback_data=f"admin::setcat::{cat[0]}") for cat in categories]
                    keyboard.add(*buttons)
                    bot.edit_message_text("اختر التصنيف الذي تريد تفعيله:", call.message.chat.id, call.message.message_id, reply_markup=keyboard)

                elif sub_action == "setcat":
                    category_id = int(data[2])
                    if set_active_category_id(category_id):
                        category = get_category_by_id(category_id)
                        if category:
                            bot.edit_message_text(f"✅ تم تفعيل التصنيف \"{category[1]}\" بنجاح.", call.message.chat.id, call.message.message_id)
                        else:
                            bot.edit_message_text("❌ التصنيف غير موجود.", call.message.chat.id, call.message.message_id)
                    
                elif sub_action == "help":
                    help_text = "قائمة أوامر الإدارة:\n- إحصائيات البوت المتقدمة\n- تعيين التصنيف النشط\n- إضافة تصنيف جديد\n- إضافة قناة مطلوبة\n- إزالة قناة مطلوبة\n- عرض القنوات المطلوبة\n- معرف حسابي (/myid)\n- البث الغني (نص، صور، فيديوهات)\n- نظام التقييمات والإحصائيات"
                    bot.send_message(call.message.chat.id, help_text)
                    
                elif sub_action == "add_channel":
                    msg = bot.send_message(call.message.chat.id, "أرسل معرف القناة (مثال: -1001234567890). (أو أرسل /cancel للإلغاء)")
                    bot.register_next_step_handler(msg, handle_add_channel_step1)
                    
                elif sub_action == "remove_channel":
                    msg = bot.send_message(call.message.chat.id, "أرسل معرف القناة التي تريد إزالتها. (أو أرسل /cancel للإلغاء)")
                    bot.register_next_step_handler(msg, handle_remove_channel_step)
                    
                elif sub_action == "list_channels":
                    handle_list_channels(call.message)

            elif action == "popular":
                sub_action = data[1]
                popular = get_popular_videos()
                
                if sub_action == "most_viewed":
                    videos = popular["most_viewed"]
                    if videos:
                        keyboard = create_paginated_keyboard(videos, len(videos), 0, "popular_page", "most_viewed")
                        bot.edit_message_text("📈 الفيديوهات الأكثر مشاهدة:", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                    else:
                        bot.edit_message_text("لا توجد فيديوهات مشاهدة حالياً.", call.message.chat.id, call.message.message_id)
                
                elif sub_action == "highest_rated":
                    videos = popular["highest_rated"]
                    if videos:
                        keyboard = create_paginated_keyboard(videos, len(videos), 0, "popular_page", "highest_rated")
                        bot.edit_message_text("⭐ الفيديوهات الأعلى تقييماً:", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                    else:
                        bot.edit_message_text("لا توجد فيديوهات مقيمة حالياً.", call.message.chat.id, call.message.message_id)
                
                bot.answer_callback_query(call.id)

            elif action == "back_to_cats":
                list_videos(call.message, edit_message=call.message)
                bot.answer_callback_query(call.id)

            elif action == "video":
                _, video_id, message_id, chat_id = data
                video_id = int(video_id)
                
                increment_video_view_count(video_id)
                
                try:
                    video = get_video_by_message_id(int(message_id))
                    if not video:
                        bot.answer_callback_query(call.id, "الفيديو غير موجود.")
                        return
                    
                    metadata = json.loads(video[4]) if isinstance(video[4], str) else video[4]
                    qualities = metadata.get("qualities", [])
                    
                    if len(qualities) > 1:
                        keyboard = InlineKeyboardMarkup()
                        for quality in qualities:
                            res = quality["resolution"]
                            keyboard.add(InlineKeyboardButton(f"جودة {res}", callback_data=f"quality::{message_id}::{res}"))
                        
                        rating_keyboard = create_video_action_keyboard(video_id, call.from_user.id)
                        for row in rating_keyboard.keyboard:
                            keyboard.keyboard.append(row)
                        
                        bot.send_message(call.message.chat.id, "اختر الجودة المطلوبة:", reply_markup=keyboard)
                        bot.answer_callback_query(call.id)
                        return
                    
                    bot.copy_message(call.message.chat.id, chat_id, int(message_id))
                    rating_keyboard = create_video_action_keyboard(video_id, call.from_user.id)
                    bot.send_message(call.message.chat.id, "قيم هذا الفيديو:", reply_markup=rating_keyboard)
                    bot.answer_callback_query(call.id, "جاري إرسال الفيديو...")
                    
                except Exception as e:
                    print(f"Error handling video callback: {e}")
                    bot.answer_callback_query(call.id, "حدث خطأ أثناء إرسال الفيديو.")

            elif action == "rate":
                _, video_id, rating = data
                video_id = int(video_id)
                rating = int(rating)
                
                if add_video_rating(video_id, call.from_user.id, rating):
                    new_keyboard = create_video_action_keyboard(video_id, call.from_user.id)
                    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=new_keyboard)
                    bot.answer_callback_query(call.id, f"تم تقييم الفيديو بـ {rating} نجوم!")
                else:
                    bot.answer_callback_query(call.id, "حدث خطأ في التقييم.")

            elif action == "quality":
                _, original_message_id, resolution = data
                try:
                    video = get_video_by_message_id(int(original_message_id))
                    if video:
                        bot.copy_message(call.message.chat.id, video[3], int(original_message_id))
                        bot.answer_callback_query(call.id, f"جاري إرسال الفيديو بجودة {resolution}...")
                    else:
                        bot.answer_callback_query(call.id, "خطأ في العثور على الفيديو.")
                except Exception as e:
                    print(f"Error handling quality callback: {e}")
                    bot.answer_callback_query(call.id, "حدث خطأ.")

            elif action == "cat":
                _, category_id, page_str = data
                page = int(page_str)
                category_id = int(category_id)
                
                child_categories = get_child_categories(category_id)
                if child_categories:
                    keyboard = create_categories_keyboard(category_id)
                    category = get_category_by_id(category_id)
                    if category:
                        bot.edit_message_text(f"التصنيفات الفرعية في \"{category[1]}\"", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                    else:
                        bot.edit_message_text("التصنيف غير موجود.", call.message.chat.id, call.message.message_id)
                else:
                    videos, total_count = get_videos(category_id, page)
                    if videos:
                        keyboard = create_paginated_keyboard(videos, total_count, page, "cat", category_id)
                        category = get_category_by_id(category_id)
                        bot.edit_message_text(f"الفيديوهات في فئة \"{category[1] if category else \"غير معروف\"}\"", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                    else:
                        bot.edit_message_text("لا توجد فيديوهات في هذا التصنيف.", call.message.chat.id, call.message.message_id)
                
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
                        bot.edit_message_text(f"لم يتم العثور على نتائج للبحث الشامل عن \"{query}\".", call.message.chat.id, call.message.message_id)
                        return
                    keyboard = create_paginated_keyboard(videos, total_count, page, "search_all", "all")
                    bot.edit_message_text(f"نتائج البحث الشامل عن \"{query}\":", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                else: 
                    category_id = int(scope)
                    videos, total_count = search_videos(query, page=page, category_id=category_id)
                    if not videos:
                        category = get_category_by_id(category_id)
                        bot.edit_message_text(f"لم يتم العثور على نتائج للبحث عن \"{query}\" في فئة \"{category[1] if category else \"غير معروف\"}\"", call.message.chat.id, call.message.message_id)
                        return
                    keyboard = create_paginated_keyboard(videos, total_count, page, "search_cat", category_id)
                    category = get_category_by_id(category_id)
                    bot.edit_message_text(f"نتائج البحث عن \"{query}\" في فئة \"{category[1] if category else \"غير معروف\"}\"", call.message.chat.id, call.message.message_id, reply_markup=keyboard)

            elif action == "search_all":
                _, context, page_str = data
                page = int(page_str)
                query = user_last_search.get(call.message.chat.id)
                if not query:
                    bot.answer_callback_query(call.id, "انتهت صلاحية البحث، يرجى البحث مرة أخرى.")
                    return
                videos, total_count = search_videos(query, page=page)
                keyboard = create_paginated_keyboard(videos, total_count, page, "search_all", "all")
                bot.edit_message_text(f"نتائج البحث الشامل عن \"{query}\":", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                bot.answer_callback_query(call.id)

            elif action == "search_cat":
                _, category_id, page_str = data
                page = int(page_str)
                category_id = int(category_id)
                query = user_last_search.get(call.message.chat.id)
                if not query:
                    bot.answer_callback_query(call.id, "انتهت صلاحية البحث، يرجى البحث مرة أخرى.")
                    return
                videos, total_count = search_videos(query, page=page, category_id=category_id)
                keyboard = create_paginated_keyboard(videos, total_count, page, "search_cat", category_id)
                category = get_category_by_id(category_id)
                bot.edit_message_text(f"نتائج البحث عن \"{query}\" في فئة \"{category[1] if category else \"غير معروف\"}\"", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                bot.answer_callback_query(call.id)
            
            elif action == "noop":
                bot.answer_callback_query(call.id)

        except Exception as e:
            print(f"Callback query error: {e}")
            bot.answer_callback_query(call.id, "حدث خطأ. حاول مرة أخرى.")



