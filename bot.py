import os
import asyncio
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Your new welcome message
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
    """Send welcome image, text, and inline buttons when /start is issued."""
    
    # Create the 3 inline buttons (they stack vertically, which looks great on mobile)
    keyboard = [
        [InlineKeyboardButton("👉 JOIN CHANNEL 👈", url=CHANNEL_LINK)],
        [InlineKeyboardButton("🚀 JOIN CHANNEL 🚀", url=CHANNEL_LINK)],
        [InlineKeyboardButton("👉 JOIN CHANNEL 👈", url=CHANNEL_LINK)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Path to your image in the GitHub repo
    image_path = "welcome.jpg" 

    # Send the image with the caption and buttons
    try:
        # Open the image file in binary mode
        with open(image_path, "rb") as photo:
            await update.message.reply_photo(
                photo=photo,
                caption=WELCOME_TEXT,
                reply_markup=reply_markup
            )
    except FileNotFoundError:
        # Fallback if the image is missing from the repo
        logger.error("welcome.jpg not found! Sending text only.")
        await update.message.reply_text(
            text=WELCOME_TEXT,
            reply_markup=reply_markup,
            disable_web_page_preview=True
        )

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    
    if not token:
        logger.error("CRITICAL: TELEGRAM_BOT_TOKEN is not set!")
        return

    # Fix for Python 3.14 event loop issue
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)

    try:
        application = Application.builder().token(token).build()
        application.add_handler(CommandHandler("start", start))
        
        logger.info("Bot is starting with long polling...")
        application.run_polling()
    except Exception as e:
        logger.error(f"Bot crashed: {e}", exc_info=True)

if __name__ == "__main__":
    main()
