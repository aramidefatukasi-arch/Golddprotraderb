
import asyncio
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
IMAGE_PATH = BASE_DIR / "welcome.jpg"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "").strip()
CHANNEL_LINK = os.getenv(
    "CHANNEL_LINK",
    "https://t.me/+CaYjuNbPEFA2NWU0",
).strip()

REMINDER_INTERVAL = 6 * 60 * 60
CAMPAIGN_DURATION = 15 * 24 * 60 * 60
CLAIM_TIMEOUT = 5 * 60
RETRY_DELAY = 5 * 60
CHECK_INTERVAL = 30

DATA_DIR = Path("/var/data")
PERSISTENT_DB = DATA_DIR / "users.db"
LEGACY_DB = BASE_DIR / "users.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - lemvest_bot - %(levelname)s - %(message)s",
)
logger = logging.getLogger("lemvest_bot")


# --------------------------------------------------
# DATABASE LOCATION AND SAFE LEGACY COPY
# --------------------------------------------------

def configure_database():
    """
    Prefer the persistent disk.
    If the persistent database does not exist, copy a
    legacy users.db into it before creating a new database.
    Never overwrite an existing persistent database.
    """
    requested_path = os.getenv("DB_FILE", "").strip()

    if requested_path:
        db_path = Path(requested_path)
    else:
        db_path = PERSISTENT_DB

    db_path.parent.mkdir(parents=True, exist_ok=True)

    if not db_path.exists():
        legacy_candidates = [
            LEGACY_DB,
            BASE_DIR / "data" / "users.db",
            Path.cwd() / "users.db",
        ]

        for candidate in legacy_candidates:
            try:
                if (
                    candidate.resolve() != db_path.resolve()
                    and candidate.is_file()
                ):
                    shutil.copy2(candidate, db_path)
                    logger.info(
                        "Copied legacy database from %s to %s",
                        candidate,
                        db_path,
                    )
                    break
            except OSError:
                logger.exception(
                    "Could not copy legacy database from %s",
                    candidate,
                )

    logger.info("Database path: %s", db_path)
    return db_path


DB_PATH = configure_database()


def connect_db():
    connection = sqlite3.connect(
        str(DB_PATH),
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def initialize_database():
    with connect_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                blocked INTEGER NOT NULL DEFAULT 0
            )
        """)

        db.execute("""
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

        # Safely add columns when using an older database.
        existing_columns = {
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(reminder_schedule)"
            ).fetchall()
        }

        required_columns = {
            "started_at": "REAL",
            "expires_at": "REAL",
            "next_send_at": "REAL",
            "active": "INTEGER NOT NULL DEFAULT 1",
            "claim_until": "REAL",
            "last_sent_at": "REAL",
        }

        for column, definition in required_columns.items():
            if column not in existing_columns:
                db.execute(
                    f"ALTER TABLE reminder_schedule "
                    f"ADD COLUMN {column} {definition}"
                )

        # Make sure every previously scheduled user is in users.
        now = time.time()
        db.execute("""
            INSERT OR IGNORE INTO users
                (user_id, username, first_name, first_seen, last_seen)
            SELECT user_id, NULL, NULL, ?, ?
            FROM reminder_schedule
        """, (now, now))

        # Recover schedules that were left with an expired claim.
        db.execute("""
            UPDATE reminder_schedule
            SET claim_until = NULL
            WHERE claim_until IS NOT NULL
              AND claim_until < ?
        """, (now,))

    logger.info("Database initialized successfully")


# --------------------------------------------------
# USER REGISTRATION
# --------------------------------------------------

def register_user(user):
    now = time.time()

    with connect_db() as db:
        db.execute("""
            INSERT INTO users
                (user_id, username, first_name, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_seen = excluded.last_seen
        """, (
            user.id,
            user.username,
            user.first_name,
            now,
            now,
        ))

        db.execute("""
            UPDATE users
            SET blocked = 0
            WHERE user_id = ?
        """, (user.id,))


def get_user_counts():
    with connect_db() as db:
        total = db.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        active = db.execute("""
            SELECT COUNT(*)
            FROM reminder_schedule
            WHERE active = 1 AND expires_at > ?
        """, (time.time(),)).fetchone()[0]

        return total, active


