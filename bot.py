
import asyncio
import logging
import os
import sqlite3
import time
from contextlib import closing, suppress
from pathlib import Path
from datetime import timedelta

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
    CommandHandler,
    ContextTypes,
)

# ============================================================
# CONFIGURATION
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent
IMAGE_PATH = BASE_DIR / "welcome.jpg"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHANNEL_LINK = os.getenv(
    "CHANNEL_LINK",
    "https://t.me/+CaYjuNbPEFA2NWU0",
).strip()

# On Render, use a persistent disk path for DB_FILE.
DB_FILE = os.getenv("DB_FILE", str(BASE_DIR / "users.db"))

REMINDER_INTERVAL = 6 * 60 * 60       # Six hours
CAMPAIGN_DURATION = 15 * 24 * 60 * 60 # Fifteen days
CHECK_INTERVAL = 30                   # Check for due reminders
BATCH_SIZE = 50
SEND_DELAY = 1
RETRY_DELAY = 15 * 60                 # Retry temporary failures in 15 minutes

WELCOME_TEXT = """Welcome to Lemvest Trading Lab!

Here, you'll find trading insights, market analysis,
educational content, and practical tools to help you
better understand the markets.

Tap the button below to join our Telegram channel.

You'll receive occasional updates from this bot every
six hours for up to 15 days. Send /stop at any time to
stop these reminders."""

PROMO_TEXT = """Welcome to Lemvest Trading Lab!

Explore our trading insights, market analysis,
educational content, and practical tools.

Tap the button below to visit our channel.

To stop these reminders at any time, send /stop."""


# ============================================================
# DATABASE
# ============================================================

def connect_db():
    connection = sqlite3.connect(DB_FILE, timeout=20)
    connection.execute("PRAGMA busy_timeout = 20000")
    return connection


def init_db():
    Path(DB_FILE).expanduser().resolve().parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with closing(connect_db()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS reminder_schedule (
                user_id INTEGER PRIMARY KEY,
                started_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                next_send_at REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                last_sent_at REAL
            )
        """)

        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(reminder_schedule)"
            )
        }

        # Migration support for the previous database version.
        if "last_sent_at" not in columns:
            conn.execute(
                "ALTER TABLE reminder_schedule "
                "ADD COLUMN last_sent_at REAL"
            )

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_reminder_due
            ON reminder_schedule(active, next_send_at, expires_at)
        """)

        conn.commit()

    logger.info("Database initialized: %s", DB_FILE)


