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

# The 8 follow-up messages
FOLLOW_UP_MESSAGES = [
    """1 📊 Welcome to Lemvest Trading Lab!

9 years of trading experience. 2,800+ community members. Real market insights, every day.

🎯 Daily Market Briefings
📈 Live Trading Sessions
📚 Trading Education & Practical Tools

Ready to take your trading knowledge further?

👇 Tap START to discover what's inside!

        😬 DISCOVER LEMVEST""",
    """2 📊 The markets move every day. Are you keeping up?

Bitcoin, Gold, and major indices. Every trading day brings new opportunities and risks.

Our Daily Market Briefings break down market movements, key price levels, and potential scenarios.

Less noise. More clarity.

👇 Get your daily market insights for free!""",
    """3 🔥 9 YEARS IN THE MARKETS. NOW SHARING THE EXPERIENCE.

After 9 years of trading, one thing is clear: experience makes a difference.

At Lemvest, we share our market approach, analysis, and trading process with a growing community of 2,800+ members.

No unnecessary hype. Just real market insights.

👇 Come see how we approach the markets!""",
    """4 🎯 EVER WONDERED WHAT GOES INTO A TRADING SETUP?

It's not just about clicking BUY or SELL.

Every setup involves:

📊 Market Structure & Key Levels
🎯 Entry & Exit Planning
🛑 Risk Management

We share real trading setups and explain the reasoning behind them.

👇 See how we analyze trading opportunities!""",
    """5 🔴 WHAT IF YOU COULD WATCH TRADING DECISIONS HAPPEN LIVE?

Charts. Market movements. Real-time analysis.

Our live trading sessions let you follow experienced traders as they analyze opportunities and manage risk in real market conditions.

No hindsight. Just the markets unfolding in real time.

👇 Don't miss our next live session!""",
    """6 🔖 TRADING IS A SKILL. ARE YOU BUILDING YOURS?

Whether you're a complete beginner or already trading, there's always something new to learn.

Inside Lemvest, you'll discover:

🎓 Trading Strategies & Market Concepts
🧠 Trading Psychology
🛑 Risk Management Principles

Learn at your own pace and build a more structured approach.

👇 Start learning with Lemvest!""",
    """7 🧮 DO YOU KNOW YOUR RISK BEFORE ENTERING A TRADE?

A good trading plan starts with the numbers.

Position sizing. Risk-to-reward ratios. Potential outcomes.

Our practical trading tools help you understand the calculations behind better-informed trading decisions.

Because managing risk matters just as much as finding opportunities.

👇 Discover the Lemvest Trader Toolkit!""",
    """8 👀 STILL WATCHING FROM THE SIDELINES?

2,800+ members have already joined Lemvest Trading Lab.

Here's what you're missing:

📊 Daily Market Briefings
🔴 Live Trading Sessions
🎯 Real Market Analysis & Trading Setups
📚 Trading Education & Practical Tools

9 years of trading experience, shared with our community.

The channel is free to join. Your next step is one click away.

👇 JOIN LEMVEST TRADING LAB TODAY!"""
]

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
                
                # ⚠️ THIS IS NOW SET TO 6 HOURS (21,600 seconds)
                if current_time - data["joined_at"] >= 21600:
                    
                    if data["message_index"] < len(FOLLOW_UP_MESSAGES):
                        msg_text = FOLLOW_UP_MESSAGES[data["message_index"]]
                        
                        # Add the button to the message
                        keyboard = [[InlineKeyboardButton("👉 JOIN CHANNEL 👈", url=CHANNEL_LINK)]]
                        reply_markup = InlineKeyboardMarkup(keyboard)

                        try:
                            # Send the IMAGE with the caption
                            logger.info(f"Attempting to send message {data['message_index'] + 1} to user {user_id}")
                            with open(IMAGE_PATH, "rb") as photo:
                                await app.bot.send_photo(
                                    chat_id=int(user_id), 
                                    photo=photo,
                                    caption=msg_text, 
                                    reply_markup=reply_markup
                                )
                            logger.info(f"Successfully sent message to {user_id}")
                            
                            # Advance to the next message and reset the 6h timer
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

    if user_id not in users:
        users[user_id] = {
            "joined_at": time.time(),
            "message_index": 0 
        }
        save_users(users)
        logger.info(f"New user added: {user_id}")

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
    
    logger.info("Bot is fully live. Next messages will send after 6 hours...")
    application.run_polling()

if __name__ == "__main__":
    main()