# --------------------------------------------------
# REMINDER SCHEDULE
# --------------------------------------------------

def start_campaign(user_id):
    now = time.time()

    with connect_db() as db:
        existing = db.execute("""
            SELECT active, expires_at
            FROM reminder_schedule
            WHERE user_id = ?
        """, (user_id,)).fetchone()

        # Do not reset an existing active campaign.
        if existing and existing["active"] and existing["expires_at"] > now:
            return False

        db.execute("""
            INSERT INTO reminder_schedule
                (user_id, started_at, expires_at, next_send_at,
                 active, claim_until, last_sent_at)
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

        return True


def stop_campaign(user_id):
    with connect_db() as db:
        db.execute("""
            UPDATE reminder_schedule
            SET active = 0, claim_until = NULL
            WHERE user_id = ?
        """, (user_id,))


def claim_due_reminder():
    """
    Atomically claim one due reminder without advancing
    next_send_at. The next send time changes only after
    a successful Telegram delivery.
    """
    now = time.time()

    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")

        row = db.execute("""
            SELECT s.user_id
            FROM reminder_schedule AS s
            JOIN users AS u ON u.user_id = s.user_id
            WHERE s.active = 1
              AND s.expires_at > ?
              AND s.next_send_at <= ?
              AND (s.claim_until IS NULL OR s.claim_until < ?)
              AND u.blocked = 0
            ORDER BY s.next_send_at ASC
            LIMIT 1
        """, (now, now, now)).fetchone()

        if row is None:
            db.commit()
            return None

        user_id = row["user_id"]

        db.execute("""
            UPDATE reminder_schedule
            SET claim_until = ?
            WHERE user_id = ?
        """, (now + CLAIM_TIMEOUT, user_id))

        db.commit()
        return user_id


def record_delivery(user_id):
    now = time.time()

    with connect_db() as db:
        row = db.execute("""
            SELECT expires_at
            FROM reminder_schedule
            WHERE user_id = ?
        """, (user_id,)).fetchone()

        if row is None:
            return

        if now >= row["expires_at"]:
            db.execute("""
                UPDATE reminder_schedule
                SET active = 0, claim_until = NULL,
                    last_sent_at = ?
                WHERE user_id = ?
            """, (now, user_id))
        else:
            db.execute("""
                UPDATE reminder_schedule
                SET last_sent_at = ?,
                    next_send_at = ?,
                    claim_until = NULL,
                    active = CASE
                        WHEN expires_at > ? THEN 1
                        ELSE 0
                    END
                WHERE user_id = ?
            """, (
                now,
                now + REMINDER_INTERVAL,
                now,
                user_id,
            ))


def record_delivery_failure(user_id, blocked=False):
    now = time.time()

    with connect_db() as db:
        db.execute("""
            UPDATE reminder_schedule
            SET claim_until = NULL,
                next_send_at = ?,
                active = CASE WHEN ? = 1 THEN 0 ELSE active END
            WHERE user_id = ?
        """, (now + RETRY_DELAY, int(blocked), user_id))

        if blocked:
            db.execute("""
                UPDATE users SET blocked = 1 WHERE user_id = ?
            """, (user_id,))


# --------------------------------------------------
# TELEGRAM COMMANDS
# --------------------------------------------------

WELCOME_TEXT = (
    "Welcome to Lemvest Trading Lab.\n\n"
    "Thank you for connecting with us. Join our Telegram "
    "channel to receive our updates and information.\n\n"
    "Your reminders are scheduled every 6 hours for up to "
    "15 days. You can stop them at any time with /stop."
)

REMINDER_TEXT = (
    "Lemvest Trading Lab reminder\n\n"
    "Visit our Telegram channel to keep up with our updates."
)


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    register_user(user)
    created = start_campaign(user.id)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "Join Lemvest Trading Lab",
            url=CHANNEL_LINK,
        )
    ]])

    try:
        if IMAGE_PATH.is_file():
            with IMAGE_PATH.open("rb") as image:
                await message.reply_photo(
                    photo=image,
                    caption=WELCOME_TEXT,
                    reply_markup=keyboard,
                )
        else:
            logger.warning("welcome.jpg was not found at %s", IMAGE_PATH)
            await message.reply_text(
                WELCOME_TEXT,
                reply_markup=keyboard,
            )
    except (TelegramError, OSError):
        logger.exception("Could not send welcome image")
        await message.reply_text(
            WELCOME_TEXT,
            reply_markup=keyboard,
        )

    if not created:
        await message.reply_text(
            "Your reminders are already active. "
            "Your existing schedule has not been reset.\n\n"
            "Use /help to see the available commands."
        )
    else:
        await message.reply_text(
            "Your reminders are now active. "
            "Your first reminder will arrive in 6 hours."
        )

    logger.info("Processed /start for user_id=%s", user.id)


async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    stop_campaign(user.id)
    await message.reply_text(
        "Your reminders have been stopped. "
        "You can send /start if you want to start them again."
    )
    logger.info("Stopped reminders for user_id=%s", user.id)


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if message:
        await message.reply_text(
            "Lemvest Trading Lab Bot\n\n"
            "/start — Show welcome message and enable reminders\n"
            "/stop — Stop reminders\n"
            "/help — Show help\n"
            "/users — Admin-only user statistics\n"
            "/diag — Check whether the bot is responding\n\n"
            "The first reminder is sent after 6 hours, then "
            "every 6 hours for up to 15 days."
        )


async def users_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user
    message = update.effective_message

    if not user or not message:
        return

    if not ADMIN_USER_ID or str(user.id) != ADMIN_USER_ID:
        await message.reply_text("You are not authorized to use this command.")
        return

    total, active = get_user_counts()

    await message.reply_text(
        "Lemvest Trading Lab Statistics\n\n"
        f"Registered bot users: {total}\n"
        f"Users with active reminders: {active}\n\n"
        "Registered bot users are people who have started "
        "the bot, not necessarily channel members."
    )


async def diag_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if message:
        logger.info(
            "DIAGNOSTIC: received command from user_id=%s",
            user.id if user else None,
        )
        await message.reply_text(
            "The bot is online and receiving commands."
        )


# --------------------------------------------------
# REMINDER WORKER
# --------------------------------------------------

async def reminder_loop(application: Application):
    logger.info("Reminder worker started")

    while True:
        user_id = None

        try:
            user_id = claim_due_reminder()

            if user_id is None:
                await asyncio.sleep(CHECK_INTERVAL)
                continue

            try:
                await application.bot.send_message(
                    chat_id=user_id,
                    text=REMINDER_TEXT,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "Visit Lemvest Trading Lab",
                            url=CHANNEL_LINK,
                        )
                    ]]),
                )
            except TelegramError as exc:
                logger.warning(
                    "Reminder failed for user_id=%s: %s",
                    user_id,
                    exc,
                )

                error_text = str(exc).lower()
                blocked = (
                    "bot was blocked by the user" in error_text
                    or "user is deactivated" in error_text
                    or "chat not found" in error_text
                )

                record_delivery_failure(user_id, blocked=blocked)
            else:
                record_delivery(user_id)
                logger.info(
                    "Reminder sent successfully to user_id=%s",
                    user_id,
                )

        except asyncio.CancelledError:
            logger.info("Reminder worker stopped")
            raise
        except Exception:
            logger.exception("Unexpected error in reminder worker")

            if user_id is not None:
                try:
                    record_delivery_failure(user_id)
                except Exception:
                    logger.exception("Could not release failed reminder")

            await asyncio.sleep(CHECK_INTERVAL)


async def post_init(application: Application):
    initialize_database()

    task = asyncio.create_task(reminder_loop(application))
    application.bot_data["reminder_task"] = task

    logger.info("Lemvest Trading Lab bot started successfully")


async def post_stop(application: Application):
    task = application.bot_data.get("reminder_task")

    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    logger.error(
        "Telegram handler error",
        exc_info=context.error,
    )


# --------------------------------------------------
# START APPLICATION
# --------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing from environment variables"
        )

    initialize_database()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("diag", diag_command))
    application.add_error_handler(error_handler)

    logger.info("Starting Lemvest Trading Lab bot")
    application.run_polling()


if __name__ == "__main__":
    main()
