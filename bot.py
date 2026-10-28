import os
import asyncio
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from contextlib import closing

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, RetryAfter
from telegram.ext import Application, CommandHandler, ContextTypes

# ==========================================
# 1. SETUP AND CONFIGURATION
# ==========================================
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "users.db"

CHANNEL_LINK = "https://t.me/+CaYjuNbPEFA2NWU0"

# ✅ DIRECT GITHUB IMAGE LINK (No Imgur blocks)
IMAGE_URL = "https://raw.githubusercontent.com/aramidefatukasi-arch/Golddprotraderb/main/welcome.jpg" 

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

👇 Tap the button below to discover what's inside!

Send /stop to stop reminders."""

REMINDER_INTERVAL = 6 * 60 * 60  # 6 hours
CAMPAIGN_DURATION = 15 * 24 * 60 * 60  # 15 days
CHECK_INTERVAL = 60

# ==========================================
# 2. DATABASE
# ==========================================
def connect_db():
    return sqlite3.connect(DB_FILE, timeout=10)

def init_db():
    with closing(connect_db()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reminder_schedule (
                user_id INTEGER PRIMARY KEY,
                started_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                next_send_at REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_reminder_due ON reminder_schedule (active, next_send_at)")

def register_user(user_id):
    now = datetime.now().timestamp()
    with closing(connect_db()) as conn, conn:
        conn.execute("""
            INSERT OR IGNORE INTO reminder_schedule (user_id, started_at, expires_at, next_send_at, active)
            VALUES (?, ?, ?, ?, 1)
        """, (user_id, now, now + CAMPAIGN_DURATION, now + REMINDER_INTERVAL))
        conn.execute("""
            UPDATE reminder_schedule SET next_send_at = MAX(next_send_at, ?) WHERE user_id = ?
        """, (now + REMINDER_INTERVAL, user_id))

def get_due_users():
    now = datetime.now().timestamp()
    with closing(connect_db()) as conn:
        rows = conn.execute("""
            SELECT user_id FROM reminder_schedule
            WHERE active = 1 AND next_send_at <= ? AND expires_at > ?
            ORDER BY next_send_at LIMIT 100
        """, (now, now)).fetchall()
    return [row[0] for row in rows]

def claim_reminder(user_id):
    now = datetime.now().timestamp()
    with closing(connect_db()) as conn, conn:
        result = conn.execute("""
            UPDATE reminder_schedule SET next_send_at = ?
            WHERE user_id = ? AND active = 1 AND next_send_at <= ? AND expires_at > ?
        """, (now + REMINDER_INTERVAL, user_id, now, now))
        return result.rowcount == 1

def record_delivery(user_id):
    with closing(connect_db()) as conn, conn:
        conn.execute("""
            UPDATE reminder_schedule SET next_send_at = MAX(next_send_at, ?) WHERE user_id = ?
        """, (datetime.now().timestamp() + REMINDER_INTERVAL, user_id))

def disable_reminders(user_id):
    now = datetime.now().timestamp()
    with closing(connect_db()) as conn, conn:
        conn.execute("""
            INSERT OR IGNORE INTO reminder_schedule (user_id, started_at, expires_at, next_send_at, active)
            VALUES (?, ?, ?, ?, 0)
        """, (user_id, now, now + CAMPAIGN_DURATION, now + REMINDER_INTERVAL))
        conn.execute("UPDATE reminder_schedule SET active = 0 WHERE user_id = ?", (user_id,))

# ==========================================
# 3. WELCOME MESSAGE & COMMANDS
# ==========================================
async def send_welcome(message):
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Join Our Channel", url=CHANNEL_LINK)]])
    try:
        await message.reply_photo(photo=IMAGE_URL, caption=WELCOME_TEXT, reply_markup=keyboard)
    except Exception:
        logger.exception("Failed to send photo, falling back to text.")
        await message.reply_text(text=WELCOME_TEXT, reply_markup=keyboard, disable_web_page_preview=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None: return

    await send_welcome(message)

    if update.effective_chat.type == "private":
        try:
            await asyncio.to_thread(register_user, user.id)
            logger.info(f"/start handled for user {user.id}")
        except Exception:
            logger.exception("Reminder registration failed")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_message: return
    try:
        await asyncio.to_thread(disable_reminders, update.effective_user.id)
        await update.effective_message.reply_text("✅ Your reminders have been stopped.")
    except Exception:
        await update.effective_message.reply_text("I couldn't save your request.")

# ==========================================
# 4. BACKGROUND REMINDER LOOP
# ==========================================
async def reminder_loop(application):
    await asyncio.sleep(10)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Discover Lemvest", url=CHANNEL_LINK)]])
    
    while True:
        try:
            users = await asyncio.to_thread(get_due_users)
            for user_id in users:
                claimed = await asyncio.to_thread(claim_reminder, user_id)
                if not claimed: continue

                try:
                    await application.bot.send_photo(
                        chat_id=user_id, photo=IMAGE_URL, caption=PROMO_TEXT, reply_markup=keyboard
                    )
                    await asyncio.to_thread(record_delivery, user_id)
                    logger.info(f"Reminder sent to user {user_id}")
                except Forbidden:
                    await asyncio.to_thread(disable_reminders, user_id)
                except RetryAfter as exc:
                    await asyncio.sleep(exc.retry_after + 1)
                    break
                except Exception:
                    logger.exception(f"Reminder failed for {user_id}")
                
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reminder cycle failed")
        
        await asyncio.sleep(CHECK_INTERVAL)

async def post_init(application):
    await asyncio.to_thread(init_db)
    application.bot_data["reminder_task"] = asyncio.create_task(reminder_loop(application))
    logger.info("Background reminder task started")

# ==========================================
# 5. MAIN (CRASH-PROOF FOR PYTHON 3.14)
# ==========================================
def main() -> None:
    print(">>> SCRIPT IS STARTING...", flush=True)
    
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print(">>> ERROR: TELEGRAM_BOT_TOKEN is missing!", flush=True)
        return

    # FIX FOR PYTHON 3.14 EVENT LOOP CRASH
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)

    try:
        print(">>> BUILDING APPLICATION...", flush=True)
        application = (
            Application.builder()
            .token(token)
            .post_init(post_init)
            .build()
        )
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("stop", stop))

        print(">>> BOT IS STARTING WITH LONG POLLING...", flush=True)
        application.run_polling()
        
    except Exception as e:
        print(f">>> FATAL ERROR: {e}", flush=True)

if __name__ == "__main__":
    main()
