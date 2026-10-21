import os
import asyncio  # Key: need to manually handle event loop
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

WELCOME_TEXT = """Welcome to Lemvest Trading Lab!  

Here, I share trading insights, market analysis, educational content, and real trading experience to help you better understand the market.

We're just getting started, so you're early!

Join for free and grow with us."""

CHANNEL_LINK = "https://t.me/+CaYjuNbPEFA2NWU0"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [[InlineKeyboardButton("Join Our Channel", url=CHANNEL_LINK)]]
    reply_markup = InlineKeyboardMarkup(keyboard)
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

    # ====== Key fix: manually create and set event loop (compatible with Python 3.14) ======
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        # If no event loop exists in the current thread, create one and set it
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
    # =================================================================

    try:
        application = Application.builder().token(token).build()
        application.add_handler(CommandHandler("start", start))
        
        logger.info("Bot is starting with long polling...")
        application.run_polling()
    except Exception as e:
        logger.error(f"Bot crashed: {e}", exc_info=True)

if __name__ == "__main__":
    main()
