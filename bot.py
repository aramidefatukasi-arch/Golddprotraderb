
import asyncio
import logging
import os
import sqlite3
import time
from contextlib import closing, suppress
from datetime import timedelta
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
)
from telegram.ext import Application, CommandHandler, ContextTypes

# ============================================================
# CONFIGURATION
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("lemvest_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent
IMAGE_PATH = BASE_DIR / "welcome.jpg"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "").strip()
CHANNEL_LINK = os.getenv(
    "CHANNEL_LINK",
    "https://t.me/+CaYjuNbPEFA2NWU0",
).strip()

DB_FILE = Path(
    os.getenv("DB_FILE", str(BASE_DIR / "users.db"))
).expanduser()

REMINDER_INTERVAL = 6 * 60 * 60
CAMPAIGN_DURATION = 15 * 24 * 60 * 60
CHECK_INTERVAL = 60
BATCH_SIZE = 50
RETRY_DELAY = 15 * 60

WELCOME_TEXT = """Welcome to Lemvest Trading Lab!

Here, I share trading insights, market analysis, educational content, and practical information to help you better understand the markets.

We're just getting started, so you're early!

Join for free and grow with us."""

PROMO_TEXT = """Welcome to Lemvest Trading Lab!

Explore trading insights, market analysis, educational content, and practical tools.

Tap the button below to visit our channel.

Send /stop at any time to stop reminders."""


# ============================================================
# DATABASE
# ============================================================

def connect_db():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_FILE), timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db():
    with closing(connect_db()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS reminder_schedule (
                user_id INTEGER PRIMARY KEY,
                started_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                next_send_at REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                claim_until REAL,
                last_sent_at REAL
            )
        """)

        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(reminder_schedule)"
            )
        }

        if "claim_until" not in columns:
            conn.execute(
                "ALTER TABLE reminder_schedule "
                "ADD COLUMN claim_until REAL"
            )

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


def register_user(user_id: int) -> str:
    """Start a campaign without resetting an active schedule."""
    now = time.time()

    with closing(connect_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("""
            SELECT active, expires_at
            FROM reminder_schedule
            WHERE user_id = ?
        """, (user_id,)).fetchone()

        if row and row[0] == 1 and row[1] > now:
            conn.commit()
            return "already_active"

        conn.execute("""
            INSERT INTO reminder_schedule (
                user_id,
                started_at,
                expires_at,
                next_send_at,
                active,
                claim_until,
                last_sent_at
            )
            VALUES (?, ?, ?, ?, 1, NULL, NULL)

            ON CONFLICT(user_id) DO UPDATE SET
                started_at = excluded.started_at,
                expires_at = excluded.expires_at,
                next_send_at = excluded.next_send_at,
                active = 1,
                claim_until = NULL,
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
    now = time.time()

    with closing(connect_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")

        conn.execute("""
            INSERT INTO reminder_schedule (
                user_id,
                started_at,
                expires_at,
                next_send_at,
                active,
                claim_until,
                last_sent_at
            )
            VALUES (?, ?, ?, ?, 0, NULL, NULL)

            ON CONFLICT(user_id) DO UPDATE SET
                active = 0,
                claim_until = NULL
        """, (
            user_id,
            now,
            now + CAMPAIGN_DURATION,
            now + REMINDER_INTERVAL,
        ))

        conn.commit()


def get_user_counts():
    now = time.time()

    with closing(connect_db()) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM reminder_schedule"
        ).fetchone()[0]

        active = conn.execute("""
            SELECT COUNT(*)
            FROM reminder_schedule
            WHERE active = 1 AND expires_at > ?
        """, (now,)).fetchone()[0]

    return total, active, total - active


def get_due_users():
    now = time.time()

    with closing(connect_db()) as conn:
        rows = conn.execute("""
            SELECT user_id
            FROM reminder_schedule
            WHERE active = 1
              AND next_send_at <= ?
              AND expires_at > ?
              AND (claim_until IS NULL OR claim_until < ?)
            ORDER BY next_send_at
            LIMIT ?
        """, (
            now,
            now,
            now,
            BATCH_SIZE,
        )).fetchall()

    return [row[0] for row in rows]


def claim_reminder(user_id: int) -> bool:
    """
    Temporarily claim a due reminder.
    IMPORTANT: Do not advance next_send_at here.
    """
    now = time.time()

    with closing(connect_db()) as conn:
        conn.execute("BEGIN IMMEDIATE")

        result = conn.execute("""
            UPDATE reminder_schedule
            SET claim_until = ?
            WHERE user_id = ?
              AND active = 1
              AND next_send_at <= ?
              AND expires_at > ?
              AND (claim_until IS NULL OR claim_until < ?)
        """, (
            now + 300,
            user_id,
            now,
            now,
            now,
        ))

        conn.commit()

    return result.rowcount == 1


def record_delivery(user_id: int):
    """Schedule the next reminder six hours after successful delivery."""
    now = time.time()

    with closing(connect_db()) as conn:
        conn.execute("""
            UPDATE reminder_schedule
            SET last_sent_at = ?,
                next_send_at = MIN(expires_at, ?),
                claim_until = NULL
            WHERE user_id = ? AND active = 1
        """, (
            now,
            now + REMINDER_INTERVAL,
            user_id,
        ))

        conn.commit()


def schedule_retry(user_id: int, delay: float = RETRY_DELAY):
    """Retry a failed delivery later, not every minute."""
    now = time.time()

    with closing(connect_db()) as conn:
        conn.execute("""
            UPDATE reminder_schedule
            SET next_send_at = MIN(expires_at, ?),
                claim_until = NULL
            WHERE user_id = ? AND active = 1
        """, (
            now + max(1, delay),
            user_id,
        ))

        conn.commit()


# ============================================================
# IMAGE AND MESSAGE SENDING
# ============================================================

def channel_keyboard(label: str):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(label, url=CHANNEL_LINK)
    ]])


