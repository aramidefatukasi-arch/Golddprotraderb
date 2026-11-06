
import asyncio
import logging
import os
import sqlite3
import time
from contextlib import suppress
from pathlib import Path

import barcode
import qrcode
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageFont

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# CONFIGURATION
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "").strip()

CHANNEL_LINK = os.getenv(
    "CHANNEL_LINK",
    "https://t.me/+CaYjuNbPEFA2NWU0"
).strip()

IMAGE_PATH = Path(
    os.getenv("WELCOME_IMAGE_PATH", str(BASE_DIR / "welcome.jpg"))
).expanduser()

DB_FILE = os.getenv(
    "DB_FILE",
    str(BASE_DIR / "users.db")
).strip()

# Bot modes
MODE_REDIRECT = "REDIRECT"
MODE_SOFTWARE = "REVERSE"

# First mode when the database is created
DEFAULT_MODE = MODE_REDIRECT

# Reminder settings
REMINDER_INTERVAL = 6 * 60 * 60
CAMPAIGN_DURATION = 15 * 24 * 60 * 60
CHECK_INTERVAL = 60
SEND_DELAY = 1

WELCOME_TEXT = (
    "Welcome to Lemvest Trading Lab!\n\n"
    "Get updates, information and useful resources "
    "from our Telegram channel.\n\n"
    "Tap the button below to join our channel."
)

PROMO_TEXT = (
    "Welcome to Lemvest Trading Lab!\n\n"
    "Stay connected with our latest updates, information "
    "and resources.\n\n"
    "Tap the button below to visit our Telegram channel."
)

SOFTWARE_MENU_TEXT = (
    "Welcome to the Software Tools Bot.\n\n"
    "Choose a tool below:\n\n"
    "1. Logo Maker\n"
    "2. QR Code Generator\n"
    "3. Barcode Generator"
)


# ============================================================
# DATABASE
# ============================================================

