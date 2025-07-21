
import re
import math
import json
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from pymediainfo import MediaInfo

VIDEOS_PER_PAGE = 10 # عدد الفيديوهات في كل صفحة
CALLBACK_DELIMITER = "::" # فاصل آمن للبيانات

def extract_video_metadata(caption):
    """استخلاص البيانات الوصفية من كابشن الفيديو."""
    metadata = {"qualities": [], "is_translated": False, "is_dubbed": False}
    if not caption:
        return metadata

    # استخلاص الجودات
    quality_patterns = {
        "1080p": [r"1080[pP]", r"FHD", r"Full\\s*HD"],
        "720p": [r"720[pP]", r"\\bHD\\b"],
        "480p": [r"480[pP]", r"\\bSD\\b"]
    }
    
    found_qualities = set()
    for res, patterns in quality_patterns.items():
        for pattern in patterns:
            if re.search(pattern, caption, re.IGNORECASE):
                found_qualities.add(res)
    
    # إضافة الجودات المكتشفة
    for quality in found_qualities:
        metadata["qualities"].append({"resolution": quality, "message_id": None})

    # استخلاص حالة الترجمة/الدبلجة
    if re.search(r"مترجم|sub|subbed|subtitle", caption, re.IGNORECASE):
        metadata["is_translated"] = True
    if re.search(r"مدبلج|dub|dubbed|arabic", caption, re.IGNORECASE):
        metadata["is_dubbed"] = True

    return metadata

