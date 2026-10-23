import os
import time
import asyncio
import logging
import sqlite3

from pathlib import Path
from contextlib import closing, suppress
from datetime import timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Forbidden, RetryAfter, BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes


# ==========================================
# 1. CONFIGURATION
# ==========================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Avoid logging request URLs containing the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent
WELCOME_IMAGE = BASE_DIR / "welcome.jpg"

DB_FILE = os.path.expanduser(
    os.environ.get("DB_FILE", str(BASE_DIR / "users.db"))
)

CHANNEL_LINK = "https://t.me/+CaYjuNbPEFA2NWU0"

REMINDER_INTERVAL = 6 * 60 * 60
CAMPAIGN_DURATION = 15 * 24 * 60 * 60
CHECK_INTERVAL = 60
BATCH_SIZE = 100

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

        old_table = conn.execute("""
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name = 'users'
        """).fetchone()

        if old_table:
            # Import original users only once.
            # Original signup dates were not stored, so imported
            # users begin a 15-day period at migration.
            # Existing reminder schedules remain unchanged.
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

    logger.info("Reminder database initialized: %s", DB_FILE)


def register_user(user_id):
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

        # Repeated /start postpones the next reminder.
        # It does not extend expiry or reactivate stopped users.
        conn.execute("""
            UPDATE reminder_schedule
            SET next_send_at = MAX(next_send_at, ?)
            WHERE user_id = ?
        """, (
            now + REMINDER_INTERVAL,
            user_id,
        ))


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
    """Reserve the next six hours BEFORE attempting delivery."""
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
    """Keep the next reminder six hours after delivery."""
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
    now = time.time()

    with closing(connect_db()) as conn, conn:
        # Also record /stop for users without an existing schedule.
        conn.execute("""
            INSERT OR IGNORE INTO reminder_schedule (
                user_id,
                started_at,
                expires_at,
                next_send_at,
                active
            )
            VALUES (?, ?, ?, ?, 0)
        """, (
            user_id,
            now,
            now + CAMPAIGN_DURATION,
            now + REMINDER_INTERVAL,
        ))

        conn.execute("""
            UPDATE reminder_schedule
            SET active = 0
            WHERE user_id = ?
        """, (user_id,))


# ==========================================
# 3. WELCOME MESSAGE
# ==========================================

async def send_welcome(message):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Join Our Channel",
                url=CHANNEL_LINK,
            )
        ]
    ])

    # Send the image, text and button together.
    if WELCOME_IMAGE.is_file():
        try:
            with WELCOME_IMAGE.open("rb") as photo:
                await message.reply_photo(
                    photo=photo,
                    caption=WELCOME_TEXT,
                    reply_markup=keyboard,
                )
            return

        except (OSError, BadRequest):
            # A missing/unreadable/rejected image falls back to text.
            logger.exception(
                "Welcome image could not be sent; using text."
            )

    else:
        logger.warning(
            "Welcome image not found: %s",
            WELCOME_IMAGE,
        )

    await message.reply_text(
        text=WELCOME_TEXT,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ==========================================
# 4. COMMANDS
# ==========================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if message is None or user is None or chat is None:
        return

    # Welcome comes first, independently of the reminder database.
    await send_welcome(message)

    if chat.type == "private":
        try:
            await asyncio.to_thread(register_user, user.id)
            logger.info("/start handled for user %s", user.id)

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
        logger.exception("Failed to save /stop")

        await update.effective_message.reply_text(
            "I couldn't save your request. Please try /stop again."
        )
        return

    await update.effective_message.reply_text(
        "✅ Your reminders have been stopped."
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.effective_message:
        return

    await update.effective_message.reply_text(
        "/start — Show the welcome image and channel link\n"
        "/stop — Stop reminders\n"
        "/help — Show this help\n\n"
        "Reminders start six hours after registration and are "
        "scheduled six hours apart for a 15-day period.\n\n"
        "Sending /start again does not extend the period "
        "or restart stopped reminders."
    )


# ==========================================
# 5. REMINDER LOOP
# ==========================================

async def reminder_loop(application):
    await asyncio.sleep(10)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Discover Lemvest",
                url=CHANNEL_LINK,
            )
        ]
    ])

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
                            "Reminders disabled for blocked user %s",
                            user_id,
                        )

                    except RetryAfter as exc:
                        delay = exc.retry_after

                        if isinstance(delay, timedelta):
                            delay = delay.total_seconds()

                        pause_until = (
                            time.time() + float(delay) + 1
                        )

                        logger.warning(
                            "Telegram rate limit: pausing reminders "
                            "for %s seconds",
                            delay,
                        )
                        break

                    except Exception:
                        # Keep the six-hour reservation even after
                        # errors. A timeout may mean Telegram already
                        # delivered the message.
                        logger.exception(
                            "Reminder failed for user %s; "
                            "six-hour reservation retained",
                            user_id,
                        )

                    # One second BETWEEN DIFFERENT USERS.
                    # The database enforces each user's six-hour gap.
                    await asyncio.sleep(1)

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception("Reminder cycle failed")

        await asyncio.sleep(CHECK_INTERVAL)


# ==========================================
# 6. STARTUP AND SHUTDOWN
# ==========================================

async def post_init(application):
    try:
        await asyncio.to_thread(init_db)

    except Exception:
        logger.exception(
            "Reminder database failed to initialize. "
            "Welcome replies remain available. "
            "Fix the database path and restart to enable reminders."
        )
        return

    application.bot_data["reminder_task"] = asyncio.create_task(
        reminder_loop(application),
        name="lemvest_reminders",
    )

    logger.info("Background reminder task started")


async def stop_background_task(application):
    task = application.bot_data.pop("reminder_task", None)

    if task is not None:
        task.cancel()

        with suppress(asyncio.CancelledError):
            await task


async def error_handler(update, context):
    error = context.error

    logger.error(
        "Unhandled bot error",
        exc_info=(
            type(error),
            error,
            error.__traceback__,
        ),
    )


# ==========================================
# 7. MAIN
# ==========================================

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")

    if not token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set in environment variables."
        )

    # Explicit event loop for environments without a default loop.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application = (
        Application.builder()
        .token(token)
        # Reminders use asyncio; no JobQueue is needed.
        .job_queue(None)
        .post_init(post_init)
        .post_stop(stop_background_task)
        .post_shutdown(stop_background_task)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("help", help_command))
    application.add_error_handler(error_handler)

    logger.info("Bot is starting with long polling...")

    application.run_polling()


if __name__ == "__main__":
    main()
