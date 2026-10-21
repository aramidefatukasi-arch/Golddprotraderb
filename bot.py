import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Your specific welcome message
WELCOME_TEXT = """Welcome to Lemvest Trading Lab!  

Here, I share trading insights, market analysis, educational content, and real trading experience to help you better understand the market.

We're just getting started, so you're early!

Join for free and grow with us."""

# Your channel link
CHANNEL_LINK = "https://t.me/+CaYjuNbPEFA2NWU0"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends the welcome message with an inline button when /start is issued."""
    # Create the inline keyboard button
    keyboard = [
        [InlineKeyboardButton("Join Our Channel", url=CHANNEL_LINK)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Send the welcome message with the button
    await update.message.reply_text(
        text=WELCOME_TEXT,
        reply_markup=reply_markup,
        disable_web_page_preview=True
    )

def main() -> None:
    """Starts the bot."""
    # Get the token from the environment variable (Railway will inject this)
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN environment variable not set.")
        return

    # Create the Application and pass it your bot's token.
    application = Application.builder().token(token).build()

    # Register the /start command handler
    application.add_handler(CommandHandler("start", start))

    # Run the bot until the user presses Ctrl-C
    logger.info("Bot is starting with long polling...")
    application.run_polling()

if __name__ == "__main__":
    main()
