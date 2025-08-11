# ==============================================================================
# ملف: update_metadata.py
# الوصف: هذا السكربت يستخدم لمرة واحدة فقط لتحديث البيانات الوصفية (metadata)
#        لجميع الفيديوهات القديمة في قاعدة البيانات بناءً على المحلل الذكي الجديد.
# ==============================================================================

import psycopg2
from psycopg2.extras import DictCursor
import os
from urllib.parse import urlparse
import logging
import re
import json

# --- إعداد نظام التسجيل (Logging) ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# --- المحلل الذكي (منسوخ من utils.py) ---
def arabic_word_to_int(word):
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
    metadata = {}
    if not caption:
        return metadata

    season_match = re.search(r'\b(الموسم|season)\s+([a-zA-Z]+|\d+)\b', caption, re.IGNORECASE)
    if season_match:
        season_str = season_match.group(2)
        if season_str.isdigit():
            metadata['season'] = int(season_str)
        else:
            season_num = arabic_word_to_int(season_str)
            if season_num:
                metadata['season'] = season_num

    episode_match = re.search(r'\b(الحلقة|episode)\s+([a-zA-Z]+|\d+)\b', caption, re.IGNORECASE)
    if episode_match:
        episode_str = episode_match.group(2)
        if episode_str.isdigit():
            metadata['episode'] = int(episode_str)
        else:
            episode_num = arabic_word_to_int(episode_str)
            if episode_num:
                metadata['episode'] = episode_num
    
    if re.search(r'مترجم|sub|subbed|subtitle', caption, re.IGNORECASE):
        metadata['status'] = 'مترجم'
    elif re.search(r'مدبلج|dub|dubbed', caption, re.IGNORECASE):
        metadata['status'] = 'مدبلج'

    series_match = re.search(r'(مسلسل|series)\s+([a-zA-Z0-9_]+)', caption, re.IGNORECASE)
    if series_match:
        metadata['series_name'] = series_match.group(2).strip()

    quality_match = re.search(r'(\d{3,4})[pP]', caption)
    if quality_match:
        metadata['quality_resolution'] = f"{quality_match.group(1)}p"

    return metadata

# --- دالة التحديث الرئيسية (المعدلة لتعمل كمولد Generator) ---
def update_all_videos_metadata_generator(conn):
    """
    تجلب كل الفيديوهات، تعيد تحليل الكابشن، وتحدث البيانات،
    مع إرسال تحديثات عن التقدم.
    """
    updated_count = 0
    total_videos = 0
    try:
        with conn.cursor(cursor_factory=DictCursor) as c:
            c.execute("SELECT id, caption, metadata FROM video_archive")
            videos = c.fetchall()
            total_videos = len(videos)
            
            if not videos:
                yield "done", 0, 0
                return

            for i, video in enumerate(videos):
                if not video['caption']:
                    continue

                new_metadata = extract_video_metadata(video['caption'])
                old_metadata = video['metadata'] if video['metadata'] else {}
                final_metadata = {**old_metadata, **new_metadata}

                if final_metadata != old_metadata:
                    metadata_json = json.dumps(final_metadata)
                    c.execute("UPDATE video_archive SET metadata = %s WHERE id = %s", (metadata_json, video['id']))
                    updated_count += 1
                
                # إرسال تحديث عن التقدم كل 50 فيديو
                if (i + 1) % 50 == 0 or (i + 1) == total_videos:
                    yield "progress", (i + 1), total_videos

            conn.commit()
            yield "done", updated_count, total_videos

    except Exception as e:
        logger.error(f"An error occurred during the update process: {e}", exc_info=True)
        if conn:
            conn.rollback()
        yield "error", str(e), None
