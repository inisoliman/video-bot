import re
from pymediainfo import MediaInfo
import logging

# إعداد المسجل (logger) لهذا الملف
logger = logging.getLogger(__name__)

def arabic_word_to_int(word):
    """يحول الكلمات العربية للأرقام إلى أعداد صحيحة."""
    num_map = {
        'الاول': 1, 'الأول': 1, 'الاولى': 1, 'الأولى': 1, 'واحد': 1,
        'الثاني': 2, 'الثانية': 2, 'اثنين': 2,
        'الثالث': 3, 'الثالثة': 3, 'ثلاثة': 3,
        'الرابع': 4, 'الرابعة': 4, 'اربعة': 4,
        'الخامس': 5, 'الخامسة': 5, 'خمسة': 5,
        'السادس': 6, 'السادسة': 6, 'ستة': 6,
        'السابع': 7, 'السابعة': 7, 'سبعة': 7,
        'الثامن': 8, 'الثامنة': 8, 'ثمانية': 8,
        'التاسع': 9, 'التاسعة': 9, 'تسعة': 9,
        'العاشر': 10, 'العاشرة': 10, 'عشرة': 10,
    }
    return num_map.get(word)

def extract_video_metadata(caption):
    """
    محلل ذكي لاستخلاص البيانات الوصفية من كابشن الفيديو.
    يستخلص اسم المسلسل، الموسم، الحلقة، والحالة (مترجم/مدبلج).
    """
    metadata = {}
    if not caption:
        return metadata

    # 1. استخلاص الموسم (Season)
    season_match = re.search(r'(الموسم|season)\s+([a-zA-Z]+|\d+)', caption, re.IGNORECASE)
    if season_match:
        season_str = season_match.group(2)
        if season_str.isdigit():
            metadata['season'] = int(season_str)
        else:
            season_num = arabic_word_to_int(season_str)
            if season_num:
                metadata['season'] = season_num

    # 2. استخلاص الحلقة (Episode)
    episode_match = re.search(r'(الحلقة|episode)\s+([a-zA-Z]+|\d+)', caption, re.IGNORECASE)
    if episode_match:
        episode_str = episode_match.group(2)
        if episode_str.isdigit():
            metadata['episode'] = int(episode_str)
        else:
            episode_num = arabic_word_to_int(episode_str)
            if episode_num:
                metadata['episode'] = episode_num
    
    # 3. استخلاص الحالة (مترجم/مدبلج)
    if re.search(r'مترجم|sub|subbed|subtitle', caption, re.IGNORECASE):
        metadata['status'] = 'مترجم'
    elif re.search(r'مدبلج|dub|dubbed', caption, re.IGNORECASE):
        metadata['status'] = 'مدبلج'

    # 4. استخلاص اسم المسلسل (Series Name) - هذا الجزء تجريبي
    # يبحث عن كلمة "مسلسل" ويأخذ الكلمة التي تليها كاسم محتمل
    series_match = re.search(r'(مسلسل|series)\s+([a-zA-Z0-9_]+)', caption, re.IGNORECASE)
    if series_match:
        metadata['series_name'] = series_match.group(2).strip()
    else:
        # كحل بديل، نأخذ السطر الأول إذا لم نجد كلمة "مسلسل"
        metadata['series_name'] = caption.split('\n')[0].strip()

    # 5. استخلاص الجودة
    quality_match = re.search(r'(\d{3,4})[pP]', caption)
    if quality_match:
        metadata['quality_resolution'] = f"{quality_match.group(1)}p"

    return metadata


def get_video_info(file_path):
    """استخلاص معلومات الفيديو باستخدام MediaInfo."""
    try:
        media_info = MediaInfo.parse(file_path)
        video_track = next((t for t in media_info.tracks if t.track_type == 'Video'), None)
        
        if video_track:
            duration_ms = video_track.duration
            duration_seconds = duration_ms / 1000 if duration_ms else 0
            
            return {
                "duration": duration_seconds, # نرجع الثواني ليتم تنسيقها لاحقاً
                "width": video_track.width,
                "height": video_track.height,
                "file_size": video_track.file_size,
                "quality_resolution": f"{video_track.height}p" if video_track.height else "N/A"
            }
        return None
    except Exception as e:
        logger.error(f"Error extracting video info from '{file_path}': {e}", exc_info=True)
        return None
