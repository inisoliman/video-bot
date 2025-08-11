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
import time
import telebot

# --- إعداد نظام التسجيل (Logging) ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# --- إعدادات قاعدة البيانات (منسوخة من db_manager.py) ---
def get_db_connection():
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
        conn = psycopg2.connect(**DB_CONFIG)
        return conn
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        return None

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

# --- دالة التحديث الرئيسية ---
def run_update_and_report_progress(bot, chat_id, message_id):
    conn = get_db_connection()
    if not conn:
        bot.edit_message_text("❌ فشل الاتصال بقاعدة البيانات.", chat_id, message_id)
        return

    updated_count = 0
    total_videos = 0
    last_edit_time = 0
    
    try:
        with conn.cursor(cursor_factory=DictCursor) as c:
            logger.info("Fetching all videos from the database...")
            c.execute("SELECT id, caption, metadata FROM video_archive")
            videos = c.fetchall()
            total_videos = len(videos)
            
            if not videos:
                bot.edit_message_text("✅ لا توجد فيديوهات في قاعدة البيانات لتحديثها.", chat_id, message_id)
                return

            for i, video in enumerate(videos):
                if not video['caption']:
                    continue

                new_metadata = extract_video_metadata(video['caption'])
                old_metadata = video['metadata'] if video['metadata'] else {}
                
                # دمج البيانات مع الحفاظ على مدة الفيديو إن وجدت
                final_metadata = old_metadata.copy()
                final_metadata.update(new_metadata)

                if final_metadata != old_metadata:
                    metadata_json = json.dumps(final_metadata)
                    c.execute("UPDATE video_archive SET metadata = %s WHERE id = %s", (metadata_json, video['id']))
                    updated_count += 1
                
                # تحديث الرسالة بشكل دوري
                if time.time() - last_edit_time > 1.5 or (i + 1) == total_videos:
                    try:
                        progress = ((i + 1) / total_videos) * 100
                        bot.edit_message_text(f"⏳ جارِ تحديث البيانات... ({i + 1}/{total_videos}) - {progress:.0f}%", chat_id, message_id)
                        last_edit_time = time.time()
                    except telebot.apihelper.ApiTelegramException as e:
                        if 'message is not modified' in e.description:
                            continue
                        else:
                            logger.error(f"Error editing progress message: {e}")
            
            conn.commit()
            bot.edit_message_text(f"✅ اكتمل التحديث بنجاح!\n\n- تم فحص: {total_videos} فيديو.\n- تم تحديث بيانات: {updated_count} فيديو.", chat_id, message_id)

    except Exception as e:
        logger.error(f"An error occurred during the update process: {e}", exc_info=True)
        if conn:
            conn.rollback()
        bot.edit_message_text(f"❌ حدث خطأ أثناء التحديث: {e}", chat_id, message_id)
    finally:
        if conn:
            conn.close()

# --- نقطة انطلاق السكربت (للتشغيل اليدوي فقط) ---
if __name__ == "__main__":
    # هذا الجزء لن يعمل بدون كائن البوت، وهو مخصص للاختبار المحلي فقط
    logger.info("This script is intended to be called from the main bot.")
    logger.info("To run manually, you would need to provide a mock bot object.")
