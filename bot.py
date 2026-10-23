import os
import time
import asyncio
import logging
import sqlite3
from pathlib import Path
from contextlib import closing, suppress
from datetime import timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, RetryAfter
from telegram.ext import Application, CommandHandler, ContextTypes


# ==========================================
# 1. CONFIGURATION
# ==========================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

CHANNEL_LINK = "https://t.me/+CaYjuNbPEFA2NWU0"

WELCOME_TEXT = """Welcome to Lemvest Trading Lab!

Here, I share trading insights, market analysis, educational content, and real trading experience to help you better understand the market.

We're just getting started, so you're early!

Join for free and grow with us."""

PROMO_TEXT = """📊 Welcome to Lemvest Trading Lab!

9 years of trading experience. 2,800+ community members. Real market insights, every day.

🎯 Daily Market Briefings
📈 Live Trading Sessions
📚 Trading Education & Practical Tools

Ready to take your trading knowledge further?

👇 Tap the button below to discover what's inside!

Send /stop to stop reminders."""

DB_FILE = os.path.expanduser(
    os.environ.get("DB_FILE", "users.db")
)

SIX_HOURS = 6 * 60 * 60
FIFTEEN_DAYS = 15 * 24 * 60 * 60
CHECK_INTERVAL = 60


# ==========================================
# 2. DATABASE
# ==========================================

def connect_db():
    return sqlite3.connect(DB_FILE, timeout=10)


def init_db():
    Path(DB_FILE).resolve().parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    now = time.time()

    with closing(connect_db()) as conn, conn:
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

        # Import users from your original reminder database,
        # if that table exists. Existing schedules are preserved.
        old_table = conn.execute("""
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name = 'users'
        """).fetchone()

        if old_table:
            conn.execute("""
                INSERT OR IGNORE INTO reminder_schedule (
                    user_id, started_at, expires_at,
                    next_send_at, active
                )
                SELECT user_id, ?, ?, ?, 1 FROM users
            """, (
                now,
                now + FIFTEEN_DAYS,
                now + SIX_HOURS,
            ))


def register_user(user_id):
    now = time.time()

    with closing(connect_db()) as conn, conn:
        conn.execute("""
            INSERT OR IGNORE INTO reminder_schedule (
                user_id, started_at, expires_at,
                next_send_at, active
            )
            VALUES (?, ?, ?, ?, 1)
        """, (
            user_id,
            now,
            now + FIFTEEN_DAYS,
            now + SIX_HOURS,
        ))

        # /start postpones the next reminder, but does not
        # extend the campaign or reactivate stopped reminders.
        conn.execute("""
            UPDATE reminder_schedule
            SET next_send_at = MAX(next_send_at, ?)
            WHERE user_id = ?
        """, (now + SIX_HOURS, user_id))


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
            LIMIT 100
        """, (now, now)).fetchall()

    return [row[0] for row in rows]


def claim_reminder(user_id):
    # Reserve before sending to prevent immediate duplicates.
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
            now + SIX_HOURS,
            user_id,
            now,
            now,
        ))

        return result.rowcount == 1


def record_delivery(user_id):
    with closing(connect_db()) as conn, conn:
        conn.execute("""
            UPDATE reminder_schedule
            SET next_send_at = MAX(next_send_at, ?)
            WHERE user_id = ?
        """, (time.time() + SIX_HOURS, user_id))


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
) -> None:
    message = update.effective_message
    user = update.effective_user

    if message is None or user is None:
        return

    keyboard = [[
        InlineKeyboardButton(
            "Join Our Channel",
            url=CHANNEL_LINK,
        )
    ]]

    # Your original welcome is sent FIRST.
    await message.reply_text(
        text=WELCOME_TEXT,
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )

    # Only private chats receive scheduled reminders.
    if update.effective_chat.type == "private":
        try:
            await asyncio.to_thread(register_user, user.id)
            logger.info("Registered /start for user %s", user.id)
        except Exception:
            logger.exception(
                "Welcome sent, but reminder registration failed "
                "for user %s",
                user.id,
            )


async def stop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.effective_user or not update.effective_message:
        return

    try:
        await asyncio.to_thread(
            disable_reminders,
            update.effective_user.id,
        )
    except Exception:
        logger.exception("Could not save /stop")
        await update.effective_message.reply_text(
            "I couldn't save your request. Please try /stop again."
        )
        return

    await update.effective_message.reply_text(
        "✅ Your reminders have been stopped."
    )


# ==========================================
# 4. BACKGROUND REMINDERS
# ==========================================

async def reminder_loop(application):
    # Allow polling to start before checking reminders.
    await asyncio.sleep(10)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Discover Lemvest",
            url=CHANNEL_LINK,
        )
    ]])

    pause_until = 0.0

    while True:
        try:
            if time.time() >= pause_until:
                users = await asyncio.to_thread(get_due_users)

                for user_id in users:
                    claimed = await asyncio.to_thread(
                        claim_reminder,
                        user_id,
                    )

                    if not claimed:
                        continue

                    try:
                        await application.bot.send_message(
                            chat_id=user_id,
                            text=PROMO_TEXT,
                            reply_markup=keyboard,
                            disable_web_page_preview=True,
                        )

                        await asyncio.to_thread(
                            record_delivery,
                            user_id,
                        )

                        logger.info(
                            "Reminder sent to user %s",
                            user_id,
                        )

                    except Forbidden:
                        await asyncio.to_thread(
                            disable_reminders,
                            user_id,
                        )
                        logger.info(
                            "Blocked user %s disabled",
                            user_id,
                        )

                    except RetryAfter as exc:
                        delay = exc.retry_after

                        if isinstance(delay, timedelta):
                            delay = delay.total_seconds()

                        pause_until = time.time() + float(delay) + 1

                        logger.warning(
                            "Telegram rate limit: paused for %s seconds",
                            delay,
                        )
                        break

                    except Exception:
                        # Keep the reservation even if delivery is
                        # uncertain. Do not retry every minute.
                        logger.exception(
                            "Reminder failed for user %s; "
                            "next attempt delayed six hours",
                            user_id,
                        )

                    # Pace messages to different users.
                    await asyncio.sleep(1)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reminder cycle failed")

        await asyncio.sleep(CHECK_INTERVAL)


async def post_init(application):
    try:
        await asyncio.to_thread(init_db)
    except Exception:
        # A database failure should not stop welcome replies.
        logger.exception(
            "Reminder database unavailable. "
            "Welcome replies remain enabled."
        )
        return

    application.bot_data["reminder_task"] = asyncio.create_task(
        reminder_loop(application),
        name="lemvest_reminders",
    )
    logger.info("Reminder background task started")


async def stop_background_task(application):
    task = application.bot_data.pop("reminder_task", None)

    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def error_handler(update, context):
    error = context.error
    logger.error(
        "Bot error",
        exc_info=(type(error), error, error.__traceback__),
    )


# ==========================================
# 5. MAIN
# ==========================================

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")

    if not token:
        logger.error("CRITICAL: TELEGRAM_BOT_TOKEN is not set!")
        return

    # Explicit event loop for Python 3.14 compatibility.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        application = (
            Application.builder()
            .token(token)
            .post_init(post_init)
            .post_stop(stop_background_task)
            .post_shutdown(stop_background_task)
            .build()
        )

        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("stop", stop))
        application.add_error_handler(error_handler)

        logger.info("Bot is starting with long polling...")
        application.run_polling()

    except Exception:
        logger.exception("Bot crashed")


if __name__ == "__main__":
    main()
