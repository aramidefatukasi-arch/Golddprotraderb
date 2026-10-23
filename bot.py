import os
import time
import asyncio
import logging
import sqlite3
from pathlib import Path
from contextlib import closing
from datetime import timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ==========================================
# 1. CONFIGURATION
# ==========================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Avoid logging Telegram request URLs containing the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

CHANNEL_LINK = "https://t.me/+CaYjuNbPEFA2NWU0"

DB_FILE = os.environ.get("DB_FILE", "users.db")

REMINDER_INTERVAL = 6 * 60 * 60
CAMPAIGN_DURATION = 15 * 24 * 60 * 60
CHECK_INTERVAL = 60
BATCH_SIZE = 100

WELCOME_TEXT = """Welcome to Lemvest Trading Lab!

Here, I share trading insights, market analysis, educational content, and real trading experience to help you better understand the market.

We're just getting started, so you're early!

Join for free and grow with us.

You'll receive a reminder every 6 hours for 15 days.
Send /stop anytime to stop reminders."""

PROMO_TEXT = """📊 Welcome to Lemvest Trading Lab!

9 years of trading experience. 2,800+ community members. Real market insights, every day.

🎯 Daily Market Briefings
📈 Live Trading Sessions
📚 Trading Education & Practical Tools

Ready to take your trading knowledge further?

👇 Tap the button below to discover what's inside!

Send /stop to stop reminders."""

# ==========================================
# 2. DATABASE
# ==========================================


def connect_db():
    return sqlite3.connect(DB_FILE, timeout=30)


def init_db():
    Path(DB_FILE).expanduser().resolve().parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    now = time.time()

    with closing(connect_db()) as conn, conn:
        # Retain compatibility with the original database.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                last_message_time TEXT
            )
        """)

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
            CREATE INDEX IF NOT EXISTS idx_reminder_due
            ON reminder_schedule (active, next_send_at)
        """)

        # One-time migration:
        # Original signup dates were not stored.
        # Existing users begin a 15-day window at migration,
        # with their first reminder six hours later.
        # INSERT OR IGNORE prevents resets on future restarts.
        conn.execute("""
            INSERT OR IGNORE INTO reminder_schedule (
                user_id,
                started_at,
                expires_at,
                next_send_at,
                active
            )
            SELECT user_id, ?, ?, ?, 1
            FROM users
        """, (
            now,
            now + CAMPAIGN_DURATION,
            now + REMINDER_INTERVAL,
        ))

    logger.info("Reminder database initialized.")


def register_user(user_id):
    """Register once; /start never extends the 15-day window."""
    now = time.time()

    with closing(connect_db()) as conn, conn:
        conn.execute("""
            INSERT OR IGNORE INTO reminder_schedule (
                user_id,
                started_at,
                expires_at,
                next_send_at,
                active
            )
            VALUES (?, ?, ?, ?, 1)
        """, (
            user_id,
            now,
            now + CAMPAIGN_DURATION,
            now + REMINDER_INTERVAL,
        ))

        # Avoid sending a reminder immediately after /start.
        # Do not reactivate stopped users or extend expiry.
        conn.execute("""
            UPDATE reminder_schedule
            SET next_send_at = MAX(next_send_at, ?)
            WHERE user_id = ?
        """, (
            now + REMINDER_INTERVAL,
            user_id,
        ))

        row = conn.execute("""
            SELECT active, expires_at
            FROM reminder_schedule
            WHERE user_id = ?
        """, (user_id,)).fetchone()

    return bool(row[0]) and row[1] > now


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
        """, (
            now,
            now,
            BATCH_SIZE,
        )).fetchall()

    return [row[0] for row in rows]


def claim_reminder(user_id):
    """
    Atomically save the next eligible time BEFORE sending.

    Concurrent jobs sharing this database cannot both claim
    the same reminder.
    """
    now = time.time()

    with closing(connect_db()) as conn, conn:
        result = conn.execute("""
            UPDATE reminder_schedule
            SET next_send_at = ?
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

        return result.rowcount == 1


def record_delivery(user_id):
    """Schedule the next reminder six hours after delivery."""
    with closing(connect_db()) as conn, conn:
        conn.execute("""
            UPDATE reminder_schedule
            SET next_send_at = MAX(next_send_at, ?)
            WHERE user_id = ?
        """, (
            time.time() + REMINDER_INTERVAL,
            user_id,
        ))


