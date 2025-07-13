import telebot
import psycopg2
import os
import time
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from urllib.parse import urlparse

# Get BOT_TOKEN from environment
BOT_TOKEN = os.getenv('BOT_TOKEN', '7770980051:AAHasDgP4Bb5uYDH0upTUjGeJtrY17THu9s')
# Removed skip_pending=True to prevent potential startup conflicts.
bot = telebot.TeleBot(BOT_TOKEN)

# Get DATABASE_URL from environment
DATABASE_URL = os.getenv('DATABASE_URL', '')
if DATABASE_URL:
    url = urlparse(DATABASE_URL)
    DB_CONFIG = {
        'dbname': url.path[1:],  # Remove leading "/"
        'user': url.username,
        'password': url.password,
        'host': url.hostname,
        'port': url.port
    }
else:
    DB_CONFIG = {}  # Fallback if no DATABASE_URL

# Replace 'YOUR_CHANNEL_ID' with your new supergroup's chat_id
CHANNEL_ID = '-1002674581978'

# Global flags to coordinate fetching and polling
FETCH_REQUESTED = False
LAST_FETCH_CHAT_ID = None

# Initialize database
def init_db():
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS videos (
                id SERIAL PRIMARY KEY,
                message_id INTEGER,
                caption TEXT,
                chat_id INTEGER,
                file_name TEXT,
                category TEXT DEFAULT 'Uncategorized'
            )
        ''')
        conn.commit()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Database error: {e}")

# Add a video to the database
def add_video(message_id, caption, chat_id, file_name=None, category='Uncategorized'):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        c.execute("INSERT INTO videos (message_id, caption, chat_id, file_name, category) VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                  (message_id, caption or "No caption", chat_id, file_name or "", category))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Add video error: {e}")

# Retrieve all videos
def get_videos(category=None):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        if category:
            c.execute("SELECT message_id, caption, chat_id, file_name, category FROM videos WHERE category = %s", (category,))
        else:
            c.execute("SELECT message_id, caption, chat_id, file_name, category FROM videos")
        videos = c.fetchall()
        conn.close()
        return videos
    except Exception as e:
        print(f"Get videos error: {e}")
        return []

# Search for videos
def search_videos(query, category=None):
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        c = conn.cursor()
        if category:
            c.execute("SELECT message_id, caption, chat_id, file_name, category FROM videos WHERE (caption LIKE %s OR file_name LIKE %s) AND category = %s",
                      ('%' + query + '%', '%' + query + '%', category))
        else:
            c.execute("SELECT message_id, caption, chat_id, file_name, category FROM videos WHERE caption LIKE %s OR file_name LIKE %s",
                      ('%' + query + '%', '%' + query + '%'))
        results = c.fetchall()
        conn.close()
        return results
    except Exception as e:
        print(f"Search videos error: {e}")
        return []

# Fetch old videos
def fetch_old_videos():
    offset = 0
    while True:
        try:
            # This is a blocking call that can conflict if polling is also running.
            updates = bot.get_updates(offset=offset, limit=100, timeout=30)
            if not updates:
                print("No more updates to fetch.")
                break
            for update in updates:
                offset = update.update_id + 1
                message = update.message or update.channel_post
                if message and (message.chat.type == 'supergroup' or message.chat.type == 'channel'):
                    if str(message.chat.id) == CHANNEL_ID and message.video:
                        caption = message.caption or "No caption"
                        file_name = message.video.file_name if message.video.file_name else ""
                        add_video(message.message_id, caption, CHANNEL_ID, file_name)
                        print(f"Fetched video with ID: {message.message_id}")
            time.sleep(1)
        except Exception as e:
            print(f"Error fetching messages: {e}")
            # Re-raise the exception so the main loop can handle it
            raise e

# Handle commands
@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "Welcome! Send a movie name to search, /fetch to retrieve videos, /add_category <name> to add category, or /list to view categories.")

@bot.message_handler(commands=['fetch'])
def fetch_videos_command(message):
    global FETCH_REQUESTED, LAST_FETCH_CHAT_ID
    if FETCH_REQUESTED:
        bot.reply_to(message, "Fetch is already scheduled or in progress.")
        return
    
    bot.reply_to(message, "Scheduled a fetch for old videos. The bot will temporarily stop, fetch, and then restart automatically. I will notify you when it's complete.")
    FETCH_REQUESTED = True
    LAST_FETCH_CHAT_ID = message.chat.id # Store chat_id to send completion message
    bot.stop_polling()

@bot.message_handler(commands=['add_category'])
def add_category(message):
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "Please provide a category name. Example: /add_category Action")
        return
    category = args[1]
    bot.reply_to(message, f"Category '{category}' added! Use /list to see it.")

@bot.message_handler(commands=['list'])
def list_videos(message):
    videos = get_videos()
    if not videos:
        bot.reply_to(message, "No videos found.")
        return
    categories = set(video[4] for video in videos)
    keyboard = InlineKeyboardMarkup()
    for cat in categories:
        keyboard.add(InlineKeyboardButton(text=cat, callback_data=f"cat_{cat}"))
    bot.reply_to(message, "Select a category:", reply_markup=keyboard)

# Handle text messages for automatic search
@bot.message_handler(content_types=['text'])
def auto_search(message):
    query = message.text.strip()
    if not query or query.startswith('/'):
        return

    results = search_videos(query)
    if not results:
        bot.reply_to(message, f"No results found for '{query}'.")
        return

    keyboard = InlineKeyboardMarkup()
    for video in results:
        caption = video[1] or video[3] or "No title"
        keyboard.add(InlineKeyboardButton(text=f"{caption[:50]} ({video[4]})", callback_data=f"video_{video[0]}_{video[2]}"))
    bot.reply_to(message, f"Search results for '{query}':", reply_markup=keyboard)

# Handle button clicks
@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    if call.data.startswith("video_"):
        _, message_id, chat_id = call.data.split('_')
        try:
            bot.copy_message(call.message.chat.id, chat_id, int(message_id))
        except Exception as e:
            bot.answer_callback_query(call.id, f"Error sending video: {str(e)}")
            print(f"Error sending video: {str(e)}")
    elif call.data.startswith("cat_"):
        category = call.data.replace("cat_", "")
        videos = get_videos(category)
        if not videos:
            bot.answer_callback_query(call.id, f"No videos in category '{category}'.")
            return
        keyboard = InlineKeyboardMarkup()
        for video in videos:
            caption = video[1] or video[3] or "No title"
            keyboard.add(InlineKeyboardButton(text=caption[:50], callback_data=f"video_{video[0]}_{video[2]}"))
        bot.edit_message_text(f"Videos in '{category}':", call.message.chat.id, call.message.message_id, reply_markup=keyboard)

# Monitor new videos
@bot.message_handler(content_types=['video'])
def handle_new_video(message):
    if message.chat.type in ['supergroup', 'channel']:
        if str(message.chat.id) == CHANNEL_ID:
            caption = message.caption or "No caption"
            file_name = message.video.file_name if message.video.file_name else ""
            add_video(message.message_id, caption, message.chat.id, file_name)
            time.sleep(1)
            print(f"New video added with ID: {message.message_id}")


# Initialize database and start
if __name__ == "__main__":
    init_db()
    
    while True:
        if FETCH_REQUESTED:
            print("Fetch requested. Running fetch_old_videos()...")
            fetch_error = None
            try:
                fetch_old_videos()
                print("Fetching complete.")
            except Exception as e:
                print(f"An error occurred during fetch_old_videos: {e}")
                fetch_error = e
            finally:
                if LAST_FETCH_CHAT_ID:
                    completion_message = "Fetching complete! You can now search for videos."
                    if fetch_error:
                        completion_message = f"Fetch process finished, but an error occurred."
                    try:
                        bot.send_message(LAST_FETCH_CHAT_ID, completion_message)
                    except Exception as send_e:
                        print(f"Failed to send fetch completion message: {send_e}")

                FETCH_REQUESTED = False
                LAST_FETCH_CHAT_ID = None

        try:
            print("The bot is running...")
            bot.polling(none_stop=False)
        except Exception as e:
            if isinstance(e, telebot.apihelper.ApiTelegramException) and e.error_code == 409:
                print("\n" + "!"*60)
                print("!!! CRITICAL ERROR: 409 Conflict.")
                print("!!! This means another instance of the bot is running with the same token.")
                print("!!! Please ensure you have stopped ALL other running copies of this bot.")
                print("!"*60 + "\n")
            else:
                print(f"Bot polling error: {e}")
            
            print("Restarting in 15 seconds...")
            time.sleep(15)