def activate_reminders(user_id: int) -> str:
    """Start a campaign, without resetting an active campaign."""
    now = time.time()

    with closing(connect_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("""
            SELECT active, expires_at
            FROM reminder_schedule
            WHERE user_id = ?
        """, (user_id,)).fetchone()

        # Repeated /start commands must not restart the campaign timer.
        if row and row[0] == 1 and row[1] > now:
            conn.commit()
            return "already_active"

        conn.execute("""
            INSERT INTO reminder_schedule (
                user_id, started_at, expires_at,
                next_send_at, active, last_sent_at
            )
            VALUES (?, ?, ?, ?, 1, NULL)
            ON CONFLICT(user_id) DO UPDATE SET
                started_at = excluded.started_at,
                expires_at = excluded.expires_at,
                next_send_at = excluded.next_send_at,
                active = 1,
                last_sent_at = NULL
        """, (
            user_id,
            now,
            now + CAMPAIGN_DURATION,
            now + REMINDER_INTERVAL,
        ))

        conn.commit()

    return "activated"


def disable_reminders(user_id: int):
    """Disable all future reminders for the user."""
    now = time.time()

    with closing(connect_db()) as conn:
        conn.execute("""
            INSERT INTO reminder_schedule (
                user_id, started_at, expires_at,
                next_send_at, active, last_sent_at
            )
            VALUES (?, ?, ?, ?, 0, NULL)
            ON CONFLICT(user_id) DO UPDATE SET
                active = 0
        """, (
            user_id,
            now,
            now + CAMPAIGN_DURATION,
            now + REMINDER_INTERVAL,
        ))

        conn.commit()


def get_due_users():
    now = time.time()

    with closing(connect_db()) as conn:
        rows = conn.execute("""
            SELECT user_id
            FROM reminder_schedule
            WHERE active = 1
              AND next_send_at <= ?
              AND expires_at > ?
            ORDER BY next_send_at
            LIMIT ?
        """, (now, now, BATCH_SIZE)).fetchall()

    return [row[0] for row in rows]


def claim_reminder(user_id: int) -> bool:
    """Claim a due reminder atomically."""
    now = time.time()

    with closing(connect_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")

        result = conn.execute("""
            UPDATE reminder_schedule
            SET next_send_at = MIN(expires_at, ?)
            WHERE user_id = ?
              AND active = 1
              AND next_send_at <= ?
              AND expires_at > ?
        """, (
            now + REMINDER_INTERVAL,
            user_id,
            now,
            now,
        ))

        conn.commit()
        return result.rowcount == 1


def record_delivery(user_id: int):
    with closing(connect_db()) as conn:
        conn.execute("""
            UPDATE reminder_schedule
            SET last_sent_at = ?
            WHERE user_id = ?
        """, (time.time(), user_id))
        conn.commit()


def schedule_retry(user_id: int, delay: float = RETRY_DELAY):
    now = time.time()

    with closing(connect_db()) as conn:
        conn.execute("""
            UPDATE reminder_schedule
            SET next_send_at = MIN(expires_at, ?)
            WHERE user_id = ? AND active = 1
        """, (now + max(1, delay), user_id))
        conn.commit()


# ============================================================
# KEYBOARDS
# ============================================================

def welcome_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Join Our Channel",
                url=CHANNEL_LINK,
            )
        ]
    ])


def promo_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Visit Lemvest Channel",
                url=CHANNEL_LINK,
            )
        ]
    ])


# ============================================================
# WELCOME MESSAGE
# ============================================================

