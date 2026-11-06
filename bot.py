import os
import uuid
import logging
import sqlite3
import asyncio
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

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "").strip()

CHANNEL_LINK = os.getenv(
    "CHANNEL_LINK",
    "https://t.me/+CaYjuNbPEFA2NWU0"
).strip()

PROMO_TEXT = os.getenv(
    "PROMO_TEXT",
    "Welcome!\n\n"
    "Join our channel to receive updates and information.\n\n"
    "Click the button below to join."
)

DB_PATH = os.getenv("DB_PATH", "/var/data/users.db")

WELCOME_IMAGE = "welcome.jpg"

DEFAULT_MODE = "REDIRECT"

MODE_REDIRECT = "REDIRECT"
MODE_REVERSE = "REVERSE"

BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------
# DATABASE
# Each Telegram user has their own independent mode.
# --------------------------------------------------

def get_connection():
    db_file = Path(DB_PATH)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(DB_PATH)
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def init_database():
    with get_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                mode TEXT NOT NULL DEFAULT 'REDIRECT'
            )
        """)

        connection.commit()


def register_user(user_id):
    with get_connection() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
            (user_id,)
        )

        connection.execute(
            """
            INSERT OR IGNORE INTO user_settings (user_id, mode)
            VALUES (?, ?)
            """,
            (user_id, DEFAULT_MODE)
        )

        connection.commit()


def get_user_mode(user_id):
    with get_connection() as connection:
        result = connection.execute(
            "SELECT mode FROM user_settings WHERE user_id = ?",
            (user_id,)
        ).fetchone()

    if result:
        return result[0]

    return DEFAULT_MODE


def set_user_mode(user_id, mode):
    if mode not in (MODE_REDIRECT, MODE_REVERSE):
        return

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO user_settings (user_id, mode)
            VALUES (?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET mode = excluded.mode
            """,
            (user_id, mode)
        )

        connection.commit()


def get_total_users():
    with get_connection() as connection:
        result = connection.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()

    return result[0] if result else 0


# --------------------------------------------------
# KEYBOARD
# Both mode buttons are always visible to every user.
# --------------------------------------------------

def build_keyboard(mode):
    keyboard = [
        [
            InlineKeyboardButton(
                "REVERSE",
                callback_data="mode:REVERSE"
            ),
            InlineKeyboardButton(
                "REDIRECT",
                callback_data="mode:REDIRECT"
            ),
        ]
    ]

    if mode == MODE_REVERSE:
        keyboard.extend([
            [
                InlineKeyboardButton(
                    "Logo Maker",
                    callback_data="tool:logo"
                ),
                InlineKeyboardButton(
                    "QR Code Generator",
                    callback_data="tool:qr"
                ),
            ],
            [
                InlineKeyboardButton(
                    "Barcode Generator",
                    callback_data="tool:barcode"
                ),
            ],
        ])

    elif mode == MODE_REDIRECT:
        keyboard.append([
            InlineKeyboardButton(
                "Join Channel",
                url=CHANNEL_LINK
            )
        ])

    return InlineKeyboardMarkup(keyboard)


# --------------------------------------------------
# WELCOME MENU
# --------------------------------------------------

async def show_menu(chat_id, context, user_id):
    mode = get_user_mode(user_id)

    if mode == MODE_REVERSE:
        caption = (
            "Welcome!\n\n"
            "Current mode: REVERSE\n\n"
            "Choose one of the software tools below."
        )
    else:
        caption = (
            "Welcome!\n\n"
            "Current mode: REDIRECT\n\n"
            f"{PROMO_TEXT}"
        )

    keyboard = build_keyboard(mode)

    image_path = BASE_DIR / WELCOME_IMAGE

    try:
        if image_path.exists():
            with open(image_path, "rb") as photo:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    reply_markup=keyboard,
                )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption,
                reply_markup=keyboard,
            )

    except Exception:
        logger.exception("Could not send welcome image")
        await context.bot.send_message(
            chat_id=chat_id,
            text=caption,
            reply_markup=keyboard,
        )


# --------------------------------------------------
# START COMMAND
# --------------------------------------------------

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user = update.effective_user

    if not user:
        return

    register_user(user.id)

    # Each user keeps their own previously selected mode.
    context.user_data.pop("awaiting_tool", None)

    await show_menu(
        chat_id=update.effective_chat.id,
        context=context,
        user_id=user.id,
    )


# --------------------------------------------------
# MODE SWITCHING
# This changes only the user who pressed the button.
# --------------------------------------------------