def get_video_info(file_path):
    """استخلاص معلومات الفيديو باستخدام MediaInfo."""
    try:
        media_info = MediaInfo.parse(file_path)
        video_track = None
        for track in media_info.tracks:
            if track.track_type == \'Video\':
                video_track = track
                break
        
        if video_track:
            duration_ms = video_track.duration # in milliseconds
            duration_seconds = duration_ms / 1000 if duration_ms else 0
            
            # تنسيق المدة إلى H:MM:SS
            hours = int(duration_seconds // 3600)
            minutes = int((duration_seconds % 3600) // 60)
            seconds = int(duration_seconds % 60)
            formatted_duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours > 0 else f"{minutes:02d}:{seconds:02d}"

            return {
                "duration": formatted_duration,
                "width": video_track.width,
                "height": video_track.height,
                "file_size": video_track.file_size, # in bytes
                "codec": video_track.codec,
                "frame_rate": video_track.frame_rate,
                "overall_bit_rate": video_track.overall_bit_rate,
                "display_aspect_ratio": video_track.display_aspect_ratio,
                "quality_resolution": f"{video_track.height}p" if video_track.height else "N/A"
            }
        return None
    except Exception as e:
        print(f"Error extracting video info: {e}")
        return None

def create_paginated_keyboard(items, total_items, page, prefix, context):
    """إنشاء لوحة مفاتيح مع أزرار الصفحات وزر الرجوع."""
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    for item in items:
        video_id, message_id, caption, chat_id, file_name, metadata, category_name, view_count = item
        title = caption or file_name or "فيديو بدون عنوان"
        
        # إضافة مؤشرات الترجمة/الدبلجة
        indicators = []
        if metadata:
            try:
                meta_dict = json.loads(metadata) if isinstance(metadata, str) else metadata
                if meta_dict.get(\'is_translated\'):
                    indicators.append("مترجم")
                if meta_dict.get(\'is_dubbed\'):
                    indicators.append("مدبلج")
            except:
                pass
        
        indicator_text = f" ({\'\'.join(indicators)})" if indicators else ""
        
        video_details_text = ""
        if metadata:
            try:
                meta_dict = json.loads(metadata) if isinstance(metadata, str) else metadata
                video_details = meta_dict.get("video_details")
                if video_details:
                    duration = video_details.get("duration", "N/A")
                    quality = video_details.get("quality_resolution", "N/A")
                    file_size_bytes = video_details.get("file_size")
                    file_size_mb = f"{file_size_bytes / (1024 * 1024):.2f} MB" if file_size_bytes else "N/A"
                    video_details_text = f" | ⏱️ {duration} | 📏 {quality} | 💾 {file_size_mb}"
            except:
                pass

        button_text = f"{title[:35]}{indicator_text} - {category_name} 👁 {view_count}{video_details_text}"
        
        keyboard.add(InlineKeyboardButton(text=button_text, callback_data=f"video::{video_id}::{message_id}::{chat_id}"))

    # أزرار التنقل
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

def create_categories_keyboard(parent_id=None):
    """إنشاء لوحة مفاتيح للتصنيفات."""
    categories = get_child_categories(parent_id)
    keyboard = InlineKeyboardMarkup(row_width=2)
    
    for cat_id, cat_name, full_path in categories:
        keyboard.add(InlineKeyboardButton(text=cat_name, callback_data=f"cat::{cat_id}::0"))
    
    if parent_id:  # إذا كنا في تصنيف فرعي، أضف زر الرجوع
        parent_category = get_category_by_id(parent_id)
        if parent_category and parent_category[2] is not None:  # parent_id موجود
            keyboard.row(InlineKeyboardButton("⬅️ الرجوع", callback_data=f"cat::{parent_category[2]}::0"))
        else:
            keyboard.row(InlineKeyboardButton("⬅️ الرجوع للرئيسية", callback_data="back_to_cats"))
    
    return keyboard

def create_video_action_keyboard(video_id, user_id):
    """إنشاء لوحة مفاتيح لإجراءات الفيديو (تقييم، إلخ)."""
    keyboard = InlineKeyboardMarkup(row_width=5)
    
    # أزرار التقييم
    rating_buttons = []
    user_rating = get_user_video_rating(video_id, user_id)
    
    for i in range(1, 6):
        star = "⭐" if user_rating == i else "☆"
        rating_buttons.append(InlineKeyboardButton(star, callback_data=f"rate::{video_id}::{i}"))
    
    keyboard.row(*rating_buttons)
    
    # إحصائيات التقييم
    stats = get_video_rating_stats(video_id)
    if stats["total_ratings"] > 0:
        keyboard.row(InlineKeyboardButton(
            f"⭐ {stats[\'average_rating\']:.1f} ({stats[\'total_ratings\']} تقييم)", 
            callback_data="noop"
        ))
    
    return keyboard

def main_menu():
    """إنشاء القائمة الرئيسية للبوت."""
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    list_button = KeyboardButton(\'🎬 عرض كل الفيديوهات\')
    popular_button = KeyboardButton(\'🔥 الفيديوهات الشائعة\')
    markup.add(list_button, popular_button)
    return markup

def check_admin(func):
    """ديكوريتر للتحقق من صلاحيات الآدمن."""
    def wrapper(message, *args, **kwargs):
        if message.from_user.id not in ADMIN_IDS:
            bot.reply_to(message, "هذا الأمر مخصص للآدمن فقط.")
            return
        return func(message, *args, **kwargs)
    return wrapper

def check_subscription(user_id, channel_id, bot):
    """التحقق مما إذا كان المستخدم مشتركًا في قناة معينة."""
    try:
        member = bot.get_chat_member(channel_id, user_id)
        if member.status in ["member", "administrator", "creator"]:
            return True
        return False
    except Exception as e:
        print(f"Check subscription error for user {user_id} in channel {channel_id}: {e}")
        return False




import re
from pymediainfo import MediaInfo

def extract_video_metadata(caption):
    """استخلاص البيانات الوصفية من كابشن الفيديو."""
    metadata = {"qualities": [], "is_translated": False, "is_dubbed": False}
    if not caption:
        return metadata

    # استخلاص الجودات
    quality_patterns = {
        "1080p": [r"1080[pP]", r"FHD", r"Full\\s*HD"],
        "720p": [r"720[pP]", r"\\bHD\\b"],
        "480p": [r"480[pP]", r"\\bSD\\b"]
    }
    
    found_qualities = set()
    for res, patterns in quality_patterns.items():
        for pattern in patterns:
            if re.search(pattern, caption, re.IGNORECASE):
                found_qualities.add(res)
    
    # إضافة الجودات المكتشفة
    for quality in found_qualities:
        metadata["qualities"].append({"resolution": quality, "message_id": None})

    # استخلاص حالة الترجمة/الدبلجة
    if re.search(r"مترجم|sub|subbed|subtitle", caption, re.IGNORECASE):
        metadata["is_translated"] = True
    if re.search(r"مدبلج|dub|dubbed|arabic", caption, re.IGNORECASE):
        metadata["is_dubbed"] = True

    return metadata

def get_video_info(file_path):
    """استخلاص معلومات الفيديو باستخدام MediaInfo."""
    try:
        media_info = MediaInfo.parse(file_path)
        video_track = None
        for track in media_info.tracks:
            if track.track_type == 'Video':
                video_track = track
                break
        
        if video_track:
            duration_ms = video_track.duration # in milliseconds
            duration_seconds = duration_ms / 1000 if duration_ms else 0
            
            # تنسيق المدة إلى H:MM:SS
            hours = int(duration_seconds // 3600)
            minutes = int((duration_seconds % 3600) // 60)
            seconds = int(duration_seconds % 60)
            formatted_duration = f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours > 0 else f"{minutes:02d}:{seconds:02d}"

            return {
                "duration": formatted_duration,
                "width": video_track.width,
                "height": video_track.height,
                "file_size": video_track.file_size, # in bytes
                "codec": video_track.codec,
                "frame_rate": video_track.frame_rate,
                "overall_bit_rate": video_track.overall_bit_rate,
                "display_aspect_ratio": video_track.display_aspect_ratio,
                "quality_resolution": f"{video_track.height}p" if video_track.height else "N/A"
            }
        return None
    except Exception as e:
        print(f"Error extracting video info: {e}")
        return None