async def send_welcome(message):
    keyboard = welcome_keyboard()

    if IMAGE_PATH.is_file():
        try:
            with IMAGE_PATH.open("rb") as photo:
                await message.reply_photo(
                    photo=photo,
                    caption=WELCOME_TEXT,
                    reply_markup=keyboard,
                )
            return

        except (BadRequest, OSError):
            logger.exception(
                "Welcome image failed; falling back to text"
            )

    await message.reply_text(
        WELCOME_TEXT,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ============================================================
# PROMOTIONAL REMINDERS
# ============================================================

async def send_promo(bot, user_id: int):
    keyboard = promo_keyboard()

    if IMAGE_PATH.is_file():
        try:
            with IMAGE_PATH.open("rb") as photo:
                await bot.send_photo(
                    chat_id=user_id,
                    photo=photo,
                    caption=PROMO_TEXT,
                    reply_markup=keyboard,
                )
            return

        except BadRequest:
            logger.exception(
                "Promotional image failed for user %s; using text",
                user_id,
            )

    await bot.send_message(
        chat_id=user_id,
        text=PROMO_TEXT,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ============================================================
# BOT COMMANDS
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    # Only private chats should receive personal reminder campaigns.
    if update.effective_chat and update.effective_chat.type != "private":
        await message.reply_text(
            "Please open this bot in a private chat and send /start."
        )
        return

    try:
        status = await asyncio.to_thread(
            activate_reminders,
            user.id,
        )
    except Exception:
        logger.exception(
            "Failed to register user %s",
            user.id,
        )
        await message.reply_text(
            "Sorry, the bot is temporarily unavailable. Please try again."
        )
        return

    await send_welcome(message)

    if status == "activated":
        await message.reply_text(
            "Your reminders are enabled. Your first update is scheduled "
            "in 6 hours, then every 6 hours for up to 15 days. "
            "Send /stop at any time to opt out."
        )
        logger.info("Started reminder campaign for %s", user.id)


async def stop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    try:
        await asyncio.to_thread(disable_reminders, user.id)
    except Exception:
        logger.exception(
            "Failed to disable reminders for %s",
            user.id,
        )
        await message.reply_text(
            "I couldn't save your request. Please try /stop again."
        )
        return

    await message.reply_text(
        "Your reminders have been stopped. You won't receive further "
        "promotional reminders unless you send /start again."
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if update.effective_message:
        await update.effective_message.reply_text(
            "/start — Show the welcome image and join button\n"
            "/stop — Stop promotional reminders\n"
            "/help — Show this help\n\n"
            "Reminders are scheduled every 6 hours for up to 15 days "
            "after you start the bot. Send /stop to opt out."
        )


# ============================================================
# BACKGROUND WORKER
# ============================================================

async def reminder_loop(application: Application):
    await asyncio.sleep(3)

    while True:
        try:
            user_ids = await asyncio.to_thread(get_due_users)

            for user_id in user_ids:
                claimed = await asyncio.to_thread(
                    claim_reminder,
                    user_id,
                )

                if not claimed:
                    continue

                try:
                    await send_promo(application.bot, user_id)

                    await asyncio.to_thread(
                        record_delivery,
                        user_id,
                    )

                    logger.info(
                        "Reminder delivered to %s",
                        user_id,
                    )

                except Forbidden:
                    # User blocked the bot or Telegram no longer allows
                    # the bot to message this user.
                    await asyncio.to_thread(
                        disable_reminders,
                        user_id,
                    )
                    logger.info(
                        "Disabled reminders for blocked user %s",
                        user_id,
                    )

                except RetryAfter as exc:
                    delay = exc.retry_after

                    if isinstance(delay, timedelta):
                        delay = delay.total_seconds()

                    delay = float(delay)

                    await asyncio.to_thread(
                        schedule_retry,
                        user_id,
                        delay + 2,
                    )

                    logger.warning(
                        "Telegram rate limit; pausing for %.1f seconds",
                        delay,
                    )

                    await asyncio.sleep(delay + 1)
                    break

                except (NetworkError, TelegramError):
                    logger.exception(
                        "Telegram error sending to %s",
                        user_id,
                    )
                    await asyncio.to_thread(
                        schedule_retry,
                        user_id,
                    )

                except Exception:
                    logger.exception(
                        "Unexpected delivery failure for %s",
                        user_id,
                    )
                    await asyncio.to_thread(
                        schedule_retry,
                        user_id,
                    )

                # Avoid sending a burst of messages.
                await asyncio.sleep(SEND_DELAY)

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception("Reminder worker cycle failed")

        await asyncio.sleep(CHECK_INTERVAL)


# ============================================================
# STARTUP AND SHUTDOWN
# ============================================================

async def post_init(application: Application):
    await asyncio.to_thread(init_db)

    application.bot_data["reminder_task"] = asyncio.create_task(
        reminder_loop(application),
        name="lemvest-reminder-loop",
    )

    logger.info("Reminder worker started")


async def post_stop(application: Application):
    task = application.bot_data.pop("reminder_task", None)

    if task:
        task.cancel()

        with suppress(asyncio.CancelledError):
            await task


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    error = context.error

    if error:
        logger.error(
            "Unhandled bot error",
            exc_info=(type(error), error, error.__traceback__),
        )


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN in Render environment variables."
        )

    if not CHANNEL_LINK.startswith("https://t.me/"):
        raise RuntimeError(
            "CHANNEL_LINK must be a valid Telegram channel link."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("help", help_command))
    application.add_error_handler(error_handler)

    logger.info("Starting Lemvest Telegram bot")
    application.run_polling()


if __name__ == "__main__":
    main()
