import os
import asyncio
import logging
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 1. This tiny web server keeps UptimeRobot happy ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# --- 2. Your Telegram Bot Code ---
WELCOME_TEXT = """🔥 Welcome to Lemvest Trading Lab!

You're one step away from joining a community of 2,800+ members!

Here's what you'll find inside:

📊 Daily Market Briefings
📈 Live Trading Sessions
🎯 Real Trading Setups & Market Analysis
📚 Educational Resources & Trading Tools

Everything built around 9 years of real trading experience.

👇 Join our Telegram channel and see for yourself!"""

CHANNEL_LINK = "https://t.me/+CaYjuNbPEFA2NWU0"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("👉 JOIN CHANNEL 👈", url=CHANNEL_LINK)],
        [InlineKeyboardButton("🚀 JOIN CHANNEL 🚀", url=CHANNEL_LINK)],
        [InlineKeyboardButton("👉 JOIN CHANNEL 👈", url=CHANNEL_LINK)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    image_path = "welcome.jpg" 

    with open(image_path, "rb") as photo:
        await update.message.reply_photo(
            photo=photo,
            caption=WELCOME_TEXT,
            reply_markup=reply_markup
        )

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("CRITICAL: TELEGRAM_BOT_TOKEN is not set!")
        return

    # Start the fake web server in a background thread so UptimeRobot can ping it
    Thread(target=run_health_server, daemon=True).start()

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)

    try:
        application = Application.builder().token(token).build()
        application.add_handler(CommandHandler("start", start))
        logger.info("Bot is starting...")
        application.run_polling()
    except Exception as e:
        logger.error(f"Bot crashed: {e}", exc_info=True)

if __name__ == "__main__":
    main()
