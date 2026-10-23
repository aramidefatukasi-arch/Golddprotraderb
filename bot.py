import os
import asyncio
import logging
import json
import time
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 1. Web Server for Render/UptimeRobot ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# --- 2. Your Messages & Data ---
CHANNEL_LINK = "https://t.me/+CaYjuNbPEFA2NWU0"
IMAGE_PATH = "welcome.jpg"

WELCOME_TEXT = """🔥 Welcome to Lemvest Trading Lab!

You're one step away from joining a community of 2,800+ members!

Here's what you'll find inside:

📊 Daily Market Briefings
📈 Live Trading Sessions
🎯 Real Trading Setups & Market Analysis
📚 Educational Resources & Trading Tools

Everything built around 9 years of real trading experience.

👇 Join our Telegram channel and see for yourself!"""

# The bot will repeat this specific message every 6 hours
MESSAGE_1 = """1 📊 Welcome to Lemvest Trading Lab!

9 years of trading experience. 2,800+ community members. Real market insights, every day.

🎯 Daily Market Briefings
📈 Live Trading Sessions
📚 Trading Education & Practical Tools

Ready to take your trading knowledge further?

👇 Tap START to discover what's inside!

        😬 DISCOVER LEMVEST"""

DB_FILE = "users.json"

def load_users():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {}

def save_users(users):
    with open(DB_FILE, "w") as f:
        json.dump(users, f)

# --- 3. Background Task to Send Messages WITH IMAGE ---
async def send_follow_ups(app):
    """Runs in the background, checking every minute if anyone needs a message."""
    while True:
        try:
            users = load_users()
            current_time = time.time()
            updated = False

            for user_id, data in users.items():
                
                # Check if 6 hours (21,600 seconds) have passed
                if current_time - data["joined_at"] >= 21600:
                    
                    # Check if we have sent less than 60 messages (60 messages = 15 days)
                    if data["message_index"] < 60:
                        
                        keyboard = [[InlineKeyboardButton("👉 JOIN CHANNEL 👈", url=CHANNEL_LINK)]]
                        reply_markup = InlineKeyboardMarkup(keyboard)

                        try:
                            logger.info(f"Sending 6-hour reminder #{data['message_index'] + 1} to {user_id}")
                            with open(IMAGE_PATH, "rb") as photo:
                                await app.bot.send_photo(
                                    chat_id=int(user_id), 
                                    photo=photo,
                                    caption=MESSAGE_1, # ALWAYS SEND MESSAGE 1
                                    reply_markup=reply_markup
                                )
                            
                            # Increase the counter and reset the 6h timer
                            users[user_id]["message_index"] += 1
                            users[user_id]["joined_at"] = current_time
                            updated = True
                            
                        except Exception as e:
                            logger.error(f"Failed to send to {user_id}: {e}")
                            if "blocked" in str(e).lower() or "chat not found" in str(e).lower():
                                del users[user_id]
                                updated = True

            if updated:
                save_users(users)

        except Exception as e:
            logger.error(f"Error in background task: {e}")

        # Checks the time every 60 seconds
        await asyncio.sleep(60) 

# --- 4. Bot Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = str(update.effective_user.id)
    users = load_users()

    # If user is new, add them. If they exist, RESET their timer to 6 hours from now.
    users[user_id] = {
        "joined_at": time.time(), # Time right now
        "message_index": 0        # Start the 15-day countdown from 0
    }
    save_users(users)
    logger.info(f"User {user_id} started the bot. 15-day timer started.")

    keyboard = [
        [InlineKeyboardButton("👉 JOIN CHANNEL 👈", url=CHANNEL_LINK)],
        [InlineKeyboardButton("🚀 JOIN CHANNEL 🚀", url=CHANNEL_LINK)],
        [InlineKeyboardButton("👉 JOIN CHANNEL 👈", url=CHANNEL_LINK)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        with open(IMAGE_PATH, "rb") as photo:
            await update.message.reply_photo(
                photo=photo,
                caption=WELCOME_TEXT,
                reply_markup=reply_markup
            )
    except Exception as e:
        logger.error(f"Image error: {e}")
        await update.message.reply_text(text=WELCOME_TEXT, reply_markup=reply_markup)

async def post_init(app):
    app.create_task(send_follow_ups(app))

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("CRITICAL: TELEGRAM_BOT_TOKEN is not set!")
        return

    Thread(target=run_health_server, daemon=True).start()

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)

    application = Application.builder().token(token).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start))
    
    logger.info("Bot is live. Reminder 1 will be sent every 6 hours for 15 days.")
    application.run_polling()

if __name__ == "__main__":
    main()
