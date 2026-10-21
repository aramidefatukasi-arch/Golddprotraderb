import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

WELCOME_TEXT = """Welcome to Lemvest Trading Lab!  

Here, I share trading insights, market analysis, educational content, and real trading experience to help you better understand the market.

We're just getting started, so you're early!

Join for free and grow with us."""

CHANNEL_LINK = "https://t.me/+CaYjuNbPEFA2NWU0"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send welcome message and channel button when /start is issued."""
    keyboard = [
        [InlineKeyboardButton("Join Our Channel", url=CHANNEL_LINK)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        text=WELCOME_TEXT,
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )

def main() -> None:
    # Fetch token from Render environment variables
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    
    # Add explicit error logging to help debug
    if not token:
        logger.error("CRITICAL ERROR: TELEGRAM_BOT_TOKEN environment variable is missing!")
        logger.error("Please go to Render Dashboard -> Environment -> Add Environment Variable")
        return

    try:
        # Build application
        application = Application.builder().token(token).build()
        application.add_handler(CommandHandler("start", start))
        
        logger.info("Bot is starting with long polling...")
        # run_polling() handles the event loop itself; do not wrap it in async
        application.run_polling()
    except Exception as e:
        logger.error(f"Bot failed to start: {e}", exc_info=True)

if __name__ == "__main__":
    main()
