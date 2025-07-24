import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
import os
import time
import re
import json
from urllib.parse import quote, unquote
import math
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

# --- استيراد الدوال من الملفات الأخرى ---
from db_manager import (
    add_category, get_categories_tree, get_child_categories, get_category_by_id,
    add_video, get_videos, get_video_group, increment_video_view_count, get_active_category_id,
    set_active_category_id, add_video_rating, get_video_rating_stats, get_user_video_rating,
    get_popular_videos, add_bot_user, get_all_user_ids, get_subscriber_count, get_bot_stats,
    search_videos, add_required_channel, remove_required_channel, get_required_channels,
    admin_steps, user_last_search, VIDEOS_PER_PAGE, CALLBACK_DELIMITER,
    migrate_old_videos_metadata
)
from utils import generate_title_and_key

# --- متغيرات عامة ---
bot = None
CHANNEL_ID = None
ADMIN_IDS = []

def register_handlers(telebot_instance, channel_id, admin_ids):
    """تسجيل جميع معالجات الأوامر والردود للبوت."""
    global bot, CHANNEL_ID, ADMIN_IDS
    bot = telebot_instance
    CHANNEL_ID = str(channel_id)
    ADMIN_IDS = admin_ids

    # --- دوال مساعدة ---
    def format_size(size_bytes):
        if not size_bytes: return ""
        if size_bytes < 1024*1024: return f"{size_bytes/1024:.0f} KB"
        size_mb = size_bytes / (1024 * 1024)
        return f"{size_mb / 1024:.2f} GB" if size_mb >= 1024 else f"{size_mb:.1f} MB"

    def check_admin(func):
        def wrapper(message):
            if message.from_user.id in ADMIN_IDS:
                return func(message)
            bot.reply_to(message, "ليس لديك صلاحية الوصول لهذا الأمر.")
        return wrapper

    def check_subscription(user_id, channel_id):
        try:
            member = bot.get_chat_member(channel_id, user_id)
            return member.status in ['member', 'administrator', 'creator']
        except telebot.apihelper.ApiTelegramException as e:
            if 'chat not found' in e.description:
                logger.warning(f"Channel {channel_id} not found.")
                return True
            return False

    # --- لوحات المفاتيح ---
    def main_menu():
        markup = ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(KeyboardButton("🎬 عرض كل الفيديوهات"), KeyboardButton("🔥 الفيديوهات الشائعة"))
        markup.add(KeyboardButton("🔍 بحث"))
        return markup

    def create_categories_keyboard(parent_id=None):
        keyboard = InlineKeyboardMarkup(row_width=2)
        categories = get_child_categories(parent_id)
        buttons = [InlineKeyboardButton(cat['name'], callback_data=f"cat::{cat['id']}::0") for cat in categories]
        keyboard.add(*buttons)
        if parent_id:
            parent_category = get_category_by_id(parent_id)
            back_target = parent_category['parent_id'] if parent_category and parent_category.get('parent_id') is not None else ""
            keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data=f"cat::{back_target}::0"))
        return keyboard

    def create_paginated_keyboard(video_groups, total_count, current_page, action_prefix, context_id):
        keyboard = InlineKeyboardMarkup(row_width=1)
        for group in video_groups:
            title = group['title'] or group.get('caption', '').split('\n')[0] or "فيديو مجمع"
            rating_text = f" ⭐ {group['avg_rating']:.1f}" if group.get('avg_rating', 0) > 0 else ""
            keyboard.add(InlineKeyboardButton(f"{title}{rating_text}", callback_data=f"group::{quote(group['grouping_key'])}"))
        
        nav_buttons = []
        total_pages = math.ceil(total_count / VIDEOS_PER_PAGE)
        if current_page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"{action_prefix}::{context_id}::{current_page - 1}"))
        if current_page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("التالي ➡️", callback_data=f"{action_prefix}::{context_id}::{current_page + 1}"))
        if nav_buttons: keyboard.add(*nav_buttons)
        
        keyboard.add(InlineKeyboardButton("🔙 رجوع للتصنيفات الرئيسية", callback_data="back_to_cats"))
        return keyboard
        
    def create_video_action_keyboard(video_id, user_id):
        keyboard = InlineKeyboardMarkup(row_width=5)
        user_rating = get_user_video_rating(video_id, user_id)
        buttons = [InlineKeyboardButton("⭐" if user_rating == i else "☆", callback_data=f"rate::{video_id}::{i}") for i in range(1, 6)]
        keyboard.add(*buttons)
        stats = get_video_rating_stats(video_id)
        if stats and stats['avg']:
            keyboard.add(InlineKeyboardButton(f"متوسط: {stats['avg']:.1f} ({stats['count']} تقييم)", callback_data="noop"))
        return keyboard

    # --- المعالجات الرئيسية ---
    @bot.message_handler(commands=["start"])
    def start(message):
        add_bot_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
        required_channels = get_required_channels()
        if required_channels:
            not_subscribed = [ch for ch in required_channels if not check_subscription(message.from_user.id, ch['channel_id'])]
            if not_subscribed:
                markup = InlineKeyboardMarkup()
                for ch in not_subscribed:
                    link_id = str(ch['channel_id']).replace("-100", "")
                    markup.add(InlineKeyboardButton(f"اشترك في {ch['channel_name']}", url=f"https://t.me/c/{link_id}"))
                bot.reply_to(message, "يرجى الاشتراك في القنوات التالية لاستخدام البوت:", reply_markup=markup)
                return
        bot.reply_to(message, "أهلاً بك!", reply_markup=main_menu())

    @bot.message_handler(func=lambda m: m.text == "🎬 عرض كل الفيديوهات")
    def handle_list_videos_button(message):
        list_videos(message)

    @bot.message_handler(func=lambda m: m.text == "🔥 الفيديوهات الشائعة")
    def handle_popular_videos_button(message):
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.add(InlineKeyboardButton("📈 الأكثر مشاهدة", callback_data="popular::most_viewed::0"))
        keyboard.add(InlineKeyboardButton("⭐ الأعلى تقييماً", callback_data="popular::highest_rated::0"))
        bot.reply_to(message, "اختر نوع الفيديوهات الشائعة:", reply_markup=keyboard)

    @bot.message_handler(func=lambda m: m.text == "🔍 بحث")
    def handle_search_button(message):
        msg = bot.reply_to(message, "أرسل كلمة البحث:")
        bot.register_next_step_handler(msg, perform_search)

    def perform_search(message):
        query = message.text.strip()
        user_last_search[message.chat.id] = query
        videos, total = search_videos(query, page=0)
        if not videos:
            bot.reply_to(message, f"لم يتم العثور على نتائج للبحث عن '{query}'.")
            return
        keyboard = create_paginated_keyboard(videos, total, 0, "search_all", "all")
        bot.reply_to(message, f"نتائج البحث عن '{query}':", reply_markup=keyboard)

    def list_videos(message, edit_message=None, parent_id=None):
        text = "اختر تصنيفًا:"
        keyboard = create_categories_keyboard(parent_id)
        if not keyboard.keyboard:
            text = "لا توجد تصنيفات متاحة حالياً."
        
        if edit_message:
            bot.edit_message_text(text, edit_message.chat.id, edit_message.message_id, reply_markup=keyboard)
        else:
            bot.reply_to(message, text, reply_markup=keyboard)

    @bot.message_handler(content_types=["video"])
    def handle_new_video(message):
        if str(message.chat.id) != CHANNEL_ID: return
        active_category_id = get_active_category_id()
        if not active_category_id:
            logger.warning(f"No active category set for new video in chat {message.chat.id}.")
            return

        title, grouping_key = generate_title_and_key(message.caption)
        video_info = {
            "duration": message.video.duration, "width": message.video.width,
            "height": message.video.height, "file_size": message.video.file_size,
            "quality_resolution": f"{message.video.height}p" if message.video.height else "N/A"
        }
        add_video(
            message_id=message.message_id, caption=message.caption, chat_id=message.chat.id,
            file_name=message.video.file_name, category_id=active_category_id, file_id=message.video.file_id,
            video_info=video_info, title=title, grouping_key=grouping_key
        )
        logger.info(f"Added/Updated video: {title}")

    # --- لوحة تحكم الآدمن ---
    def generate_admin_panel():
        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            InlineKeyboardButton("📢 بث للكل", callback_data="admin::broadcast"),
            InlineKeyboardButton("👤 عدد المشتركين", callback_data="admin::sub_count"),
            InlineKeyboardButton("📊 الإحصائيات", callback_data="admin::stats"),
            InlineKeyboardButton("➕ إضافة تصنيف", callback_data="admin::add_new_cat"),
            InlineKeyboardButton("🔘 تعيين النشط", callback_data="admin::set_active"),
            InlineKeyboardButton("🔄 تحديث القديم", callback_data="admin::updatemeta")
        )
        return keyboard
        
    @bot.message_handler(commands=["admin"])
    @check_admin
    def admin_panel(message):
        bot.send_message(message.chat.id, "أهلاً بك في لوحة تحكم الآدمن.", reply_markup=generate_admin_panel())

    # --- إصلاح صلاحيات الأدمن: دالة داخلية بدون مصادقة ---
    def _run_update_metadata(message):
        bot.send_message(message.chat.id, "⏳ جاري تحديث بيانات الفيديوهات القديمة...")
        updated_count = migrate_old_videos_metadata()
        bot.send_message(message.chat.id, f"✅ اكتمل! تم تحديث {updated_count} فيديو.")

    @bot.message_handler(commands=["updatemetadata"])
    @check_admin
    def update_metadata_command(message):
        _run_update_metadata(message)

    def admin_broadcast_step(message):
        if message.text == '/cancel':
            bot.send_message(message.chat.id, "تم إلغاء البث.")
            return
        user_ids = get_all_user_ids()
        sent_count, failed_count = 0, 0
        bot.send_message(message.chat.id, f"بدء البث إلى {len(user_ids)} مشترك...")
        for user_id in user_ids:
            try:
                bot.copy_message(user_id, message.chat.id, message.message_id)
                sent_count += 1
            except Exception as e:
                failed_count += 1
                logger.warning(f"Failed to broadcast to {user_id}: {e}")
            time.sleep(0.1)
        bot.send_message(message.chat.id, f"✅ اكتمل البث!\nناجح: {sent_count}\nفاشل: {failed_count}")

    def admin_add_cat_step(message):
        if message.text == '/cancel':
            bot.send_message(message.chat.id, "تم الإلغاء.")
            return
        success, result = add_category(message.text.strip())
        if success:
            bot.send_message(message.chat.id, f"✅ تم إنشاء التصنيف '{message.text.strip()}' بنجاح.")
        else:
            bot.send_message(message.chat.id, f"❌ فشل إنشاء التصنيف: {result}")

    # --- معالج الردود (Callback Query Handler) ---
    @bot.callback_query_handler(func=lambda call: True)
    def callback_query(call):
        try:
            parts = call.data.split(CALLBACK_DELIMITER)
            action = parts[0]

            if action == "admin":
                if call.from_user.id not in ADMIN_IDS:
                    bot.answer_callback_query(call.id, "ليس لديك صلاحية الوصول.", show_alert=True)
                    return
                
                sub_action = parts[1]
                bot.answer_callback_query(call.id)
                if sub_action == "updatemeta":
                    _run_update_metadata(call.message)
                elif sub_action == "broadcast":
                    msg = bot.send_message(call.message.chat.id, "أرسل الرسالة التي تريد بثها (أو /cancel للإلغاء).")
                    bot.register_next_step_handler(msg, admin_broadcast_step)
                elif sub_action == "sub_count":
                    bot.send_message(call.message.chat.id, f"👤 عدد المشتركين: {get_subscriber_count()}")
                elif sub_action == "stats":
                    stats = get_bot_stats()
                    text = (f"📊 *الإحصائيات:*\n"
                            f"- الفيديوهات: *{stats['video_count']}*\n"
                            f"- التصنيفات: *{stats['category_count']}*\n"
                            f"- المشاهدات: *{stats['total_views']}*\n"
                            f"- التقييمات: *{stats['total_ratings']}*")
                    bot.send_message(call.message.chat.id, text, parse_mode="Markdown")
                elif sub_action == "add_new_cat":
                    msg = bot.send_message(call.message.chat.id, "أرسل اسم التصنيف الجديد (أو /cancel للإلغاء).")
                    bot.register_next_step_handler(msg, admin_add_cat_step)
                elif sub_action == "set_active":
                    cats = get_categories_tree()
                    if not cats:
                        bot.send_message(call.message.chat.id, "لا توجد تصنيفات.")
                        return
                    keyboard = InlineKeyboardMarkup(row_width=2)
                    buttons = [InlineKeyboardButton(c['name'], callback_data=f"setcat::{c['id']}") for c in cats]
                    keyboard.add(*buttons)
                    bot.edit_message_text("اختر التصنيف النشط:", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                return

            elif action == "setcat":
                cat_id = int(parts[1])
                set_active_category_id(cat_id)
                cat_name = get_category_by_id(cat_id)['name']
                bot.edit_message_text(f"✅ تم تفعيل التصنيف '{cat_name}'.", call.message.chat.id, call.message.message_id)
                bot.answer_callback_query(call.id)

            elif action == "group":
                group_key = unquote(parts[1])
                videos_in_group = get_video_group(group_key)
                if not videos_in_group:
                    bot.answer_callback_query(call.id, "لم يتم العثور على الفيديو.", show_alert=True)
                    return

                group_title = videos_in_group[0]['title'] or "تفاصيل الفيديو"
                details_text = f"🎬 *{group_title}*\n\nاختر الجودة:"
                keyboard = InlineKeyboardMarkup(row_width=1)
                for video in videos_in_group:
                    meta = video['metadata'] or {}
                    quality = meta.get('quality_resolution', 'N/A')
                    duration_sec = meta.get('duration')
                    size_bytes = meta.get('file_size')
                    duration_str = ""
                    if duration_sec:
                        mins, secs = divmod(int(duration_sec), 60)
                        hours, mins = divmod(mins, 60)
                        duration_str = f" | {hours:02d}:{mins:02d}:{secs:02d}" if hours > 0 else f" | {mins:02d}:{secs:02d}"
                    size_str = f" | {format_size(size_bytes)}" if size_bytes else ""
                    button_text = f"▶️ {quality}{duration_str}{size_str}"
                    callback = f"send_video::{video['id']}::{video['message_id']}::{video['chat_id']}"
                    keyboard.add(InlineKeyboardButton(button_text, callback_data=callback))
                
                keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data=f"cat::{videos_in_group[0]['category_id']}::0"))
                bot.edit_message_text(details_text, call.message.chat.id, call.message.message_id, reply_markup=keyboard, parse_mode="Markdown")
                bot.answer_callback_query(call.id)

            elif action == "send_video":
                _, video_db_id, message_id, chat_id = parts
                increment_video_view_count(int(video_db_id))
                try:
                    bot.copy_message(call.message.chat.id, chat_id, int(message_id))
                    rating_keyboard = create_video_action_keyboard(int(video_db_id), call.from_user.id)
                    bot.send_message(call.message.chat.id, "قيّم هذا الفيديو:", reply_markup=rating_keyboard)
                    bot.answer_callback_query(call.id)
                except Exception as e:
                    logger.error(f"Error sending video (msg_id: {message_id}): {e}", exc_info=True)
                    bot.answer_callback_query(call.id, "خطأ: تعذر إرسال الفيديو.", show_alert=True)
            
            elif action == "rate":
                _, video_id, rating = parts
                add_video_rating(int(video_id), call.from_user.id, int(rating))
                new_keyboard = create_video_action_keyboard(int(video_id), call.from_user.id)
                bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=new_keyboard)
                bot.answer_callback_query(call.id, f"تم تقييم الفيديو بـ {rating} نجوم!")

            elif action == "cat":
                _, category_id_str, page_str = parts
                page = int(page_str)
                category_id = int(category_id_str) if category_id_str else None
                
                if category_id and get_child_categories(category_id):
                    list_videos(call.message, edit_message=call.message, parent_id=category_id)
                else:
                    videos, total = get_videos(category_id=category_id, page=page)
                    category = get_category_by_id(category_id) if category_id else None
                    cat_name = category['name'] if category else "التصنيفات الرئيسية"
                    
                    if videos:
                        keyboard = create_paginated_keyboard(videos, total, page, "cat", category_id or "")
                        bot.edit_message_text(f"الفيديوهات في '{cat_name}':", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                    else:
                        keyboard = InlineKeyboardMarkup()
                        parent_id_of_empty_cat = ""
                        if category:
                            parent_id_of_empty_cat = category.get('parent_id') or ""
                        keyboard.add(InlineKeyboardButton("🔙 رجوع", callback_data=f"cat::{parent_id_of_empty_cat}::0"))
                        bot.edit_message_text(f"لا توجد فيديوهات في تصنيف '{cat_name}'.", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                bot.answer_callback_query(call.id)

            elif action == "search_all":
                _, context, page_str = parts
                page = int(page_str)
                query = user_last_search.get(call.message.chat.id)
                if not query:
                    bot.answer_callback_query(call.id, "انتهت صلاحية البحث.", show_alert=True)
                    return
                videos, total = search_videos(query, page=page)
                keyboard = create_paginated_keyboard(videos, total, page, "search_all", context)
                bot.edit_message_text(f"نتائج البحث عن '{query}':", call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                bot.answer_callback_query(call.id)

            elif action == "popular":
                _, pop_type, page_str = parts
                page = int(page_str)
                popular_data = get_popular_videos()
                videos = popular_data.get(pop_type, [])
                if videos:
                    title = "📈 الأكثر مشاهدة" if pop_type == "most_viewed" else "⭐ الأعلى تقييماً"
                    keyboard = create_paginated_keyboard(videos, len(videos), page, "popular", pop_type)
                    bot.edit_message_text(title, call.message.chat.id, call.message.message_id, reply_markup=keyboard)
                else:
                    bot.edit_message_text("لا توجد بيانات لعرضها.", call.message.chat.id, call.message.message_id)
                bot.answer_callback_query(call.id)

            elif action == "back_to_cats":
                list_videos(call.message, edit_message=call.message, parent_id=None)
                bot.answer_callback_query(call.id)
                
            elif action == "noop":
                bot.answer_callback_query(call.id)

        except Exception as e:
            logger.error(f"Critical callback error: {e}", exc_info=True)
            try:
                bot.answer_callback_query(call.id, "حدث خطأ فادح.", show_alert=True)
            except: pass