async def send_photo_or_text(
    *,
    message=None,
    bot=None,
    chat_id=None,
    caption: str,
    keyboard,
):
    """
    Upload welcome.jpg directly from the project folder.
    If the image is missing or rejected, send the text instead.
    """
    if not IMAGE_PATH.is_file():
        logger.error(
            "Image file is missing: %s. Add welcome.jpg beside bot.py.",
            IMAGE_PATH,
        )

        if message is not None:
            await message.reply_text(
                caption,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=caption,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        return

    try:
        with IMAGE_PATH.open("rb") as photo:
            if message is not None:
                await message.reply_photo(
                    photo=photo,
                    caption=caption,
                    reply_markup=keyboard,
                )
            else:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    reply_markup=keyboard,
                )

    except (BadRequest, OSError):
        logger.exception("Image upload failed; falling back to text")

        if message is not None:
            await message.reply_text(
                caption,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=caption,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )


async def send_welcome(message):
    await send_photo_or_text(
        message=message,
        caption=WELCOME_TEXT,
        keyboard=channel_keyboard("Join Our Channel"),
    )


async def send_promo(bot, user_id: int):
    await send_photo_or_text(
        bot=bot,
        chat_id=user_id,
        caption=PROMO_TEXT,
        keyboard=channel_keyboard("Visit Lemvest Channel"),
    )