async def mode_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    register_user(user_id)

    selected_mode = query.data.split(":")[1]

    if selected_mode not in (MODE_REDIRECT, MODE_REVERSE):
        return

    set_user_mode(user_id, selected_mode)

    # Cancel any unfinished software tool request.
    context.user_data.pop("awaiting_tool", None)

    await show_menu(
        chat_id=query.message.chat_id,
        context=context,
        user_id=user_id,
    )


# --------------------------------------------------
# SOFTWARE TOOL BUTTONS
# --------------------------------------------------

async def tool_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    user_id = query.from_user.id

    register_user(user_id)

    current_mode = get_user_mode(user_id)

    if current_mode != MODE_REVERSE:
        await query.answer(
            "Select REVERSE first to use the software tools.",
            show_alert=True,
        )
        return

    await query.answer()

    tool = query.data.split(":")[1]

    tool_prompts = {
        "logo": (
            "LOGO MAKER\n\n"
            "Send the name or text you want to use for your logo.\n\n"
            "Send /cancel to cancel."
        ),
        "qr": (
            "QR CODE GENERATOR\n\n"
            "Send the text, website address or information "
            "you want to convert into a QR code.\n\n"
            "Send /cancel to cancel."
        ),
        "barcode": (
            "BARCODE GENERATOR\n\n"
            "Send the numbers or text you want to convert "
            "into a barcode.\n\n"
            "Send /cancel to cancel."
        ),
    }

    if tool not in tool_prompts:
        return

    context.user_data["awaiting_tool"] = tool

    await query.message.reply_text(tool_prompts[tool])


# --------------------------------------------------
# FONT HELPER
# --------------------------------------------------

def get_font(size):
    possible_fonts = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
        "/Library/Fonts/Arial Bold.ttf",
    ]

    for font_path in possible_fonts:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size)
            except Exception:
                pass

    return ImageFont.load_default()


# --------------------------------------------------
# LOGO MAKER
# --------------------------------------------------

def create_logo(text):
    image = Image.new(
        "RGB",
        (1200, 630),
        (248, 250, 252)
    )

    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle(
        (45, 45, 1155, 585),
        radius=35,
        outline=(30, 50, 80),
        width=5,
    )

    draw.line(
        (350, 210, 850, 210),
        fill=(30, 100, 180),
        width=5,
    )

    draw.line(
        (350, 425, 850, 425),
        fill=(30, 100, 180),
        width=5,
    )

    display_text = text.strip()[:40]

    font_size = 100

    while font_size > 25:
        font = get_font(font_size)
        box = draw.textbbox((0, 0), display_text, font=font)
        text_width = box[2] - box[0]

        if text_width <= 950:
            break

        font_size -= 5

    font = get_font(font_size)

    box = draw.textbbox((0, 0), display_text, font=font)

    text_width = box[2] - box[0]
    text_height = box[3] - box[1]

    x = (1200 - text_width) / 2
    y = (315 - text_height) / 2 - box[1]

    draw.text(
        (x, y),
        display_text,
        fill=(20, 35, 60),
        font=font,
    )

    output = os.path.join(
        "/tmp",
        f"logo_{uuid.uuid4().hex}.png"
    )

    image.save(output)

    return output


# --------------------------------------------------
# QR CODE GENERATOR
# --------------------------------------------------

def create_qr_code(data):
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=12,
        border=4,
    )

    qr.add_data(data)
    qr.make(fit=True)

    image = qr.make_image(
        fill_color="black",
        back_color="white"
    ).convert("RGB")

    output = os.path.join(
        "/tmp",
        f"qr_{uuid.uuid4().hex}.png"
    )

    image.save(output)

    return output


# --------------------------------------------------
# BARCODE GENERATOR
# --------------------------------------------------

def create_barcode(data):
    barcode_class = barcode.get_barcode_class("code128")

    barcode_object = barcode_class(
        data,
        writer=ImageWriter()
    )

    output_base = os.path.join(
        "/tmp",
        f"barcode_{uuid.uuid4().hex}"
    )

    output = barcode_object.save(
        output_base,
        options={
            "write_text": True,
            "module_height": 15.0,
            "module_width": 0.3,
        }
    )

    return output


# --------------------------------------------------
# TOOL INPUT HANDLER
# --------------------------------------------------

