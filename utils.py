import re
import logging
from pymediainfo import MediaInfo
import os

logger = logging.getLogger(__name__)

def generate_title_and_key(caption):
    """
    Generates a clean title and a grouping key from a video caption.
    """
    if not caption:
        # استخدم مفتاحاً فريداً للفيديوهات بدون كابشن لتجنب تجميعها معاً
        return "فيديو بدون عنوان", f"no_caption_{os.urandom(4).hex()}"

    # 1. إنشاء العنوان عن طريق إزالة وسوم الجودة فقط
    title = caption
    quality_tags_for_title = [
        r'\b\d{3,4}p\b', r'\b(fhd|hd|sd)\b', r'full\s*hd'
    ]
    for tag in quality_tags_for_title:
        title = re.sub(tag, '', title, flags=re.IGNORECASE)
    
    # تنظيف الفواصل الإضافية التي قد تتبقى
    title = re.sub(r'\|\s*\|', '|', title) # "||" to "|"
    title = title.strip(' |-_')
    title = ' '.join(title.split())

    # 2. إنشاء مفتاح التجميع عن طريق تنظيف أكثر قوة
    key = title.lower()
    
    # إزالة الكلمات الوصفية التي لا تحدد هوية الفيلم
    extra_words = [
        r'مترجم', r'مدبلج', r'حصريا', r'حصري', r'نسخة', r'أصلية', r'جودة عالية'
    ]
    for word in extra_words:
        key = re.sub(word, '', key, flags=re.IGNORECASE)

    # توحيد الأحرف (مثل: أ، إ، آ -> ا)
    key = re.sub(r'[أإآ]', 'ا', key)
    key = re.sub(r'ة', 'ه', key)
    
    # إزالة جميع الأحرف غير الأبجدية الرقمية (ما عدا المسافات)
    key = re.sub(r'[^\w\s]', '', key)
    key = ' '.join(key.split())

    if not key:
        # حل بديل إذا كان الكابشن يحتوي فقط على كلمات مفتاحية تم حذفها
        return title, f"empty_key_{os.urandom(4).hex()}"

    return title, key

def get_video_info(file_path):
    """
    Extracts video information using MediaInfo.
    """
    try:
        media_info = MediaInfo.parse(file_path)
        video_track = next((t for t in media_info.tracks if t.track_type == 'Video'), None)
        
        if video_track:
            duration_ms = video_track.duration
            duration_seconds = duration_ms / 1000 if duration_ms else 0
            
            hours, remainder = divmod(duration_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            formatted_duration = f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}" if hours > 0 else f"{int(minutes):02d}:{int(seconds):02d}"

            return {
                "duration": formatted_duration,
                "width": video_track.width,
                "height": video_track.height,
                "file_size": video_track.file_size,
                "codec": video_track.codec,
                "frame_rate": video_track.frame_rate,
                "quality_resolution": f"{video_track.height}p" if video_track.height else "N/A"
            }
        return None
    except Exception as e:
        logger.error(f"Error extracting video info from '{file_path}': {e}", exc_info=True)
        return None
