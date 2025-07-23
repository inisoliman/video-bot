# ==============================================================================
# ملف: bot.py (النسخة النهائية مع تفعيل نظام فحص قاعدة البيانات)
# الوصف: نقطة انطلاق البوت التي تضمن سلامة قاعدة البيانات قبل بدء التشغيل.
# ==============================================================================

import telebot
import os
import time
import logging
# استيراد الاستثناء المخصص من المكتبة لمعالجة أخطاء API بشكل أفضل
from telebot.apihelper import ApiTelegramException

# استيراد الوظائف الجديدة من db_manager
from db_manager import verify_and_repair_schema
from handlers import register_handlers

# --- إعداد نظام التسجيل (Logging) ---
# هذا الإعداد يضمن أن أي سجلات من أي ملف سيتم توجيهها بشكل صحيح
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- الإعدادات الأساسية (قراءة آمنة من متغيرات البيئة) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL") # يتم استخدامه الآن داخل db_manager
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_IDS_STR = os.getenv("ADMIN_IDS", "")

if not all([BOT_TOKEN, DATABASE_URL, CHANNEL_ID, ADMIN_IDS_STR]):
    logger.critical("FATAL ERROR: Missing one or more environment variables (BOT_TOKEN, DATABASE_URL, CHANNEL_ID, ADMIN_IDS).")
    exit()

try:
    ADMIN_IDS = [int(admin_id) for admin_id in ADMIN_IDS_STR.split(",") if admin_id.strip()]
except ValueError:
    logger.critical("FATAL ERROR: ADMIN_IDS contains non-integer values.")
    exit()

bot = telebot.TeleBot(BOT_TOKEN, parse_mode='HTML') # استخدام HTML كوضع افتراضي للتنسيق

# --- نقطة انطلاق البوت ---

if __name__ == "__main__":
    logger.info("Bot is starting up...")

    # 1. التحقق من سلامة قاعدة البيانات وإصلاحها تلقائياً
    # هذه هي أهم خطوة جديدة لضمان استقرار البوت
    verify_and_repair_schema()

    # ملاحظة: لم نعد بحاجة لـ init_db() أو migrate_old_data() هنا
    # لأن verify_and_repair_schema يقوم بالمهمة بشكل أشمل وأكثر أماناً.
    # إذا كانت هناك عمليات ترحيل بيانات معقدة لاحقاً، يمكن إنشاء دالة منفصلة لها.

    # 2. تسجيل معالجات الأوامر والردود
    register_handlers(bot, CHANNEL_ID, ADMIN_IDS)
    
    logger.info("✅ Bot has started successfully and is now polling for updates.")
    
    # 3. حلقة التشغيل مع معالجة متقدمة للأخطاء
    while True:
        try:
            bot.polling(non_stop=True)
        except ApiTelegramException as e:
            logger.error(f"Telegram API Error caught: {e.description}")
            # معالجة خاصة لخطأ 409 الذي يعني وجود نسخة أخرى من البوت تعمل
            if e.error_code == 409:
                logger.warning("Conflict error (409): Another instance of the bot is likely running.")
                logger.warning("The script will wait for 1 minute before retrying, to allow the other instance to terminate.")
                logger.warning("Please check your hosting platform (e.g., Railway) to ensure only one instance is active.")
                time.sleep(60)
            else:
                logger.info("A non-conflict API error occurred. Retrying in 30 seconds...")
                time.sleep(30)
        except Exception as e:
            logger.error(f"An unexpected error occurred in the main polling loop: {e}", exc_info=True)
            logger.info("Restarting in 15 seconds...")
            time.sleep(15)
