import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
import os
import time
import re
import json
from urllib.parse import urlparse
import math
from datetime import datetime
import logging
import threading

# إعداد المسجل (logger) لهذا الملف
logger = logging.getLogger(__name__)

# --- جملة الاستيراد المصححة مع إضافة الدوال الجديدة ---
from db_manager import (
    add_category, get_categories_tree, get_child_categories,
    get_category_by_id, add_video, get_videos, increment_video_view_count,
    get_video_by_message_id, get_active_category_id, set_active_category_id,
    add_video_rating, get_video_rating_stats, get_user_video_rating,
    get_popular_videos, add_bot_user, get_all_user_ids, get_subscriber_count,
    get_bot_stats, search_videos, add_required_channel, remove_required_channel,
    get_required_channels, admin_steps, user_last_search, VIDEOS_PER_PAGE, CALLBACK_DELIMITER,
    move_video_to_category, get_video_by_id, delete_videos_by_ids,
    delete_category_and_contents, move_videos_from_category, delete_category_by_id as delete_cat_record,
    get_db_connection # استيراد دالة الاتصال
)
from utils import extract_video_metadata, get_video_info
# --- استيراد دالة التحديث ---
from update_metadata import update_all_videos_metadata_generator

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
        for cat in categories:
            buttons.append(InlineKeyboardButton(cat['name'], callback_data=f"cat::{cat['id']}::0"))
        keyboard.add(*buttons)
        if parent_id:
            parent_category = get_category_by_id(parent_id)
            if parent_category and parent_category.get('parent_id') is not None:
                 keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data=f"cat::{parent_category['parent_id']}::0"))
            else:
                 keyboard.add(InlineKeyboardButton("🔙 رجوع للتصنيفات الرئيسية", callback_data="back_to_cats"))
        return keyboard

    def format_duration(seconds):
        if not seconds or not isinstance(seconds, (int, float)):
            return ""
        secs = int(seconds)
        mins, secs = divmod(secs, 60)
        hours, mins = divmod(mins, 60)
        if hours > 0:
            return f"{hours:02}:{mins:02}:{secs:02}"
        return f"{mins:02}:{secs:02}"

    def format_video_display_info(video):
        """إنشاء عنوان عرض غني بالمعلومات للفيديو مع سلوك احتياطي ذكي."""
        metadata = video.get('metadata') or {}
        
        series_name = metadata.get('series_name')
        season = metadata.get('season')
        episode = metadata.get('episode')

        if series_name and (season or episode):
            title_base = series_name.strip()
            season_episode_part = []
            if season: season_episode_part.append(f"م{season}")
            if episode: season_episode_part.append(f"ح{episode}")
            title = f"{video['id']}. {title_base} - {' '.join(season_episode_part)}"
        else:
            fallback_title = video['caption'].split('\n')[0] if video['caption'] else ""
            title = f"{video['id']}. {fallback_title.strip()}"

        info_parts = []
        status = metadata.get('status')
        if status: info_parts.append(status)
        quality = metadata.get('quality_resolution')
        if quality: info_parts.append(quality)
        duration_str = format_duration(metadata.get('duration'))
        if duration_str: info_parts.append(duration_str)
        info_line = f" ({' | '.join(info_parts)})" if info_parts else ""
        
        rating_text = f" ⭐ {video['avg_rating']:.1f}/5" if video.get('avg_rating', 0) > 0 else ""
        views_text = f" 👁️ {video['view_count']}"
        
        return f"{title}{info_line}{rating_text}{views_text}"

    def create_paginated_keyboard(videos, total_count, current_page, action_prefix, context_id):
        keyboard = InlineKeyboardMarkup(row_width=1)
        for video in videos:
            display_title = format_video_display_info(video)
            keyboard.add(InlineKeyboardButton(
                display_title, 
                callback_data=f"video::{video['id']}::{video['message_id']}::{video['chat_id']}"
            ))
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

    def create_combined_keyboard(child_categories, videos, total_video_count, current_page, parent_category_id):
        keyboard = InlineKeyboardMarkup()
        if child_categories:
            keyboard.add(InlineKeyboardButton("📂--- التصنيفات الفرعية ---📂", callback_data="noop"), row_width=1)
            cat_buttons = [InlineKeyboardButton(f"📁 {cat['name']}", callback_data=f"cat::{cat['id']}::0") for cat in child_categories]
            for i in range(0, len(cat_buttons), 2):
                keyboard.add(*cat_buttons[i:i+2])
        if videos:
            if child_categories:
                keyboard.add(InlineKeyboardButton("🎬--- الفيديوهات ---🎬", callback_data="noop"), row_width=1)
            for video in videos:
                display_title = format_video_display_info(video)
                keyboard.add(InlineKeyboardButton(
                    display_title,
                    callback_data=f"video::{video['id']}::{video['message_id']}::{video['chat_id']}"
                ), row_width=1)
        nav_buttons = []
        if current_page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"cat::{parent_category_id}::{current_page - 1}"))
        total_pages = math.ceil(total_video_count / VIDEOS_PER_PAGE) - 1
        if current_page < total_pages:
            nav_buttons.append(InlineKeyboardButton("التالي ➡️", callback_data=f"cat::{parent_category_id}::{current_page + 1}"))
        if nav_buttons:
            keyboard.add(*nav_buttons, row_width=2)
        parent_category = get_category_by_id(parent_category_id)
        if parent_category and parent_category.get('parent_id') is not None:
             keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data=f"cat::{parent_category['parent_id']}::0"), row_width=1)
        else:
             keyboard.add(InlineKeyboardButton("🔙 رجوع للتصنيفات الرئيسية", callback_data="back_to_cats"), row_width=1)
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
        if stats and stats['avg']:
            avg_rating = stats['avg']
            total_ratings = stats['count']
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
                logger.warning(f"Channel {channel_id} not found. Please remove it from required channels.")
                return True
            return False

    # --- أوامر البوت ---
    @bot.message_handler(commands=["start"])
    def start(message):
        add_bot_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
        required_channels = get_required_channels()
        if required_channels:
            not_subscribed_channels = []
            for channel in required_channels:
                if not check_subscription(message.from_user.id, channel['channel_id']):
                    not_subscribed_channels.append(channel)
            if not_subscribed_channels:
                markup = InlineKeyboardMarkup(row_width=1)
                for channel in not_subscribed_channels:
                    channel_link_id = str(channel['channel_id']).replace("-100", "")
                    markup.add(InlineKeyboardButton(f"اشترك في {channel['channel_name']}", url=f"https://t.me/c/{channel_link_id}"))
                markup.add(InlineKeyboardButton("✅ لقد اشتركت، تحقق الآن", callback_data="check_subscription"))
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
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.add(InlineKeyboardButton("📈 الأكثر مشاهدة", callback_data="popular::most_viewed"))
        keyboard.add(InlineKeyboardButton("⭐ الأعلى تقييماً", callback_data="popular::highest_rated"))
        bot.reply_to(message, "اختر نوع الفيديوهات الشائعة:", reply_markup=keyboard)

    def list_videos(message, edit_message=None, parent_id=None):
        keyboard = create_categories_keyboard(parent_id)
        if keyboard.keyboard:
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

    # --- لوحة تحكم الآدمن (مُعاد ترتيبها) ---
    def generate_admin_panel():
        keyboard = InlineKeyboardMarkup(row_width=2)
        btn_add_cat = InlineKeyboardButton("➕ إضافة تصنيف", callback_data="admin::add_new_cat")
        btn_delete_cat = InlineKeyboardButton("🗑️ حذف تصنيف", callback_data="admin::delete_category_select")
        btn_move_video = InlineKeyboardButton("➡️ نقل فيديو بالرقم", callback_data="admin::move_video_by_id")
        btn_delete_video = InlineKeyboardButton("❌ حذف فيديوهات بالأرقام", callback_data="admin::delete_videos_by_ids")
        btn_set_active = InlineKeyboardButton("🔘 تعيين التصنيف النشط", callback_data="admin::set_active")
        btn_update_meta = InlineKeyboardButton("🔄 تحديث بيانات الفيديوهات القديمة", callback_data="admin::update_metadata")
        btn_add_channel = InlineKeyboardButton("➕ إضافة قناة اشتراك", callback_data="admin::add_channel")
        btn_remove_channel = InlineKeyboardButton("➖ إزالة قناة اشتراك", callback_data="admin::remove_channel")
        btn_list_channels = InlineKeyboardButton("📋 عرض القنوات", callback_data="admin::list_channels")
        btn_broadcast = InlineKeyboardButton("📢 بث رسالة", callback_data="admin::broadcast")
        btn_stats = InlineKeyboardButton("📊 الإحصائيات", callback_data="admin::stats")
        btn_subs = InlineKeyboardButton("👤 عدد المشتركين", callback_data="admin::sub_count")
        btn_help = InlineKeyboardButton("ℹ️ مساعدة", callback_data="admin::help")
        
        keyboard.add(btn_add_cat, btn_delete_cat)
        keyboard.add(btn_move_video, btn_delete_video)
        keyboard.add(btn_set_active, btn_update_meta)
        keyboard.add(btn_add_channel, btn_remove_channel)
        keyboard.add(btn_list_channels)
        keyboard.add(btn_broadcast, btn_stats, btn_subs, btn_help)
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
        if message.text == "/cancel":
            if message.chat.id in admin_steps:
                del admin_steps[message.chat.id]
            bot.send_message(message.chat.id, "تم إلغاء العملية.")
            return True
        return False

    def handle_rich_broadcast(message):
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
                logger.warning(f"Failed to send broadcast to {user_id}: {e}")
            time.sleep(0.1)
        bot.send_message(message.chat.id, f"✅ اكتمل البث!\n\n- رسائل ناجحة: {sent_count}\n- رسائل فاشلة: {failed_count}")

    def handle_add_new_category(message):
        if check_cancel(message): return
        category_name = message.text.strip()
        step_data = admin_steps.pop(message.chat.id, {})
        parent_id = step_data.get("parent_id")
        success, result = add_category(category_name, parent_id=parent_id)
        if success:
            bot.reply_to(message, f"✅ تم إنشاء التصنيف الجديد بنجاح: \"{category_name}\".")
        else:
            bot.reply_to(message, f"❌ خطأ في إنشاء التصنيف: {result}")

    def handle_add_channel_step1(message):
        if check_cancel(message): return
        try:
            channel_id = int(message.text.strip())
            admin_steps[message.chat.id] = {"channel_id": channel_id}
            msg = bot.send_message(message.chat.id, "الآن أرسل اسم القناة (مثال: قناة الأفلام). (أو /cancel)")
            bot.register_next_step_handler(msg, handle_add_channel_step2)
        except ValueError:
            msg = bot.send_message(message.chat.id, "معرف القناة غير صالح. أرسل رقم صحيح. (أو /cancel)")
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
                bot.send_message(message.chat.id, "❌ حدث خطأ أو القناة غير موجودة.")
        except ValueError:
            msg = bot.send_message(message.chat.id, "معرف القناة غير صالح. أرسل رقم صحيح. (أو /cancel)")
            bot.register_next_step_handler(msg, handle_remove_channel_step)

    def handle_list_channels(message):
        channels = get_required_channels()
        if channels:
            response = "📋 *القنوات المطلوبة:*\n" + "\n".join([f"- {ch['channel_name']} (ID: `{ch['channel_id']}`)" for ch in channels])
            bot.send_message(message.chat.id, response, parse_mode="Markdown")
        else:
            bot.send_message(message.chat.id, "لا توجد قنوات مطلوبة حالياً.")

    # --- معالجات الرسائل العامة والبحث ---
    @bot.message_handler(func=lambda message: message.text and not message.text.startswith("/") and message.chat.type == "private")
    def handle_private_text_search(message):
        query = message.text.strip()
        user_last_search[message.chat.id] = query
        categories = get_categories_tree()
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.add(InlineKeyboardButton("بحث في كل التصنيفات", callback_data=f"search_scope::all"))
        for cat in categories:
            keyboard.add(InlineKeyboardButton(f"بحث في: {cat['name']}", callback_data=f"search_scope::{cat['id']}"))
        bot.reply_to(message, f"أين تريد البحث عن \"{query}\"؟", reply_markup=keyboard)

    def handle_text_search(message):
        handle_private_text_search(message)

    @bot.message_handler(commands=["search"])
    def handle_search_command(message):
        if message.chat.type == "private":
            msg = bot.reply_to(message, "أرسل الكلمة المفتاحية للبحث:")
            bot.register_next_step_handler(msg, handle_private_text_search)
        else:
            if len(message.text.split()) > 1:
                query = " ".join(message.text.split()[1:])
                perform_group_search(message, query)
            else:
                bot.reply_to(message, "يرجى إدخال كلمة البحث بعد الأمر /search")

    def perform_group_search(message, query):
        user_last_search[message.chat.id] = query
        videos, total_count = search_videos(query, page=0)
        if not videos:
            bot.reply_to(message, f"لم يتم العثور على نتائج للبحث عن \"{query}\".")
            return
        keyboard = create_paginated_keyboard(videos, total_count, 0, "search_all", "all")
        bot.reply_to(message, f"نتائج البحث عن \"{query}\":", reply_markup=keyboard)

    # --- معالج الفيديو الجديد من القناة ---
    @bot.message_handler(content_types=["video"])
    def handle_new_video(message):
        if str(message.chat.id) == CHANNEL_ID:
            active_category_id = get_active_category_id()
            if not active_category_id:
                logger.warning("No active category set. Video will not be saved.")
                return
            metadata = extract_video_metadata(message.caption)
            if message.video:
                metadata['duration'] = message.video.duration
                if 'quality_resolution' not in metadata:
                    metadata['quality_resolution'] = f"{message.video.height}p" if message.video.height else "N/A"
            add_video(
                message_id=message.message_id, caption=message.caption, chat_id=message.chat.id,
                file_name=message.video.file_name if message.video else "", category_id=active_category_id,
                file_id=message.video.file_id, video_info=metadata
            )
            logger.info(f"Video {message.message_id} added to category {active_category_id} with smart metadata.")

    # --- معالجات نقل وحذف الفيديوهات الجديدة (بالأرقام) ---
    def handle_delete_by_ids_input(message):
        if check_cancel(message): return
        try:
            video_ids_str = re.split(r'[,\s\n]+', message.text.strip())
            video_ids = [int(num) for num in video_ids_str if num.isdigit()]
            if not video_ids:
                bot.reply_to(message, "لم يتم إدخال أرقام صحيحة. حاول مرة أخرى أو أرسل /cancel.")
                return
            deleted_count = delete_videos_by_ids(video_ids)
            bot.reply_to(message, f"✅ تم حذف {deleted_count} فيديو بنجاح.")
        except Exception as e:
            logger.error(f"Error in handle_delete_by_ids_input: {e}", exc_info=True)
            bot.reply_to(message, "حدث خطأ. تأكد من إدخال أرقام فقط مفصولة بمسافات أو فواصل.")

    def handle_move_by_id_input(message):
        if check_cancel(message): return
        try:
            video_id = int(message.text.strip())
            video = get_video_by_id(video_id)
            if not video:
                msg = bot.reply_to(message, "عذراً، لا يوجد فيديو بهذا الرقم. حاول مرة أخرى أو أرسل /cancel.")
                bot.register_next_step_handler(msg, handle_move_by_id_input)
                return
            keyboard = create_categories_keyboard()
            if not keyboard.keyboard:
                bot.reply_to(message, "لا توجد تصنيفات لنقل الفيديو إليها.")
                return
            for row in keyboard.keyboard:
                for button in row:
                    parts = button.callback_data.split(CALLBACK_DELIMITER)
                    button.callback_data = f"admin::move_confirm::{video['id']}::{parts[1]}"
            bot.reply_to(message, f"اختر التصنيف الجديد لنقل الفيديو رقم {video_id}:", reply_markup=keyboard)
        except ValueError:
            msg = bot.reply_to(message, "الرجاء إدخال رقم صحيح. حاول مرة أخرى أو أرسل /cancel.")
            bot.register_next_step_handler(msg, handle_move_by_id_input)
        except Exception as e:
            logger.error(f"Error in handle_move_by_id_input: {e}", exc_info=True)
            bot.reply_to(message, "حدث خطأ غير متوقع.")

    # --- دالة تشغيل التحديث في الخلفية ---
    def run_metadata_update(chat_id, message_id):
        conn = get_db_connection()
        if not conn:
            bot.edit_message_text("❌ فشل الاتصال بقاعدة البيانات.", chat_id, message_id)
            return
        
        try:
            last_edit_time = 0
            for status, val1, val2 in update_all_videos_metadata_generator(conn):
                if status == "progress":
                    if time.time() - last_edit_time > 1.5: # تحديث كل 1.5 ثانية
                        try:
                            progress = (val1 / val2) * 100 if val2 > 0 else 0
                            bot.edit_message_text(f"⏳ جارِ تحديث البيانات... ({val1}/{val2}) - {progress:.0f}%", chat_id, message_id)
                            last_edit_time = time.time()
                        except telebot.apihelper.ApiTelegramException as e:
                            if 'message is not modified' in e.description:
                                continue # تجاهل هذا الخطأ الشائع
                            else:
                                logger.error(f"Error editing progress message: {e}")
                elif status == "done":
                    updated_count, total_videos = val1, val2
                    bot.edit_message_text(f"✅ اكتمل التحديث بنجاح!\n\n- تم فحص: {total_videos} فيديو.\n- تم تحديث: {updated_count} فيديو.", chat_id, message_id)
                elif status == "error":
                    bot.edit_message_text(f"❌ حدث خطأ أثناء التحديث: {val1}", chat_id, message_id)
        except Exception as e:
            logger.error(f"Error in run_metadata_update thread: {e}", exc_info=True)
            bot.edit_message_text("❌ حدث خطأ فادح أثناء تشغيل التحديث.", chat_id, message_id)
        finally:
            if conn:
                conn.close()

    # --- معالج ضغطات الأزرار الشامل ---
    @bot.callback_query_handler(func=lambda call: True)
    def callback_query(call):
        try:
            data = call.data.split(CALLBACK_DELIMITER)
            action = data[0]

            if action == "admin":
                sub_action = data[1]
                bot.answer_callback_query(call.id)
                
                if sub_action == "add_new_cat":
                    keyboard = InlineKeyboardMarkup()
                    keyboard.add(InlineKeyboardButton("تصنيف رئيسي جديد", callback_data="admin::add_cat_main"))
                    keyboard.add(InlineKeyboardButton("تصنيف فرعي", callback_data="admin::add_cat_sub_select_parent"))
                    bot.edit_message_text("اختر نوع التصنيف الذي تريد إضافته:", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                
                elif sub_action == "add_cat_main":
                    admin_steps[call.message.chat.id] = {"parent_id": None}
                    msg = bot.send_message(call.message.chat.id, "أرسل اسم التصنيف الرئيسي الجديد. (أو /cancel)")
                    bot.register_next_step_handler(msg, handle_add_new_category)

                elif sub_action == "add_cat_sub_select_parent":
                    keyboard = create_categories_keyboard()
                    if not keyboard.keyboard:
                        bot.answer_callback_query(call.id, "أنشئ تصنيفاً رئيسياً أولاً.", show_alert=True)
                        return
                    for row in keyboard.keyboard:
                        for button in row:
                            parts = button.callback_data.split(CALLBACK_DELIMITER)
                            button.callback_data = f"admin::add_cat_sub_set_parent::{parts[1]}"
                    bot.edit_message_text("اختر التصنيف الأب:", call.message.chat.id, call.message.message_id, reply_markup=keyboard)

                elif sub_action == "add_cat_sub_set_parent":
                    parent_id = int(data[2])
                    admin_steps[call.message.chat.id] = {"parent_id": parent_id}
                    msg = bot.send_message(call.message.chat.id, "الآن أرسل اسم التصنيف الفرعي الجديد. (أو /cancel)")
                    bot.register_next_step_handler(msg, handle_add_new_category)

                elif sub_action == "delete_category_select":
                    keyboard = create_categories_keyboard()
                    if not keyboard.keyboard:
                        bot.answer_callback_query(call.id, "لا توجد تصنيفات لحذفها.", show_alert=True)
                        return
                    for row in keyboard.keyboard:
                        for button in row:
                            parts = button.callback_data.split(CALLBACK_DELIMITER)
                            button.callback_data = f"admin::delete_category_confirm::{parts[1]}"
                    bot.edit_message_text("اختر التصنيف الذي تريد حذفه:", call.message.chat.id, call.message.message_id, reply_markup=keyboard)

                elif sub_action == "delete_category_confirm":
                    category_id = int(data[2])
                    category = get_category_by_id(category_id)
                    keyboard = InlineKeyboardMarkup(row_width=1)
                    keyboard.add(InlineKeyboardButton("🗑️ حذف التصنيف مع كل فيديوهاته", callback_data=f"admin::delete_cat_and_videos::{category_id}"))
                    keyboard.add(InlineKeyboardButton("➡️ نقل فيديوهاته لتصنيف آخر", callback_data=f"admin::delete_cat_move_videos_select_dest::{category_id}"))
                    keyboard.add(InlineKeyboardButton("🔙 إلغاء", callback_data="admin::cancel_delete_cat"))
                    bot.edit_message_text(f"أنت على وشك حذف \"{category['name']}\". ماذا أفعل بالفيديوهات؟", call.message.chat.id, call.message.message_id, reply_markup=keyboard)

                elif sub_action == "delete_cat_and_videos":
                    category_id = int(data[2])
                    category = get_category_by_id(category_id)
                    delete_category_and_contents(category_id)
                    bot.edit_message_text(f"✅ تم حذف التصنيف \"{category['name']}\" وكل محتوياته.", call.message.chat.id, call.message.message_id)

                elif sub_action == "delete_cat_move_videos_select_dest":
                    old_category_id = int(data[2])
                    categories = [cat for cat in get_categories_tree() if cat['id'] != old_category_id]
                    if not categories:
                        bot.edit_message_text("لا يوجد تصنيف آخر لنقل الفيديوهات إليه.", call.message.chat.id, call.message.message_id)
                        return
                    keyboard = InlineKeyboardMarkup(row_width=1)
                    for cat in categories:
                         keyboard.add(InlineKeyboardButton(cat['name'], callback_data=f"admin::delete_cat_move_videos_confirm::{old_category_id}::{cat['id']}"))
                    keyboard.add(InlineKeyboardButton("🔙 إلغاء", callback_data="admin::cancel_delete_cat"))
                    bot.edit_message_text("اختر التصنيف الذي ستُنقل إليه الفيديوهات:", call.message.chat.id, call.message.message_id, reply_markup=keyboard)

                elif sub_action == "delete_cat_move_videos_confirm":
                    old_category_id = int(data[2])
                    new_category_id = int(data[3])
                    category_to_delete = get_category_by_id(old_category_id)
                    move_videos_from_category(old_category_id, new_category_id)
                    delete_cat_record(old_category_id)
                    new_cat = get_category_by_id(new_category_id)
                    bot.edit_message_text(f"✅ تم نقل الفيديوهات إلى \"{new_cat['name']}\" وحذف التصنيف \"{category_to_delete['name']}\".", call.message.chat.id, call.message.message_id)

                elif sub_action == "cancel_delete_cat":
                     bot.edit_message_text("👍 تم إلغاء عملية حذف التصنيف.", call.message.chat.id, call.message.message_id)

                elif sub_action == "move_video_by_id":
                    msg = bot.send_message(call.message.chat.id, "أرسل رقم الفيديو (ID) الذي تريد نقله. (أو /cancel)")
                    bot.register_next_step_handler(msg, handle_move_by_id_input)

                elif sub_action == "delete_videos_by_ids":
                    msg = bot.send_message(call.message.chat.id, "أرسل أرقام الفيديوهات (IDs) التي تريد حذفها، مفصولة بمسافة أو فاصلة. (أو /cancel)")
                    bot.register_next_step_handler(msg, handle_delete_by_ids_input)

                elif sub_action == "move_confirm":
                    _, video_id, new_category_id = data
                    move_video_to_category(int(video_id), int(new_category_id))
                    category = get_category_by_id(int(new_category_id))
                    bot.edit_message_text(f"✅ تم نقل الفيديو بنجاح إلى تصنيف \"{category['name']}\".", call.message.chat.id, call.message.message_id)
                
                elif sub_action == "update_metadata":
                    msg = bot.edit_message_text("تم إرسال طلب تحديث البيانات...", call.message.chat.id, call.message.message_id)
                    update_thread = threading.Thread(target=run_metadata_update, args=(msg.chat.id, msg.message_id))
                    update_thread.start()

                elif sub_action == "set_active":
                    categories = get_categories_tree()
                    if not categories:
                        bot.answer_callback_query(call.id, "لا توجد تصنيفات حالياً.", show_alert=True)
                        return
                    keyboard = InlineKeyboardMarkup(row_width=2)
                    buttons = [InlineKeyboardButton(text=cat['name'], callback_data=f"admin::setcat::{cat['id']}") for cat in categories]
                    keyboard.add(*buttons)
                    bot.edit_message_text("اختر التصنيف الذي تريد تفعيله:", call.message.chat.id, call.message.message_id, reply_markup=keyboard)

                elif sub_action == "setcat":
                    category_id = int(data[2])
                    if set_active_category_id(category_id):
                        category = get_category_by_id(category_id)
                        bot.edit_message_text(f"✅ تم تفعيل التصنيف \"{category['name']}\" بنجاح.", call.message.chat.id, call.message.message_id)
                    
                elif sub_action == "add_channel":
                    msg = bot.send_message(call.message.chat.id, "أرسل معرف القناة (مثال: -1001234567890). (أو /cancel)")
                    bot.register_next_step_handler(msg, handle_add_channel_step1)
                    
                elif sub_action == "remove_channel":
                    msg = bot.send_message(call.message.chat.id, "أرسل معرف القناة التي تريد إزالتها. (أو /cancel)")
                    bot.register_next_step_handler(msg, handle_remove_channel_step)
                    
                elif sub_action == "list_channels":
                    handle_list_channels(call.message)

                elif sub_action == "broadcast":
                    msg = bot.send_message(call.message.chat.id, "أرسل الرسالة التي تريد بثها. (أو /cancel)")
                    bot.register_next_step_handler(msg, handle_rich_broadcast)

                elif sub_action == "sub_count":
                    count = get_subscriber_count()
                    bot.send_message(call.message.chat.id, f"👤 إجمالي عدد المشتركين: *{count}*", parse_mode="Markdown")
                
                elif sub_action == "stats":
                    stats = get_bot_stats()
                    popular = get_popular_videos()
                    stats_text = (f"📊 *إحصائيات المحتوى*\n\n"
                                  f"- إجمالي الفيديوهات: *{stats['video_count']}*\n"
                                  f"- إجمالي التصنيفات: *{stats['category_count']}*\n"
                                  f"- إجمالي المشاهدات: *{stats['total_views']}*\n"
                                  f"- إجمالي التقييمات: *{stats['total_ratings']}*")
                    if popular["most_viewed"]:
                        most_viewed = popular["most_viewed"][0]
                        title = (most_viewed['caption'] or "").split('\n')[0] or "فيديو"
                        stats_text += f"\n\n🔥 الأكثر مشاهدة: {title} ({most_viewed['view_count']} مشاهدة)"
                    if popular["highest_rated"]:
                        highest_rated = popular["highest_rated"][0]
                        title = (highest_rated['caption'] or "").split('\n')[0] or "فيديو"
                        stats_text += f"\n⭐ الأعلى تقييماً: {title} ({highest_rated['avg_rating']:.1f}/5)"
                    bot.send_message(call.message.chat.id, stats_text, parse_mode="Markdown")
                
                elif sub_action == "help":
                    help_text = "قائمة أوامر الإدارة:\n- يمكنك الآن إدارة التصنيفات والفيديوهات مباشرة من الأزرار.\n- استخدم الأوامر النصية عند الحاجة فقط."
                    bot.send_message(call.message.chat.id, help_text)

            # --- قسم المستخدمين (تصفح، بحث، تقييم) ---
            elif action == "check_subscription":
                required_channels = get_required_channels()
                not_subscribed_channels = []
                if required_channels:
                    for channel in required_channels:
                        if not check_subscription(call.from_user.id, channel['channel_id']):
                            not_subscribed_channels.append(channel)
                
                if not_subscribed_channels:
                    bot.answer_callback_query(call.id, "❌ لم تشترك في جميع القنوات بعد. يرجى المحاولة مرة أخرى.", show_alert=True)
                else:
                    bot.answer_callback_query(call.id, "✅ شكراً لاشتراكك!")
                    bot.delete_message(call.message.chat.id, call.message.message_id)
                    bot.send_message(call.message.chat.id, "أهلاً بك في بوت البحث عن الفيديوهات!", reply_markup=main_menu())

            elif action == "popular":
                sub_action = data[1]
                popular = get_popular_videos()
                videos = popular.get(sub_action)
                title = "📈 الفيديوهات الأكثر مشاهدة:" if sub_action == "most_viewed" else "⭐ الفيديوهات الأعلى تقييماً:"
                if videos:
                    keyboard = create_paginated_keyboard(videos, len(videos), 0, "popular_page", sub_action)
                    bot.edit_message_text(title, call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                else:
                    bot.edit_message_text("لا توجد فيديوهات كافية لعرضها حالياً.", call.message.chat.id, call.message.message_id)
                bot.answer_callback_query(call.id)

            elif action == "back_to_cats":
                list_videos(call.message, edit_message=call.message, parent_id=None)
                bot.answer_callback_query(call.id)

            elif action == "video":
                _, video_id, message_id, chat_id = data
                increment_video_view_count(int(video_id))
                try:
                    bot.copy_message(call.message.chat.id, chat_id, int(message_id))
                    rating_keyboard = create_video_action_keyboard(int(video_id), call.from_user.id)
                    bot.send_message(call.message.chat.id, "قيم هذا الفيديو:", reply_markup=rating_keyboard)
                    bot.answer_callback_query(call.id, "جاري إرسال الفيديو...")
                except Exception as e:
                    logger.error(f"Error handling video callback: {e}", exc_info=True)
                    bot.answer_callback_query(call.id, "خطأ: الفيديو غير موجود بالقناة.", show_alert=True)

            elif action == "rate":
                _, video_id, rating = data
                if add_video_rating(int(video_id), call.from_user.id, int(rating)):
                    new_keyboard = create_video_action_keyboard(int(video_id), call.from_user.id)
                    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=new_keyboard)
                    bot.answer_callback_query(call.id, f"تم تقييم الفيديو بـ {rating} نجوم!")
                else:
                    bot.answer_callback_query(call.id, "حدث خطأ في التقييم.")

            elif action == "cat":
                _, category_id_str, page_str = data
                category_id, page = int(category_id_str), int(page_str)
                
                child_categories = get_child_categories(category_id)
                videos, total_count = get_videos(category_id, page)
                category = get_category_by_id(category_id)

                if not child_categories and not videos:
                    bot.edit_message_text(f"التصنيف \"{category['name']}\" فارغ حالياً.", call.message.chat.id, call.message.message_id,
                        reply_markup=create_combined_keyboard([], [], 0, 0, category_id))
                else:
                    keyboard = create_combined_keyboard(
                        child_categories=child_categories, videos=videos,
                        total_video_count=total_count, current_page=page,
                        parent_category_id=category_id
                    )
                    bot.edit_message_text(f"محتويات تصنيف \"{category['name']}\":", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                bot.answer_callback_query(call.id)

            elif action.startswith("search_"):
                query = user_last_search.get(call.message.chat.id)
                if not query:
                    bot.edit_message_text("انتهت صلاحية البحث، يرجى البحث مرة أخرى.", call.message.chat.id, call.message.message_id)
                    return
                if action == "search_scope":
                    scope = data[1]
                    page = 0
                    if scope == "all":
                        videos, total_count = search_videos(query, page=page)
                        keyboard = create_paginated_keyboard(videos, total_count, page, "search_all", "all")
                        bot.edit_message_text(f"نتائج البحث الشامل عن \"{query}\":", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                    else:
                        category_id = int(scope)
                        videos, total_count = search_videos(query, page=page, category_id=category_id)
                        category = get_category_by_id(category_id)
                        keyboard = create_paginated_keyboard(videos, total_count, page, "search_cat", category_id)
                        bot.edit_message_text(f"نتائج البحث عن \"{query}\" في \"{category['name']}\":", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                else:
                    _, context, page_str = data
                    page = int(page_str)
                    category_id = int(context) if context != "all" else None
                    videos, total_count = search_videos(query, page=page, category_id=category_id)
                    keyboard = create_paginated_keyboard(videos, total_count, page, action, context)
                    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                bot.answer_callback_query(call.id)
            
            elif action == "noop":
                bot.answer_callback_query(call.id)

        except Exception as e:
            logger.error(f"Callback query error: {e}", exc_info=True)
            try:
                bot.answer_callback_query(call.id, "حدث خطأ. حاول مرة أخرى.", show_alert=True)
            except Exception as e_inner:
                logger.error(f"Could not even answer callback query: {e_inner}")
