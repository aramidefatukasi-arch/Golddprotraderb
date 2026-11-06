import os
import io
import sqlite3
import logging
from pathlib import Path

import qrcode
import barcode
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageFont

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIGURATION
# =========================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_USER_ID", "0"))

CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/yourchannel")
PROMO_TEXT = os.getenv(
    "PROMO_TEXT",
    "Welcome! Join our channel to receive updates and information."
)

WELCOME_IMAGE = os.getenv("WELCOME_IMAGE", "welcome.jpg")
DB_PATH = os.getenv("DB_PATH", "users.db")

MODE_REDIRECT = "REDIRECT"
MODE_REVERSE = "REVERSE"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# =========================================================
# DATABASE
# =========================================================

def db_connection():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)

    return sqlite3.connect(DB_PATH)


def initialize_database():
    with db_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                setting TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        conn.execute("""
            INSERT OR IGNORE INTO settings (setting, value)
            VALUES ('bot_mode', 'REDIRECT')
        """)


def save_user(user_id):
    with db_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
            (user_id,),
        )


def get_user_count():
    with db_connection() as conn:
        result = conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()
        return result[0]


def get_mode():
    with db_connection() as conn:
        result = conn.execute(
            "SELECT value FROM settings WHERE setting = 'bot_mode'"
        ).fetchone()

    return result[0] if result else MODE_REDIRECT


def set_mode(mode):
    with db_connection() as conn:
        conn.execute("""
            INSERT INTO settings (setting, value)
            VALUES ('bot_mode', ?)
            ON CONFLICT(setting)
            DO UPDATE SET value = excluded.value
        """, (mode,))


def is_admin(user_id):
    return user_id == ADMIN_ID and ADMIN_ID != 0


# =========================================================
# IMAGE GENERATORS
# =========================================================

def load_font(size):
    possible_fonts = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]

    for font_path in possible_fonts:
        if os.path.exists(font_path):
            return ImageFont.truetype(font_path, size)

    return ImageFont.load_default()


def create_logo(text, style):
    styles = {
        "blue": ((20, 55, 110), (40, 150, 220)),
        "dark": ((15, 15, 20), (65, 65, 75)),
        "gold": ((75, 50, 10), (205, 155, 55)),
        "purple": ((55, 25, 95), (150, 85, 200)),
        "green": ((10, 65, 45), (55, 160, 105)),
    }

    start, end = styles.get(style, styles["blue"])

    image = Image.new("RGB", (1000, 600))
    draw = ImageDraw.Draw(image)

    for y in range(600):
        ratio = y / 600
        color = tuple(
            int(start[i] * (1 - ratio) + end[i] * ratio)
            for i in range(3)
        )
        draw.line((0, y, 1000, y), fill=color)

    font = load_font(65)
    text = text[:22]

    box = draw.textbbox((0, 0), text, font=font)
    text_width = box[2] - box[0]
    text_height = box[3] - box[1]

    draw.text(
        ((1000 - text_width) / 2, (600 - text_height) / 2),
        text,
        fill="white",
        font=font,
    )

    output = io.BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output


def create_qr(data):
    qr = qrcode.QRCode(
        version=None,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)

    image = qr.make_image(
        fill_color="black",
        back_color="white",
    ).convert("RGB")

    output = io.BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output


def create_barcode(data):
    code_class = barcode.get_barcode_class("code128")
    code = code_class(data, writer=ImageWriter())

    output = io.BytesIO()
    code.write(output)
    output.seek(0)
    return output


# =========================================================
# KEYBOARDS
# =========================================================

def admin_mode_buttons():
    return [
        [
            InlineKeyboardButton(
                "REDIRECT",
                callback_data="mode:REDIRECT",
            ),
            InlineKeyboardButton(
                "REVERSE",
                callback_data="mode:REVERSE",
            ),
        ]
    ]


def redirect_keyboard(user_id):
    keyboard = [
        [
            InlineKeyboardButton(
                "Join Channel",
                url=CHANNEL_LINK,
            )
        ]
    ]

    if is_admin(user_id):
        keyboard.extend(admin_mode_buttons())

    return InlineKeyboardMarkup(keyboard)


def software_keyboard(user_id):
    keyboard = [
        [
            InlineKeyboardButton(
                "Logo Maker",
                callback_data="tool:logo",
            )
        ],
        [
            InlineKeyboardButton(
                "QR Code Generator",
                callback_data="tool:qr",
            )
        ],
        [
            InlineKeyboardButton(
                "Barcode Generator",
                callback_data="tool:barcode",
            )
        ],
    ]

    if is_admin(user_id):
        keyboard.extend(admin_mode_buttons())

    return InlineKeyboardMarkup(keyboard)


def logo_styles_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Blue", callback_data="style:blue"),
            InlineKeyboardButton("Dark", callback_data="style:dark"),
        ],
        [
            InlineKeyboardButton("Gold", callback_data="style:gold"),
            InlineKeyboardButton("Purple", callback_data="style:purple"),
            InlineKeyboardButton("Green", callback_data="style:green"),
        ],
    ])


# =========================================================
# WELCOME SCREENS
# =========================================================

async def show_redirect_screen(message, user_id):
    keyboard = redirect_keyboard(user_id)

    if os.path.isfile(WELCOME_IMAGE):
        with open(WELCOME_IMAGE, "rb") as photo:
            await message.reply_photo(
                photo=photo,
                caption=PROMO_TEXT,
                reply_markup=keyboard,
            )
    else:
        await message.reply_text(
            PROMO_TEXT,
            reply_markup=keyboard,
        )


