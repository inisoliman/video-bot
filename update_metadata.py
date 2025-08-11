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

# --- إعدادات قاعدة البيانات (منسوخة من db_manager.py) ---
try:
    DATABASE_URL = os.environ.get('DATABASE_URL')
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL environment variable not set.")
        
    result = urlparse(DATABASE_URL)
    DB_CONFIG = {
        'user': result.username,
        'password': result.password,
        'host': result.hostname,
        'port': result.port,
        'dbname': result.path[1:]
    }
except Exception as e:
    logger.critical(f"FATAL: Could not parse DATABASE_URL. Error: {e}")
    exit()

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

    season_match = re.search(r'(الموسم|season)\s+([a-zA-Z]+|\d+)', caption, re.IGNORECASE)
    if season_match:
        season_str = season_match.group(2)
        if season_str.isdigit():
            metadata['season'] = int(season_str)
        else:
            season_num = arabic_word_to_int(season_str)
            if season_num:
                metadata['season'] = season_num

    episode_match = re.search(r'(الحلقة|episode)\s+([a-zA-Z]+|\d+)', caption, re.IGNORECASE)
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
    else:
        metadata['series_name'] = caption.split('\n')[0].strip()

    quality_match = re.search(r'(\d{3,4})[pP]', caption)
    if quality_match:
        metadata['quality_resolution'] = f"{quality_match.group(1)}p"

    return metadata

# --- دالة التحديث الرئيسية ---
def update_all_videos_metadata():
    """
    تجلب كل الفيديوهات من قاعدة البيانات، تعيد تحليل الكابشن،
    وتحدث حقل البيانات الوصفية (metadata).
    """
    conn = None
    updated_count = 0
    total_videos = 0
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor(cursor_factory=DictCursor)
        
        logger.info("Fetching all videos from the database...")
        c.execute("SELECT id, caption, metadata FROM video_archive")
        videos = c.fetchall()
        total_videos = len(videos)
        
        if not videos:
            logger.info("No videos found in the database. Nothing to do.")
            return 0, 0

        logger.info(f"Found {total_videos} videos. Starting metadata update process...")
        
        for video in videos:
            if not video['caption']:
                continue

            new_metadata = extract_video_metadata(video['caption'])
            old_metadata = video['metadata'] if video['metadata'] else {}
            final_metadata = {**old_metadata, **new_metadata}
            metadata_json = json.dumps(final_metadata)
            
            c.execute("UPDATE video_archive SET metadata = %s WHERE id = %s", (metadata_json, video['id']))
            updated_count += 1
            
            if updated_count % 100 == 0:
                logger.info(f"Processed {updated_count}/{total_videos} videos...")

        conn.commit()
        logger.info(f"✅ Successfully updated metadata for {updated_count} videos!")
        return updated_count, total_videos

    except Exception as e:
        logger.error(f"An error occurred during the update process: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return updated_count, total_videos
    finally:
        if conn:
            conn.close()

# --- نقطة انطلاق السكربت (للتشغيل اليدوي فقط) ---
if __name__ == "__main__":
    logger.info("Starting the one-time metadata update script manually.")
    count, total = update_all_videos_metadata()
    logger.info(f"Script finished. Updated {count}/{total} videos.")