def disable_reminders(user_id):
    with closing(connect_db()) as conn, conn:
        conn.execute("""
            UPDATE reminder_schedule
            SET active = 0
            WHERE user_id = ?
        """, (user_id,))


# ==========================================
# 3. COMMANDS
# ==========================================


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.effective_message:
        return

    if not update.effective_chat or update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "Please open a private chat with me and send /start."
        )
        return

    reminders_active = register_user(update.effective_user.id)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "Join Our Channel",
            url=CHANNEL_LINK,
        )]
    ])

    text = WELCOME_TEXT

    if not reminders_active:
        text = WELCOME_TEXT.replace(
            "You'll receive a reminder every 6 hours for 15 days.\n"
            "Send /stop anytime to stop reminders.",
            "Your reminders are stopped or your 15-day period has ended.\n"
            "You can still join the channel using the button below.",
        )

    await update.effective_message.reply_text(
        text=text,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def stop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.effective_message:
        return

    disable_reminders(update.effective_user.id)

    await update.effective_message.reply_text(
        "✅ Your reminders have been stopped."
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_message:
        return

    await update.effective_message.reply_text(
        "/start — Open the welcome message and channel link\n"
        "/stop — Stop reminders\n"
        "/help — Show this help\n\n"
        "Reminders are scheduled six hours apart and end "
        "15 days after registration. Sending /start again "
        "does not extend the period or restart stopped reminders."
    )


# ==========================================
# 4. REMINDER JOB
# ==========================================


async def broadcast_task(context: ContextTypes.DEFAULT_TYPE):
    # Respect Telegram's rate-limit cooldown across job runs.
    pause_until = context.application.bot_data.get(
        "broadcast_pause_until",
        0,
    )

    if time.time() < pause_until:
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "Discover Lemvest",
            url=CHANNEL_LINK,
        )]
    ])

    for user_id in get_due_users():
        # Commit the six-hour reservation before sending.
        # Database failures here abort the job without sending.
        if not claim_reminder(user_id):
            continue

        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=PROMO_TEXT,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

            record_delivery(user_id)
            logger.info("Reminder delivered to user %s", user_id)

        except Forbidden:
            disable_reminders(user_id)
            logger.info(
                "Reminders disabled: bot blocked by user %s",
                user_id,
            )

        except RetryAfter as exc:
            delay = exc.retry_after

            if isinstance(delay, timedelta):
                delay = delay.total_seconds()

            context.application.bot_data[
                "broadcast_pause_until"
            ] = time.time() + float(delay) + 1

            logger.warning(
                "Telegram rate limit: pausing broadcasts for %s seconds.",
                delay,
            )

            # Keep this user's six-hour reservation.
            # No immediate retry.
            return

        except Exception:
            # Telegram may have received a message even when
            # our request times out. Keep the reservation to
            # avoid immediately sending a duplicate.
            logger.exception(
                "Reminder attempt failed for user %s; "
                "six-hour reservation retained.",
                user_id,
            )

        # This spaces messages to DIFFERENT users.
        # Each user's six-hour limit is enforced in SQLite.
        await asyncio.sleep(1)


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    error = context.error
    logger.error(
        "Unhandled bot error",
        exc_info=(type(error), error, error.__traceback__),
    )


# ==========================================
# 5. MAIN
# ==========================================


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")

    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing from environment variables."
        )

    init_db()

    # Explicit event loop for compatibility with Python 3.14.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = Application.builder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("help", help_command))
    application.add_error_handler(error_handler)

    if application.job_queue is None:
        raise RuntimeError(
            'JobQueue is unavailable. Install '
            '"python-telegram-bot[job-queue]".'
        )

    application.job_queue.run_repeating(
        broadcast_task,
        interval=CHECK_INTERVAL,
        first=10,
        name="six_hour_reminders",
        job_kwargs={
            "max_instances": 1,
            "coalesce": True,
        },
    )

    logger.info(
        "Bot starting: reminders six hours apart, "
        "15-day expiry, database=%s",
        DB_FILE,
    )

    application.run_polling()


if __name__ == "__main__":
    main()