async def show_software_screen(message, user_id):
    await message.reply_text(
        "Welcome to the Software Tools Bot.\n\n"
        "Choose the tool you want to use:",
        reply_markup=software_keyboard(user_id),
    )


async def show_active_screen(message, user_id):
    if get_mode() == MODE_REDIRECT:
        await show_redirect_screen(message, user_id)
    else:
        await show_software_screen(message, user_id)


# =========================================================
# COMMANDS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    save_user(user.id)

    context.user_data.clear()

    await show_active_screen(
        update.effective_message,
        user.id,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if get_mode() == MODE_REDIRECT:
        await show_redirect_screen(
            update.effective_message,
            user_id,
        )
        return

    await update.effective_message.reply_text(
        "Choose a tool from the menu to get started.\n\n"
        "Use /start to return to the main menu.\n"
        "Use /cancel to cancel your current task."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    await update.effective_message.reply_text(
        "Your current task has been cancelled."
    )

    await show_active_screen(
        update.effective_message,
        update.effective_user.id,
    )


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    await update.effective_message.reply_text(
        f"Total registered users: {get_user_count()}\n"
        f"Active mode: {get_mode()}"
    )


# =========================================================
# MODE SWITCHING
# =========================================================

async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    if not is_admin(user_id):
        await query.answer(
            "You are not authorized to change the bot mode.",
            show_alert=True,
        )
        return

    selected_mode = query.data.split(":", 1)[1]

    if selected_mode not in (MODE_REDIRECT, MODE_REVERSE):
        await query.answer("Invalid mode.")
        return

    set_mode(selected_mode)
    context.user_data.clear()

    await query.answer(f"{selected_mode} mode activated.")

    # Refresh the admin's screen immediately.
    await query.message.delete()

    await show_active_screen(
        query.message,
        user_id,
    )


# =========================================================
# SOFTWARE TOOL BUTTONS
# =========================================================

async def tool_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    await query.answer()

    # Check the current global mode every time a button is pressed.
    if get_mode() != MODE_REVERSE:
        await query.message.reply_text(
            "The software tools are currently unavailable."
        )
        await show_active_screen(query.message, user_id)
        return

    tool = query.data.split(":", 1)[1]

    if tool == "logo":
        context.user_data["waiting_for"] = "logo"
        await query.message.reply_text(
            "Send the text you want to use for your logo."
        )

    elif tool == "qr":
        context.user_data["waiting_for"] = "qr"
        await query.message.reply_text(
            "Send the text or URL you want to convert into a QR code."
        )

    elif tool == "barcode":
        context.user_data["waiting_for"] = "barcode"
        await query.message.reply_text(
            "Send the numbers or text you want to convert into a barcode."
        )


async def style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if get_mode() != MODE_REVERSE:
        await query.message.reply_text(
            "The software tools are currently unavailable."
        )
        return

    if context.user_data.get("waiting_for") != "logo_style":
        return

    style = query.data.split(":", 1)[1]
    logo_text = context.user_data.get("logo_text", "My Logo")

    image = create_logo(logo_text, style)

    context.user_data.clear()

    await query.message.reply_photo(
        photo=image,
        caption="Your logo is ready.",
    )


# =========================================================
# TEXT INPUT
# =========================================================

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.effective_message.text.strip()

    # In REDIRECT mode, users only receive the promotional function.
    if get_mode() == MODE_REDIRECT:
        context.user_data.clear()

        await show_redirect_screen(
            update.effective_message,
            user_id,
        )
        return

    waiting_for = context.user_data.get("waiting_for")

    if not waiting_for:
        await update.effective_message.reply_text(
            "Please choose a tool from the menu.",
            reply_markup=software_keyboard(user_id),
        )
        return

    if waiting_for == "logo":
        context.user_data["logo_text"] = text[:22]
        context.user_data["waiting_for"] = "logo_style"

        await update.effective_message.reply_text(
            "Choose a logo style:",
            reply_markup=logo_styles_keyboard(),
        )

    elif waiting_for == "qr":
        try:
            image = create_qr(text)
            context.user_data.clear()

            await update.effective_message.reply_photo(
                photo=image,
                caption="Your QR code is ready.",
            )
        except Exception:
            logger.exception("QR generation failed")
            await update.effective_message.reply_text(
                "I could not generate that QR code. Please try again."
            )

    elif waiting_for == "barcode":
        try:
            image = create_barcode(text)
            context.user_data.clear()

            await update.effective_message.reply_photo(
                photo=image,
                caption="Your barcode is ready.",
            )
        except Exception:
            logger.exception("Barcode generation failed")
            await update.effective_message.reply_text(
                "I could not generate that barcode. Please try another value."
            )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):
    logger.exception(
        "An error occurred while processing an update:",
        exc_info=context.error,
    )


# =========================================================
# START BOT
# =========================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is missing.")

    if ADMIN_ID == 0:
        raise RuntimeError("ADMIN_USER_ID is missing or invalid.")

    initialize_database()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("users", users_command))

    application.add_handler(
        CallbackQueryHandler(mode_callback, pattern=r"^mode:")
    )
    application.add_handler(
        CallbackQueryHandler(tool_callback, pattern=r"^tool:")
    )
    application.add_handler(
        CallbackQueryHandler(style_callback, pattern=r"^style:")
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Bot is starting. Current mode: %s", get_mode())

    application.run_polling()


if __name__ == "__main__":
    main()