# ============================================================
# BOT COMMANDS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat

    if user is None or message is None:
        return

    if chat is None or chat.type != "private":
        await message.reply_text(
            "Please open this bot in a private chat and send /start."
        )
        return

    try:
        status = await asyncio.to_thread(register_user, user.id)
    except Exception:
        logger.exception("Failed to register user %s", user.id)
        await message.reply_text(
            "Sorry, the bot is temporarily unavailable. Please try again."
        )
        return

    await send_welcome(message)

    if status == "activated":
        await message.reply_text(
            "Reminders are enabled. Your first reminder will arrive in "
            "6 hours, then every 6 hours for up to 15 days. "
            "Send /stop to opt out."
        )
    else:
        await message.reply_text(
            "Your reminders are already active. Your existing schedule "
            "has not been reset."
        )

    logger.info("/start handled for user %s: %s", user.id, status)


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    try:
        await asyncio.to_thread(disable_reminders, user.id)

        await message.reply_text(
            "Your reminders have been stopped. "
            "Send /start to opt in again."
        )

    except Exception:
        logger.exception("Failed to stop reminders for %s", user.id)
        await message.reply_text(
            "I couldn't save your request. Please try /stop again."
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_message:
        await update.effective_message.reply_text(
            "/start — Show welcome message and enable reminders\n"
            "/stop — Stop reminders\n"
            "/help — Show help\n"
            "/users — Admin-only user statistics\n\n"
            "The first reminder is sent after 6 hours, then every "
            "6 hours for up to 15 days."
        )


async def users_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if user is None or message is None:
        return

    if not ADMIN_USER_ID.isdigit():
        await message.reply_text(
            "Admin access is not configured. Check Render environment variables."
        )
        logger.error("ADMIN_USER_ID is missing or invalid")
        return

    if user.id != int(ADMIN_USER_ID):
        await message.reply_text(
            "You are not authorized to use this command."
        )
        return

    try:
        total, active, inactive = await asyncio.to_thread(get_user_counts)

        await message.reply_text(
            "Bot User Statistics\n\n"
            f"Total users: {total}\n"
            f"Active reminder campaigns: {active}\n"
            f"Stopped or expired campaigns: {inactive}"
        )

    except Exception:
        logger.exception("Failed to retrieve user statistics")
        await message.reply_text(
            "Unable to retrieve statistics right now."
        )


# ============================================================
# BACKGROUND REMINDER WORKER
# ============================================================

async def reminder_loop(application: Application):
    logger.info(
        "Reminder worker started. Schedule check interval: %s seconds",
        CHECK_INTERVAL,
    )

    while True:
        try:
            user_ids = await asyncio.to_thread(get_due_users)

            for user_id in user_ids:
                claimed = await asyncio.to_thread(claim_reminder, user_id)

                if not claimed:
                    continue

                try:
                    await send_promo(application.bot, user_id)

                    # Advance the schedule only after successful delivery.
                    await asyncio.to_thread(record_delivery, user_id)

                    logger.info(
                        "Reminder sent to %s. Next reminder due in six hours.",
                        user_id,
                    )

                except Forbidden:
                    await asyncio.to_thread(disable_reminders, user_id)
                    logger.info(
                        "Reminders disabled for blocked user %s",
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
                        "Telegram rate limit. Retry after %.1f seconds.",
                        delay,
                    )

                    await asyncio.sleep(delay + 1)
                    break

                except (NetworkError, TelegramError):
                    logger.exception(
                        "Telegram delivery failed for user %s",
                        user_id,
                    )
                    await asyncio.to_thread(schedule_retry, user_id)

                except Exception:
                    logger.exception(
                        "Unexpected delivery error for user %s",
                        user_id,
                    )
                    await asyncio.to_thread(schedule_retry, user_id)

                await asyncio.sleep(1)

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception("Reminder worker cycle failed")

        # This only checks the database. It does NOT send a message
        # unless that user's scheduled due time has passed.
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

    logger.info("Background reminder task started")


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
    if context.error:
        logger.error(
            "Unhandled bot error",
            exc_info=(
                type(context.error),
                context.error,
                context.error.__traceback__,
            ),
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
            "CHANNEL_LINK must start with https://t.me/"
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
    application.add_handler(CommandHandler("users", users_command))
    application.add_error_handler(error_handler)

    logger.info("Starting Lemvest Trading Lab bot")
    application.run_polling()


if __name__ == "__main__":
    main()
