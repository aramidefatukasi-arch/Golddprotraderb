import os
import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# ==========================================
# 1. SETUP AND CONFIGURATION
# ==========================================
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

CHANNEL_LINK = "https://t.me/+CaYjuNbPEFA2NWU0"

WELCOME_TEXT = """Welcome to Lemvest Trading Lab!  

Here, I share trading insights, market analysis, educational content, and real trading experience to help you better understand the market.

We're just getting started, so you're early!

Join for free and grow with us."""

PROMO_TEXT = """📊 Welcome to Lemvest Trading Lab!

9 years of trading experience. 2,800+ community members. Real market insights, every day.

🎯 Daily Market Briefings
📈 Live Trading Sessions
📚 Trading Education & Practical Tools

Ready to take your trading knowledge further?

👇 Tap the button below to discover what's inside!"""

# ==========================================
# 2. DATABASE (To track 6-hour limits)
# ==========================================
DB_FILE = "users.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, last_message_time TEXT)''')
    conn.commit()
    conn.close()

def update_user_time(user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    now = datetime.now().isoformat()
    c.execute("INSERT OR REPLACE INTO users (user_id, last_message_time) VALUES (?, ?)", (user_id, now))
    conn.commit()
    conn.close()

def get_users_for_broadcast():
    """Finds users who haven't received a message in the last 6 hours."""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    six_hours_ago = (datetime.now() - timedelta(hours=6)).isoformat()
    
    # Select users whose last message was more than 6 hours ago
    c.execute("SELECT user_id FROM users WHERE last_message_time < ?", (six_hours_ago,))
    users = [row[0] for row in c.fetchall()]
    conn.close()
    return users

# ==========================================
# 3. BOT COMMANDS
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    
    # Anti-spam: Record the time they clicked start
    update_user_time(user_id)

    keyboard = [[InlineKeyboardButton("Join Our Channel", url=CHANNEL_LINK)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        text=WELCOME_TEXT,
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )

# ==========================================
# 4. THE 6-HOUR BACKGROUND TASK
# ==========================================
async def broadcast_task(context: ContextTypes.DEFAULT_TYPE):
    """Runs every minute to check if anyone needs the 6-hour message."""
    users_to_message = get_users_for_broadcast()
    
    if not users_to_message:
        return

    keyboard = [[InlineKeyboardButton("Discover Lemvest", url=CHANNEL_LINK)]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    for user_id in users_to_message:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=PROMO_TEXT,
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )
            # Update their time so they don't get spammed again for another 6 hours
            update_user_time(user_id)
            logger.info(f"Sent 6-hour promo to user {user_id}")
            
            # Sleep for 1 second between messages to avoid hitting Telegram's rate limits
            await asyncio.sleep(1) 
        except Exception as e:
            logger.error(f"Failed to send promo to {user_id}: {e}")

# ==========================================
# 5. MAIN EXECUTION
# ==========================================
def main() -> None:
    # Initialize the database
    init_db()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("CRITICAL: TELEGRAM_BOT_TOKEN is not set in Render Environment Variables!")
        return

    # FIX FOR PYTHON 3.14 CRASH
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)

    # Build the application
    application = Application.builder().token(token).build()
    
    # Add /start command
    application.add_handler(CommandHandler("start", start))

    # Start the background task (checks every 60 seconds)
    if application.job_queue:
        application.job_queue.run_repeating(broadcast_task, interval=60, first=10)
    else:
        logger.warning("JobQueue not available. Check requirements.txt!")

    logger.info("Bot is starting with long polling...")
    application.run_polling()

if __name__ == "__main__":
    main()
