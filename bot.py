
import os
import io
import re
import sqlite3
import logging
from datetime import datetime, timezone

import qrcode
import barcode
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageFont

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)


# ==========================================
# CONFIGURATION
# ==========================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

CHANNEL_LINK = os.getenv(
    "CHANNEL_LINK",
    "https://t.me/yourchannel"
).strip()

PROMO_TEXT = os.getenv(
    "PROMO_TEXT",
    "Welcome! Join our channel to receive updates and announcements."
)

DB_PATH = os.getenv("DB_PATH", "users.db")

# LOGO = Normal utility mode
# REDIRECT = Promotional mode

GLOBAL_BOT_MODE = "LOGO"


# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ==========================================
# DATABASE
# ==========================================

def init_db():

    with sqlite3.connect(DB_PATH) as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen TEXT NOT NULL
            )
        """)

        conn.commit()


def save_user(user_id):

    with sqlite3.connect(DB_PATH) as conn:

        conn.execute(
            "INSERT OR IGNORE INTO users VALUES (?, ?)",
            (
                user_id,
                datetime.now(timezone.utc).isoformat()
            )
        )

        conn.commit()


def user_count():

    with sqlite3.connect(DB_PATH) as conn:

        result = conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()

        return result[0]


def is_admin(update):

    return bool(
        update.effective_user
        and ADMIN_USER_ID
        and update.effective_user.id == ADMIN_USER_ID
    )


# ==========================================
# MAIN MENU
# ==========================================

def main_keyboard():

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("Logo Maker"),
                KeyboardButton("QR Code Generator")
            ],
            [
                KeyboardButton("Barcode Generator"),
                KeyboardButton("Help")
            ]
        ],
        resize_keyboard=True
    )


# ==========================================
# LOGO GENERATOR
# ==========================================

def create_logo(text, style):

    palettes = {

        "blue": (
            (12, 32, 72),
            (34, 126, 230),
            (255, 255, 255)
        ),

        "dark": (
            (18, 18, 24),
            (65, 65, 82),
            (255, 255, 255)
        ),

        "gold": (
            (42, 28, 8),
            (176, 128, 36),
            (255, 245, 210)
        ),

        "purple": (
            (42, 18, 72),
            (132, 70, 190),
            (255, 255, 255)
        ),

        "green": (
            (10, 54, 43),
            (28, 145, 105),
            (255, 255, 255)
        )

    }

    top, bottom, foreground = palettes.get(
        style,
        palettes["blue"]
    )

    width = 1000
    height = 600

    image = Image.new("RGB", (width, height))

    pixels = image.load()

    # Background gradient

    for y in range(height):

        ratio = y / (height - 1)

        color = tuple(
            int(
                top[i] * (1 - ratio)
                + bottom[i] * ratio
            )
            for i in range(3)
        )

        for x in range(width):

            pixels[x, y] = color

    draw = ImageDraw.Draw(image)

    # Simple logo symbol

    center_x = width // 2
    center_y = 210

    draw.rounded_rectangle(
        (
            center_x - 90,
            center_y - 90,
            center_x + 90,
            center_y + 90
        ),
        radius=38,
        outline=foreground,
        width=10
    )

    draw.line(
        (
            center_x - 45,
            center_y + 25,
            center_x,
            center_y - 35,
            center_x + 45,
            center_y + 25
        ),
        fill=foreground,
        width=14
    )

    # Load font

    font_path = (
        "/usr/share/fonts/truetype/"
        "dejavu/DejaVuSans-Bold.ttf"
    )

    font_size = 72

    try:

        font = ImageFont.truetype(
            font_path,
            font_size
        )

        while (
            font_size > 28
            and draw.textbbox(
                (0, 0),
                text,
                font=font
            )[2] > 850
        ):

            font_size -= 4

            font = ImageFont.truetype(
                font_path,
                font_size
            )

    except OSError:

        font = ImageFont.load_default()

    bbox = draw.textbbox(
        (0, 0),
        text,
        font=font
    )

    text_width = bbox[2] - bbox[0]

    draw.text(
        (
            (width - text_width) // 2,
            410
        ),
        text,
        font=font,
        fill=foreground
    )

    output = io.BytesIO()

    output.name = "logo.png"

    image.save(output, "PNG")

    output.seek(0)

    return output


# ==========================================
# QR CODE GENERATOR
# ==========================================

def create_qr(data):

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4
    )

    qr.add_data(data)

    qr.make(fit=True)

    image = qr.make_image(
        fill_color="black",
        back_color="white"
    ).convert("RGB")

    output = io.BytesIO()

    output.name = "qr_code.png"

    image.save(output, "PNG")

    output.seek(0)

    return output


# ==========================================
# BARCODE GENERATOR
# ==========================================

def create_barcode(data):

    code = barcode.get(
        "code128",
        data,
        writer=ImageWriter()
    )

    output = io.BytesIO()

    output.name = "barcode.png"

    code.write(
        output,
        options={
            "module_width": 0.35,
            "module_height": 18,
            "quiet_zone": 6,
            "font_size": 12,
            "text_distance": 5,
            "write_text": True
        }
    )

    output.seek(0)

    return output


# ==========================================
# START COMMAND
# ==========================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_user:

        save_user(update.effective_user.id)

    # Redirect mode

    if GLOBAL_BOT_MODE == "REDIRECT":

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Join Channel",
                        url=CHANNEL_LINK
                    )
                ]
            ]
        )

        await update.effective_message.reply_text(
            PROMO_TEXT,
            reply_markup=keyboard
        )

        return

    # Normal utility mode

    context.user_data.clear()

    await update.effective_message.reply_text(
        "Welcome! I can create simple logos, QR codes, "
        "and barcodes.\n\n"
        "Choose a tool below to get started.",
        reply_markup=main_keyboard()
    )


# ==========================================
# HELP COMMAND
# ==========================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if GLOBAL_BOT_MODE == "REDIRECT":

        return

    message = (
        "Available Tools:\n\n"

        "1. Logo Maker\n"
        "Create a simple text-based logo.\n\n"

        "2. QR Code Generator\n"
        "Convert text or website links into QR codes.\n\n"

        "3. Barcode Generator\n"
        "Create Code 128 barcodes.\n\n"

        "Choose a tool from the menu and follow "
        "the instructions."
    )

    if is_admin(update):

        message += (
            "\n\nAdmin Command:\n"
            "/users - Check registered users."
        )

    await update.effective_message.reply_text(
        message,
        reply_markup=main_keyboard()
    )


# ==========================================
# USERS COMMAND
# ==========================================

async def users_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if GLOBAL_BOT_MODE == "REDIRECT":

        return

    if not is_admin(update):

        return

    total = user_count()

    await update.effective_message.reply_text(
        f"Registered users: {total}"
    )


# ==========================================
# CANCEL COMMAND
# ==========================================

async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if GLOBAL_BOT_MODE == "REDIRECT":

        return

    context.user_data.clear()

    await update.effective_message.reply_text(
        "Cancelled. Choose another tool.",
        reply_markup=main_keyboard()
    )


# ==========================================
# TEXT HANDLER
# ==========================================

async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global GLOBAL_BOT_MODE

    message = update.effective_message

    if not message or not message.text:

        return

    text = message.text.strip()

    # ======================================
    # ADMIN SECRET COMMANDS
    # ======================================

    if is_admin(update):

        if text == "REDIRECT":

            GLOBAL_BOT_MODE = "REDIRECT"

            await message.reply_text(
                "Redirect mode enabled."
            )

            return

        if text == "REVERSE":

            GLOBAL_BOT_MODE = "LOGO"

            await message.reply_text(
                "Normal utility mode restored."
            )

            return

    # ======================================
    # REDIRECT MODE
    # ======================================

    if GLOBAL_BOT_MODE == "REDIRECT":

        return

    # ======================================
    # HELP BUTTON
    # ======================================

    if text == "Help":

        await help_command(update, context)

        return

    # ======================================
    # TOOL SELECTION
    # ======================================

    options = {

        "Logo Maker": "logo",

        "QR Code Generator": "qr",

        "Barcode Generator": "barcode"

    }

    if text in options:

        tool = options[text]

        context.user_data["tool"] = tool

        context.user_data["step"] = "input"

        prompts = {

            "logo": (
                "Send the name or text for your logo. "
                "Maximum 80 characters."
            ),

            "qr": (
                "Send the text or website URL "
                "for your QR code."
            ),

            "barcode": (
                "Send the letters or numbers "
                "for your Code 128 barcode."
            )

        }

        await message.reply_text(
            prompts[tool]
        )

        return

    # ======================================
    # CHECK USER INPUT
    # ======================================

    if context.user_data.get("step") != "input":

        await message.reply_text(
            "Please choose Logo Maker, QR Code Generator, "
            "or Barcode Generator.",
            reply_markup=main_keyboard()
        )

        return

    tool = context.user_data.get("tool")

    if not text:

        await message.reply_text(
            "Please send some text."
        )

        return

    try:

        # ==================================
        # LOGO GENERATION
        # ==================================

        if tool == "logo":

            if len(text) > 80:

                await message.reply_text(
                    "Please keep logo text to 80 characters or fewer."
                )

                return

            context.user_data["pending_text"] = text

            context.user_data["step"] = "style"

            buttons = [

                [
                    InlineKeyboardButton(
                        "Blue",
                        callback_data="style:blue"
                    ),

                    InlineKeyboardButton(
                        "Dark",
                        callback_data="style:dark"
                    )
                ],

                [
                    InlineKeyboardButton(
                        "Gold",
                        callback_data="style:gold"
                    ),

                    InlineKeyboardButton(
                        "Purple",
                        callback_data="style:purple"
                    )
                ],

                [
                    InlineKeyboardButton(
                        "Green",
                        callback_data="style:green"
                    )
                ]

            ]

            await message.reply_text(
                "Choose a logo color style:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

            return

        # ==================================
        # QR CODE GENERATION
        # ==================================

        if tool == "qr":

            if len(text) > 2000:

                await message.reply_text(
                    "Please keep QR content under 2,000 characters."
                )

                return

            image = create_qr(text)

            await message.reply_photo(
                photo=image,
                caption="Your QR code is ready."
            )

        # ==================================
        # BARCODE GENERATION
        # ==================================

        elif tool == "barcode":

            if (
                len(text) > 80
                or not re.fullmatch(r"[ -~]+", text)
            ):

                await message.reply_text(
                    "Please use up to 80 standard letters, "
                    "numbers, or basic symbols."
                )

                return

            image = create_barcode(text)

            await message.reply_photo(
                photo=image,
                caption="Your Code 128 barcode is ready."
            )

    except Exception:

        logger.exception("Generation failed")

        await message.reply_text(
            "I couldn't generate that image. "
            "Please check your input and try again."
        )

    finally:

        if tool in ("qr", "barcode"):

            context.user_data.clear()


# ==========================================
# LOGO STYLE CALLBACK
# ==========================================

async def style_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    if GLOBAL_BOT_MODE == "REDIRECT":

        return

    style = query.data.split(":", 1)[1]

    text = context.user_data.get("pending_text")

    if not text:

        await query.edit_message_text(
            "That logo request expired. Please start again."
        )

        return

    try:

        image = create_logo(text, style)

        await query.message.reply_photo(
            photo=image,
            caption="Your logo is ready."
        )

        await query.edit_message_text(
            "Logo generated successfully."
        )

    except Exception:

        logger.exception("Logo generation failed")

        await query.edit_message_text(
            "I couldn't create that logo. Please try again."
        )

    context.user_data.clear()


# ==========================================
# BOT INITIALIZATION
# ==========================================

async def post_init(application):

    init_db()

    logger.info(
        "Bot started in %s mode",
        GLOBAL_BOT_MODE
    )


# ==========================================
# MAIN FUNCTION
# ==========================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN in your environment variables."
        )

    if not ADMIN_USER_ID:

        logger.warning(
            "ADMIN_USER_ID is missing. Admin mode commands will not work."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Commands

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("cancel", cancel_command)
    )

    application.add_handler(
        CommandHandler("users", users_command)
    )

    # Logo callback

    application.add_handler(
        CallbackQueryHandler(
            style_callback,
            pattern=r"^style:"
        )
    )

    # Text handler

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text
        )
    )

    logger.info("Starting Telegram bot...")

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":

    main()