async def handle_tool_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id

    tool = context.user_data.get("awaiting_tool")

    if not tool:
        return

    # Check the user's mode again before generating anything.
    if get_user_mode(user_id) != MODE_REVERSE:
        context.user_data.pop("awaiting_tool", None)
        await update.message.reply_text(
            "Your current mode is REDIRECT. Select REVERSE "
            "to use the software tools."
        )
        return

    data = update.message.text.strip()

    if not data:
        await update.message.reply_text("Please send valid text.")
        return

    output_path = None

    try:
        if tool == "logo":
            if len(data) > 40:
                await update.message.reply_text(
                    "Please use a name or text with no more than 40 characters."
                )
                return

            output_path = await asyncio.to_thread(
                create_logo,
                data
            )

            caption = "Your logo has been created."

        elif tool == "qr":
            if len(data) > 2000:
                await update.message.reply_text(
                    "Please keep the QR code content below 2,000 characters."
                )
                return

            output_path = await asyncio.to_thread(
                create_qr_code,
                data
            )

            caption = "Your QR code has been created."

        elif tool == "barcode":
            if len(data) > 80:
                await update.message.reply_text(
                    "Please keep the barcode content below 80 characters."
                )
                return

            try:
                data.encode("ascii")
            except UnicodeEncodeError:
                await update.message.reply_text(
                    "Please use standard English letters and numbers "
                    "for barcode generation."
                )
                return

            output_path = await asyncio.to_thread(
                create_barcode,
                data
            )

            caption = "Your barcode has been created."

        else:
            context.user_data.pop("awaiting_tool", None)
            return

        with open(output_path, "rb") as document:
            await update.message.reply_document(
                document=document,
                filename=os.path.basename(output_path),
                caption=caption,
            )

        context.user_data.pop("awaiting_tool", None)

        await update.message.reply_text(
            "You can select another software tool below.",
            reply_markup=build_keyboard(MODE_REVERSE),
        )

    except Exception:
        logger.exception("Tool generation failed")

        await update.message.reply_text(
            "Something went wrong while creating your file. "
            "Please try again."
        )

    finally:
        if output_path and os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass


# --------------------------------------------------
# CANCEL COMMAND
# --------------------------------------------------

async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    context.user_data.pop("awaiting_tool", None)

    await update.message.reply_text(
        "The current operation has been cancelled.",
        reply_markup=build_keyboard(
            get_user_mode(update.effective_user.id)
        ),
    )


# --------------------------------------------------
# HOME COMMAND
# --------------------------------------------------

async def home_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id

    register_user(user_id)
    context.user_data.pop("awaiting_tool", None)

    await show_menu(
        chat_id=update.effective_chat.id,
        context=context,
        user_id=user_id,
    )


# --------------------------------------------------
# ADMIN USERS COMMAND
# --------------------------------------------------

async def users_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if not ADMIN_USER_ID:
        await update.message.reply_text(
            "Admin access has not been configured."
        )
        return

    if str(update.effective_user.id) != ADMIN_USER_ID:
        await update.message.reply_text(
            "You are not authorized to use this command."
        )
        return

    total = get_total_users()

    await update.message.reply_text(
        f"Total registered users: {total}"
    )


# --------------------------------------------------
# HELP COMMAND
# --------------------------------------------------

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    await update.message.reply_text(
        "BOT MENU\n\n"
        "REVERSE:\n"
        "- Logo Maker\n"
        "- QR Code Generator\n"
        "- Barcode Generator\n\n"
        "REDIRECT:\n"
        "- Promotional message\n"
        "- Join Channel button\n\n"
        "Commands:\n"
        "/start - Open the bot\n"
        "/home - Show the menu\n"
        "/cancel - Cancel an active operation\n"
        "/help - Show this help message"
    )


# --------------------------------------------------
# ERROR HANDLER
# --------------------------------------------------

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):
    error = context.error

    logger.error(
        "An error occurred while processing an update",
        exc_info=(
            type(error),
            error,
            error.__traceback__,
        ) if error else None,
    )


# --------------------------------------------------
# MAIN APPLICATION
# --------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN environment variable."
        )

    init_database()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("home", home_command)
    )

    application.add_handler(
        CommandHandler("cancel", cancel_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("users", users_command)
    )

    # Mode buttons
    application.add_handler(
        CallbackQueryHandler(
            mode_callback,
            pattern=r"^mode:(REVERSE|REDIRECT)$"
        )
    )

    # Software tool buttons
    application.add_handler(
        CallbackQueryHandler(
            tool_callback,
            pattern=r"^tool:(logo|qr|barcode)$"
        )
    )

    # Text sent after selecting a software tool
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_tool_input
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Bot is starting...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