def connect_db():
    connection = sqlite3.connect(DB_FILE, timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def init_db():
    Path(DB_FILE).expanduser().resolve().parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with connect_db() as conn:

        conn.execute("PRAGMA journal_mode=WAL")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at REAL NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            )
        """)

        conn.execute("""
            INSERT OR IGNORE INTO settings
            (setting_key, setting_value)
            VALUES ('bot_mode', ?)
        """, (DEFAULT_MODE,))

        conn.execute("""
            CREATE TABLE IF NOT EXISTS reminder_schedule (
                user_id INTEGER PRIMARY KEY,
                started_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                next_send_at REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_reminders_due
            ON reminder_schedule(active, next_send_at, expires_at)
        """)

        conn.commit()

    logger.info("Database initialized successfully.")


def register_user(user):
    with connect_db() as conn:
        conn.execute("""
            INSERT INTO users (
                user_id,
                username,
                first_name,
                created_at
            )
            VALUES (?, ?, ?, ?)

            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
        """, (
            user.id,
            user.username,
            user.first_name,
            time.time()
        ))

        conn.commit()


def get_mode():
    with connect_db() as conn:
        result = conn.execute("""
            SELECT setting_value
            FROM settings
            WHERE setting_key = 'bot_mode'
        """).fetchone()

    if result and result[0] in (MODE_REDIRECT, MODE_SOFTWARE):
        return result[0]

    return DEFAULT_MODE


def set_mode(mode):
    if mode not in (MODE_REDIRECT, MODE_SOFTWARE):
        raise ValueError("Invalid bot mode.")

    with connect_db() as conn:
        conn.execute("""
            INSERT INTO settings (
                setting_key,
                setting_value
            )
            VALUES ('bot_mode', ?)

            ON CONFLICT(setting_key)
            DO UPDATE SET setting_value = excluded.setting_value
        """, (mode,))

        conn.commit()


def get_user_count():
    with connect_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]


# ============================================================
# REMINDER DATABASE FUNCTIONS
# ============================================================

def start_reminders(user_id):
    now = time.time()

    with connect_db() as conn:

        existing = conn.execute("""
            SELECT active, expires_at
            FROM reminder_schedule
            WHERE user_id = ?
        """, (user_id,)).fetchone()

        if existing and existing[0] == 1 and existing[1] > now:
            return False

        conn.execute("""
            INSERT INTO reminder_schedule (
                user_id,
                started_at,
                expires_at,
                next_send_at,
                active
            )
            VALUES (?, ?, ?, ?, 1)

            ON CONFLICT(user_id) DO UPDATE SET
                started_at = excluded.started_at,
                expires_at = excluded.expires_at,
                next_send_at = excluded.next_send_at,
                active = 1
        """, (
            user_id,
            now,
            now + CAMPAIGN_DURATION,
            now + REMINDER_INTERVAL
        ))

        conn.commit()

    return True


def stop_reminders(user_id):
    with connect_db() as conn:
        conn.execute("""
            UPDATE reminder_schedule
            SET active = 0
            WHERE user_id = ?
        """, (user_id,))

        conn.commit()


def get_due_users():
    now = time.time()

    with connect_db() as conn:
        results = conn.execute("""
            SELECT user_id
            FROM reminder_schedule
            WHERE active = 1
              AND next_send_at <= ?
              AND expires_at > ?
            ORDER BY next_send_at
            LIMIT 50
        """, (now, now)).fetchall()

    return [row[0] for row in results]


def postpone_reminder(user_id, seconds=REMINDER_INTERVAL):
    now = time.time()

    with connect_db() as conn:
        conn.execute("""
            UPDATE reminder_schedule
            SET next_send_at = MIN(expires_at, ?)
            WHERE user_id = ?
              AND active = 1
        """, (now + seconds, user_id))

        conn.commit()


# ============================================================
# ADMIN CHECK
# ============================================================

def is_admin(user_id):
    return (
        ADMIN_USER_ID.isdigit()
        and user_id == int(ADMIN_USER_ID)
    )


# ============================================================
# BUTTONS
# ============================================================

def join_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Join Channel",
                url=CHANNEL_LINK
            )
        ]
    ])


def admin_redirect_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Join Channel",
                url=CHANNEL_LINK
            )
        ],
        [
            InlineKeyboardButton(
                "REDIRECT",
                callback_data="mode:REDIRECT"
            ),
            InlineKeyboardButton(
                "REVERSE",
                callback_data="mode:REVERSE"
            )
        ]
    ])


def software_keyboard(admin=False):

    buttons = [
        [
            InlineKeyboardButton(
                "Logo Maker",
                callback_data="tool:logo"
            )
        ],
        [
            InlineKeyboardButton(
                "QR Code Generator",
                callback_data="tool:qr"
            )
        ],
        [
            InlineKeyboardButton(
                "Barcode Generator",
                callback_data="tool:barcode"
            )
        ]
    ]

    if admin:
        buttons.append([
            InlineKeyboardButton(
                "REDIRECT",
                callback_data="mode:REDIRECT"
            ),
            InlineKeyboardButton(
                "REVERSE",
                callback_data="mode:REVERSE"
            )
        ])

    return InlineKeyboardMarkup(buttons)


# ============================================================
# WELCOME IMAGE
# ============================================================

async def send_photo_or_text(message, caption, keyboard):

    if IMAGE_PATH.is_file():

        try:
            with IMAGE_PATH.open("rb") as photo:

                await message.reply_photo(
                    photo=photo,
                    caption=caption,
                    reply_markup=keyboard
                )

            return

        except (BadRequest, OSError):
            logger.exception("Unable to send welcome image.")

    await message.reply_text(
        caption,
        reply_markup=keyboard,
        disable_web_page_preview=True
    )


# ============================================================
# SHOW CURRENT MODE
# ============================================================

async def show_active_screen(message, user_id):

    mode = await asyncio.to_thread(get_mode)

    admin = is_admin(user_id)

    if admin:

        if mode == MODE_REDIRECT:

            caption = (
                f"{WELCOME_TEXT}\n\n"
                "ADMIN CONTROL PANEL\n\n"
                "Current mode: REDIRECT\n\n"
                "Choose REDIRECT or REVERSE below "
                "to change the bot mode for everyone."
            )

            await send_photo_or_text(
                message,
                caption,
                admin_redirect_keyboard()
            )

        else:

            caption = (
                "Software mode is currently active.\n\n"
                "Choose a software tool below.\n\n"
                "You can switch modes using the buttons."
            )

            await send_photo_or_text(
                message,
                caption,
                software_keyboard(admin=True)
            )

        return

    # Normal users see only the active mode.

    if mode == MODE_REDIRECT:

        await send_photo_or_text(
            message,
            PROMO_TEXT,
            join_keyboard()
        )

    else:

        await message.reply_text(
            SOFTWARE_MENU_TEXT,
            reply_markup=software_keyboard()
        )


# ============================================================
# LOGO MAKER
# ============================================================

def load_font(size, bold=False):

    fonts = (
        ["DejaVuSans-Bold.ttf", "arialbd.ttf"]
        if bold
        else ["DejaVuSans.ttf", "arial.ttf"]
    )

    for font_name in fonts:

        try:
            return ImageFont.truetype(
                font_name,
                size=size
            )

        except OSError:
            continue

    return ImageFont.load_default()


def create_logo(text_value):

    brand = " ".join(text_value.strip().split())[:28]

    if not brand:
        raise ValueError("Please enter a brand name.")

    image = Image.new(
        "RGB",
        (1100, 650),
        "#f4f6fa"
    )

    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle(
        (55, 55, 1045, 595),
        radius=45,
        fill="#ffffff",
        outline="#dce2ec",
        width=4
    )

    draw.ellipse(
        (425, 115, 675, 365),
        fill="#172b4d"
    )

    draw.ellipse(
        (485, 175, 615, 305),
        fill="#ffffff"
    )

    draw.ellipse(
        (520, 210, 580, 270),
        fill="#2878d0"
    )

    font = load_font(68, bold=True)

    while True:

        box = draw.textbbox(
            (0, 0),
            brand,
            font=font
        )

        width = box[2] - box[0]

        if width <= 900 or getattr(font, "size", 68) <= 28:
            break

        font = load_font(
            font.size - 4,
            bold=True
        )

    x_position = (1100 - width) / 2

    draw.text(
        (x_position, 420),
        brand,
        font=font,
        fill="#172b4d"
    )

    tagline = "YOUR BRAND - YOUR IDENTITY"

    tagline_font = load_font(24)

    tagline_box = draw.textbbox(
        (0, 0),
        tagline,
        font=tagline_font
    )

    tagline_width = tagline_box[2] - tagline_box[0]

    draw.text(
        ((1100 - tagline_width) / 2, 520),
        tagline,
        font=tagline_font,
        fill="#68758a"
    )

    output = BASE_DIR / "generated_logo.png"

    image.save(output, format="PNG")

    return output


# ============================================================
# QR CODE GENERATOR
# ============================================================

def create_qr(data):

    value = data.strip()

    if not value:
        raise ValueError(
            "Please send text or a website link."
        )

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4
    )

    qr.add_data(value)
    qr.make(fit=True)

    image = qr.make_image(
        fill_color="black",
        back_color="white"
    ).convert("RGB")

    output = BASE_DIR / "generated_qr.png"

    image.save(output)

    return output


# ============================================================
# BARCODE GENERATOR
# ============================================================

def create_barcode(data):

    value = data.strip()

    if not value:
        raise ValueError(
            "Please send text or numbers for the barcode."
        )

    if len(value) > 60:
        raise ValueError(
            "Please use 60 characters or fewer."
        )

    barcode_class = barcode.get_barcode_class("code128")

    code = barcode_class(
        value,
        writer=ImageWriter()
    )

    saved_path = code.save(
        str(BASE_DIR / "generated_barcode")
    )

    return Path(saved_path)


# ============================================================
# START COMMAND
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    if update.effective_chat.type != "private":

        await message.reply_text(
            "Please open this bot in a private chat and send /start."
        )

        return

    context.user_data.pop("waiting_for", None)

    await asyncio.to_thread(register_user, user)

    mode = await asyncio.to_thread(get_mode)

    if mode == MODE_REDIRECT and not is_admin(user.id):

        await asyncio.to_thread(
            start_reminders,
            user.id
        )

    await show_active_screen(
        message,
        user.id
    )


# ============================================================
# HELP COMMAND
# ============================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_message:

        await update.effective_message.reply_text(
            "/start - Open the current bot mode\n"
            "/help - Show help information\n"
            "/cancel - Cancel a software operation\n"
            "/stop - Stop promotional reminders\n"
            "/users - Admin-only user count"
        )


# ============================================================
# CANCEL COMMAND
# ============================================================

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data.pop("waiting_for", None)

    if update.effective_message:

        await update.effective_message.reply_text(
            "Your current operation has been cancelled."
        )


# ============================================================
# STOP REMINDERS
# ============================================================

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    await asyncio.to_thread(
        stop_reminders,
        user.id
    )

    await message.reply_text(
        "Your promotional reminders have been stopped."
    )


# ============================================================
# ADMIN USERS COMMAND
# ============================================================

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    if not is_admin(user.id):

        await message.reply_text(
            "You are not authorized to use this command."
        )

        return

    count = await asyncio.to_thread(get_user_count)

    await message.reply_text(
        f"Total recorded users: {count}"
    )


# ============================================================
# BUTTON CALLBACK HANDLER
# ============================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    user = update.effective_user

    if query is None or user is None:
        return

    data = query.data or ""

    # --------------------------------------------------------
    # ADMIN MODE SWITCHING
    # --------------------------------------------------------

    if data.startswith("mode:"):

        if not is_admin(user.id):

            await query.answer(
                "Only the bot administrator can change modes.",
                show_alert=True
            )

            return

        new_mode = data.split(":", 1)[1]

        if new_mode not in (MODE_REDIRECT, MODE_SOFTWARE):

            await query.answer(
                "Invalid mode.",
                show_alert=True
            )

            return

        await query.answer("Changing bot mode...")

        await asyncio.to_thread(
            set_mode,
            new_mode
        )

        context.user_data.pop("waiting_for", None)

        if new_mode == MODE_REDIRECT:

            caption = (
                f"{WELCOME_TEXT}\n\n"
                "ADMIN CONTROL PANEL\n\n"
                "Current mode: REDIRECT\n\n"
                "All users now see the channel welcome screen."
            )

            keyboard = admin_redirect_keyboard()

        else:

            caption = (
                "Software mode is now active for all users.\n\n"
                "Choose a tool below or switch modes using "
                "the admin buttons."
            )

            keyboard = software_keyboard(admin=True)

        try:

            if query.message and query.message.photo:

                await query.edit_message_caption(
                    caption=caption,
                    reply_markup=keyboard
                )

            elif query.message:

                await query.edit_message_text(
                    text=caption,
                    reply_markup=keyboard
                )

        except BadRequest:

            logger.exception(
                "Could not update admin control panel."
            )

        logger.info(
            "Admin %s switched mode to %s",
            user.id,
            new_mode
        )

        return

    # --------------------------------------------------------
    # SOFTWARE TOOL BUTTONS
    # --------------------------------------------------------

    if data.startswith("tool:"):

        await query.answer()

        mode = await asyncio.to_thread(get_mode)

        if mode != MODE_SOFTWARE:

            await query.answer(
                "Software tools are currently unavailable.",
                show_alert=True
            )

            return

        tool = data.split(":", 1)[1]

        prompts = {
            "logo": (
                "logo",
                "Send the brand or business name for your logo."
            ),

            "qr": (
                "qr",
                "Send the text or website link for your QR code."
            ),

            "barcode": (
                "barcode",
                "Send the text or numbers for your barcode."
            )
        }

        if tool not in prompts:
            return

        context.user_data["waiting_for"] = prompts[tool][0]

        await query.message.reply_text(
            prompts[tool][1]
        )


# ============================================================
# SOFTWARE TEXT INPUT
# ============================================================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    message = update.effective_message

    if user is None or message is None or not message.text:
        return

    mode = await asyncio.to_thread(get_mode)

    # Ignore software inputs in REDIRECT mode.
    if mode != MODE_SOFTWARE:
        return

    task = context.user_data.get("waiting_for")

    if not task:

        await message.reply_text(
            "Please choose a software tool below.",
            reply_markup=software_keyboard(
                admin=is_admin(user.id)
            )
        )

        return

    try:

        if task == "logo":

            output = await asyncio.to_thread(
                create_logo,
                message.text
            )

            caption = "Your logo is ready."

        elif task == "qr":

            output = await asyncio.to_thread(
                create_qr,
                message.text
            )

            caption = "Your QR code is ready."

        elif task == "barcode":

            output = await asyncio.to_thread(
                create_barcode,
                message.text
            )

            caption = "Your barcode is ready."

        else:

            context.user_data.pop("waiting_for", None)

            await message.reply_text(
                "Please select a tool again."
            )

            return

        with output.open("rb") as file_obj:

            await message.reply_document(
                document=file_obj,
                caption=caption
            )

        context.user_data.pop("waiting_for", None)

        await message.reply_text(
            "Choose another tool:",
            reply_markup=software_keyboard(
                admin=is_admin(user.id)
            )
        )

    except (ValueError, OSError) as exc:

        await message.reply_text(str(exc))

    except Exception:

        logger.exception(
            "Software generation failed for user %s",
            user.id
        )

        await message.reply_text(
            "Sorry, something went wrong. Please try again."
        )


# ============================================================
# PROMOTIONAL REMINDER SENDER
# ============================================================

async def send_reminder(application: Application, user_id: int):

    keyboard = join_keyboard()

    if IMAGE_PATH.is_file():

        try:

            with IMAGE_PATH.open("rb") as photo:

                await application.bot.send_photo(
                    chat_id=user_id,
                    photo=photo,
                    caption=PROMO_TEXT,
                    reply_markup=keyboard
                )

            return

        except (BadRequest, OSError):

            logger.exception(
                "Could not send reminder image to %s",
                user_id
            )

    await application.bot.send_message(
        chat_id=user_id,
        text=PROMO_TEXT,
        reply_markup=keyboard,
        disable_web_page_preview=True
    )


# ============================================================
# BACKGROUND REMINDER WORKER
# ============================================================

async def reminder_loop(application: Application):

    logger.info("Reminder worker started.")

    while True:

        try:

            mode = await asyncio.to_thread(get_mode)

            users = await asyncio.to_thread(get_due_users)

            for user_id in users:

                if mode != MODE_REDIRECT:

                    await asyncio.to_thread(
                        postpone_reminder,
                        user_id,
                        30 * 60
                    )

                    continue

                try:

                    await send_reminder(
                        application,
                        user_id
                    )

                    await asyncio.to_thread(
                        postpone_reminder,
                        user_id,
                        REMINDER_INTERVAL
                    )

                    logger.info(
                        "Reminder sent to %s",
                        user_id
                    )

                except Forbidden:

                    await asyncio.to_thread(
                        stop_reminders,
                        user_id
                    )

                    logger.info(
                        "Reminders stopped for blocked user %s",
                        user_id
                    )

                except RetryAfter as exc:

                    delay = exc.retry_after

                    if hasattr(delay, "total_seconds"):
                        delay = delay.total_seconds()

                    delay = float(delay)

                    await asyncio.to_thread(
                        postpone_reminder,
                        user_id,
                        delay + 5
                    )

                    await asyncio.sleep(delay + 1)

                    break

                except (NetworkError, TelegramError):

                    logger.exception(
                        "Telegram error sending to %s",
                        user_id
                    )

                    await asyncio.to_thread(
                        postpone_reminder,
                        user_id,
                        15 * 60
                    )

                except Exception:

                    logger.exception(
                        "Reminder error for user %s",
                        user_id
                    )

                    await asyncio.to_thread(
                        postpone_reminder,
                        user_id,
                        15 * 60
                    )

                await asyncio.sleep(SEND_DELAY)

        except asyncio.CancelledError:
            raise

        except Exception:

            logger.exception(
                "Reminder worker cycle failed."
            )

        await asyncio.sleep(CHECK_INTERVAL)


# ============================================================
# STARTUP AND SHUTDOWN
# ============================================================

async def post_init(application: Application):

    await asyncio.to_thread(init_db)

    application.bot_data["reminder_task"] = asyncio.create_task(
        reminder_loop(application),
        name="promotional-reminder-worker"
    )

    logger.info("Bot startup completed.")


async def post_stop(application: Application):

    task = application.bot_data.pop(
        "reminder_task",
        None
    )

    if task:

        task.cancel()

        with suppress(asyncio.CancelledError):
            await task


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):

    if context.error:

        logger.error(
            "Unhandled bot error",
            exc_info=(
                type(context.error),
                context.error,
                context.error.__traceback__
            )
        )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing from Render."
        )

    if not ADMIN_USER_ID.isdigit():

        raise RuntimeError(
            "ADMIN_USER_ID must be your numeric Telegram user ID."
        )

    if not CHANNEL_LINK.startswith("https://t.me/"):

        raise RuntimeError(
            "CHANNEL_LINK must be a valid Telegram link."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )

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
        CommandHandler("stop", stop_command)
    )

    application.add_handler(
        CommandHandler("users", users_command)
    )

    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Starting Telegram bot.")

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
