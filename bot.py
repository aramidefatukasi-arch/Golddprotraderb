
import asyncio
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, TelegramError
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

DATA_DIR = Path("/var/data")
PERSISTENT_DB = DATA_DIR / "users.db"
LEGACY_DB = BASE_DIR / "users.db"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

CHANNEL_LINK = os.getenv(
    "CHANNEL_LINK",
    "https://t.me/+CaYjuNbPEFA2NWU0",
).strip()

REMINDER_INTERVAL = 6 * 60 * 60
CAMPAIGN_DURATION = 15 * 24 * 60 * 60
CLAIM_DURATION = 5 * 60
RETRY_DELAY = 5 * 60
REMINDER_CHECK_INTERVAL = 30

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DB_PATH = None


# --------------------------------------------------
# DATABASE
# --------------------------------------------------

def configure_database():
    """Use the Render persistent disk for the SQLite database."""
    global DB_PATH

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # Respect an explicitly configured database path.
    configured_path = os.getenv("DB_FILE", "").strip()

    if configured_path:
        DB_PATH = Path(configured_path)
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    else:
        DB_PATH = PERSISTENT_DB

    # Copy a legacy database only if the destination does not exist.
    # Never overwrite an existing persistent database.
    if not DB_PATH.exists():
        candidates = [
            LEGACY_DB,
            BASE_DIR / "data" / "users.db",
            Path.cwd() / "users.db",
        ]

        for candidate in candidates:
            try:
                if candidate.resolve() == DB_PATH.resolve():
                    continue

                if candidate.is_file():
                    shutil.copy2(candidate, DB_PATH)
                    logger.info(
                        "Copied existing database from %s to %s",
                        candidate,
                        DB_PATH,
                    )
                    break
            except OSError:
                logger.exception(
                    "Could not copy legacy database from %s",
                    candidate,
                )

    logger.info("Database path: %s", DB_PATH)


def get_db_connection():
    if DB_PATH is None:
        configure_database()

    connection = sqlite3.connect(str(DB_PATH), timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def initialize_database():
    """Create tables and add columns needed by the reminder system."""
    configure_database()

    with get_db_connection() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                first_seen REAL NOT NULL DEFAULT 0,
                last_seen REAL NOT NULL DEFAULT 0,
                blocked INTEGER NOT NULL DEFAULT 0
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS reminder_schedule (
                user_id INTEGER PRIMARY KEY,
                started_at REAL NOT NULL DEFAULT 0,
                expires_at REAL NOT NULL DEFAULT 0,
                next_send_at REAL NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                claim_until REAL NOT NULL DEFAULT 0,
                last_sent_at REAL NOT NULL DEFAULT 0
            )
        """)

        # Add any missing columns to an older database.
        required_columns = {
            "users": {
                "username": "TEXT",
                "first_name": "TEXT",
                "first_seen": "REAL NOT NULL DEFAULT 0",
                "last_seen": "REAL NOT NULL DEFAULT 0",
                "blocked": "INTEGER NOT NULL DEFAULT 0",
            },
            "reminder_schedule": {
                "started_at": "REAL NOT NULL DEFAULT 0",
                "expires_at": "REAL NOT NULL DEFAULT 0",
                "next_send_at": "REAL NOT NULL DEFAULT 0",
                "active": "INTEGER NOT NULL DEFAULT 1",
                "claim_until": "REAL NOT NULL DEFAULT 0",
                "last_sent_at": "REAL NOT NULL DEFAULT 0",
            },
        }

        for table, columns in required_columns.items():
            existing = {
                row["name"]
                for row in connection.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }

            for column, definition in columns.items():
                if column not in existing:
                    connection.execute(
                        f"ALTER TABLE {table} "
                        f"ADD COLUMN {column} {definition}"
                    )

        # Ensure users with reminder schedules are also registered.
        connection.execute("""
            INSERT OR IGNORE INTO users (
                user_id, first_seen, last_seen, blocked
            )
            SELECT user_id, started_at, started_at, 0
            FROM reminder_schedule
        """)

        # Expire old campaigns and clear stale claims.
        now = time.time()

        connection.execute("""
            UPDATE reminder_schedule
            SET active = 0, claim_until = 0
            WHERE expires_at > 0 AND expires_at <= ?
        """, (now,))

        connection.execute("""
            UPDATE reminder_schedule
            SET claim_until = 0
            WHERE claim_until > 0 AND claim_until <= ?
        """, (now,))

    logger.info("Database initialized successfully")


def register_user(user):
    now = time.time()

    with get_db_connection() as connection:
        connection.execute("""
            INSERT INTO users (
                user_id, username, first_name,
                first_seen, last_seen, blocked
            )
            VALUES (?, ?, ?, ?, ?, 0)
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


def start_reminders(user_id):
    """Start a campaign only if the user has no active campaign."""
    now = time.time()

    with get_db_connection() as connection:
        existing = connection.execute("""
            SELECT active, expires_at
            FROM reminder_schedule
            WHERE user_id = ?
        """, (user_id,)).fetchone()

        if (
            existing
            and existing["active"]
            and existing["expires_at"] > now
        ):
            return False

        connection.execute("""
            INSERT INTO reminder_schedule (
                user_id, started_at, expires_at,
                next_send_at, active, claim_until, last_sent_at
            )
            VALUES (?, ?, ?, ?, 1, 0, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                started_at = excluded.started_at,
                expires_at = excluded.expires_at,
                next_send_at = excluded.next_send_at,
                active = 1,
                claim_until = 0,
                last_sent_at = 0
        """, (
            user_id,
            now,
            now + CAMPAIGN_DURATION,
            now + REMINDER_INTERVAL,
        ))

    return True


def stop_reminders(user_id):
    with get_db_connection() as connection:
        cursor = connection.execute("""
            UPDATE reminder_schedule
            SET active = 0, claim_until = 0
            WHERE user_id = ? AND active = 1
        """, (user_id,))

    return cursor.rowcount > 0


def get_user_statistics():
    now = time.time()

    with get_db_connection() as connection:
        total_users = connection.execute("""
            SELECT COUNT(*) AS total FROM users
        """).fetchone()["total"]

        active_reminders = connection.execute("""
            SELECT COUNT(*) AS total
            FROM reminder_schedule
            WHERE active = 1 AND expires_at > ?
        """, (now,)).fetchone()["total"]

        blocked_users = connection.execute("""
            SELECT COUNT(*) AS total
            FROM users
            WHERE blocked = 1
        """).fetchone()["total"]

    return total_users, active_reminders, blocked_users


def claim_due_reminder():
    """Claim one due reminder so it isn't sent twice concurrently."""
    now = time.time()

    with get_db_connection() as connection:
        connection.execute("""
            UPDATE reminder_schedule
            SET active = 0, claim_until = 0
            WHERE active = 1
              AND expires_at > 0
              AND expires_at <= ?
        """, (now,))

        row = connection.execute("""
            SELECT user_id, expires_at
            FROM reminder_schedule
            WHERE active = 1
              AND next_send_at <= ?
              AND expires_at > ?
              AND (claim_until = 0 OR claim_until <= ?)
            ORDER BY next_send_at ASC
            LIMIT 1
        """, (now, now, now)).fetchone()

        if row is None:
            return None

        cursor = connection.execute("""
            UPDATE reminder_schedule
            SET claim_until = ?
            WHERE user_id = ?
              AND active = 1
              AND (claim_until = 0 OR claim_until <= ?)
        """, (
            now + CLAIM_DURATION,
            row["user_id"],
            now,
        ))

        if cursor.rowcount != 1:
            return None

        return row["user_id"], row["expires_at"]


def finish_reminder(user_id, success, blocked=False):
    now = time.time()

    with get_db_connection() as connection:
        if blocked:
            connection.execute("""
                UPDATE users SET blocked = 1
                WHERE user_id = ?
            """, (user_id,))

            connection.execute("""
                UPDATE reminder_schedule
                SET active = 0, claim_until = 0
                WHERE user_id = ?
            """, (user_id,))
            return

        if success:
            connection.execute("""
                UPDATE reminder_schedule
                SET last_sent_at = ?,
                    next_send_at = ?,
                    claim_until = 0,
                    active = CASE
                        WHEN expires_at <= ? THEN 0
                        ELSE active
                    END
                WHERE user_id = ?
            """, (
                now,
                now + REMINDER_INTERVAL,
                now,
                user_id,
            ))
        else:
            connection.execute("""
                UPDATE reminder_schedule
                SET next_send_at = ?,
                    claim_until = 0
                WHERE user_id = ?
            """, (
                now + RETRY_DELAY,
                user_id,
            ))


# --------------------------------------------------
# COMMANDS
# --------------------------------------------------

def is_admin(user_id):
    return ADMIN_USER_ID != 0 and user_id == ADMIN_USER_ID


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.effective_chat:
        return

    user = update.effective_user
    register_user(user)

    newly_started = start_reminders(user.id)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Join Lemvest Trading Lab", url=CHANNEL_LINK)]
    ])

    welcome_text = (
        "Welcome to Lemvest Trading Lab.\n\n"
        "Join our channel to stay connected.\n\n"
        "Your first reminder will arrive after 6 hours, "
        "then every 6 hours for up to 15 days. "
        "You can stop reminders anytime with /stop."
    )

    try:
        if IMAGE_PATH.is_file():
            with IMAGE_PATH.open("rb") as image_file:
                await update.effective_chat.send_photo(
                    photo=image_file,
                    caption=welcome_text,
                    reply_markup=keyboard,
                )
        else:
            await update.effective_chat.send_message(
                text=welcome_text,
                reply_markup=keyboard,
            )
    except TelegramError:
        logger.exception("Could not send the welcome message")
        await update.effective_chat.send_message(welcome_text)

    if not newly_started:
        await update.effective_chat.send_message(
            "Your reminders are already active. "
            "Your existing schedule has not been reset."
        )


async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.effective_message:
        return

    user_id = update.effective_user.id
    register_user(update.effective_user)

    if stop_reminders(user_id):
        message = "Your reminders have been stopped."
    else:
        message = "You don't have any active reminders."

    await update.effective_message.reply_text(message)


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.effective_message:
        return

    user_id = update.effective_user.id

    if is_admin(user_id):
        help_text = (
            "Lemvest Trading Lab Bot\n\n"
            "/start — Show welcome message and enable reminders\n"
            "/stop — Stop reminders\n"
            "/help — Show help\n"
            "/users — Admin-only user statistics\n"
            "/diag — Check whether the bot is responding\n\n"
            "The first reminder is sent after 6 hours, then every "
            "6 hours for up to 15 days."
        )
    else:
        help_text = (
            "Lemvest Trading Lab Bot\n\n"
            "/start — Show welcome message and enable reminders\n"
            "/stop — Stop reminders\n\n"
            "The first reminder is sent after 6 hours, then every "
            "6 hours for up to 15 days."
        )

    await update.effective_message.reply_text(help_text)


async def users_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.effective_message:
        return

    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text(
            "This command is available to the bot administrator only."
        )
        return

    total, active, blocked = get_user_statistics()

    await update.effective_message.reply_text(
        "Lemvest Trading Lab Bot Statistics\n\n"
        f"Registered bot users: {total}\n"
        f"Active reminder campaigns: {active}\n"
        f"Users who blocked the bot: {blocked}"
    )


async def diag_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not update.effective_user or not update.effective_message:
        return

    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text(
            "This command is available to the bot administrator only."
        )
        return

    try:
        with get_db_connection() as connection:
            connection.execute("SELECT 1").fetchone()

        await update.effective_message.reply_text(
            "Bot status: responding\n"
            "Database status: connected\n"
            f"Database path: {DB_PATH}"
        )
    except Exception:
        logger.exception("Database diagnostic failed")
        await update.effective_message.reply_text(
            "The bot is responding, but the database check failed. "
            "Please check the Render logs."
        )


# --------------------------------------------------
# REMINDER LOOP
# --------------------------------------------------

async def reminder_loop(application: Application):
    logger.info("Reminder loop started")

    while True:
        try:
            claimed = claim_due_reminder()

            if claimed is None:
                await asyncio.sleep(REMINDER_CHECK_INTERVAL)
                continue

            user_id, expires_at = claimed

            # Do not send if the campaign has expired.
            if time.time() >= expires_at:
                finish_reminder(user_id, success=False)
                continue

            reminder_text = (
                "This is your scheduled reminder from "
                "Lemvest Trading Lab.\n\n"
                "Stay connected with the channel for updates."
            )

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "Visit Lemvest Trading Lab",
                        url=CHANNEL_LINK,
                    )
                ]
            ])

            try:
                await application.bot.send_message(
                    chat_id=user_id,
                    text=reminder_text,
                    reply_markup=keyboard,
                )
                finish_reminder(user_id, success=True)
                logger.info("Reminder sent to user %s", user_id)

            except Forbidden:
                logger.info(
                    "User %s blocked the bot or cannot receive messages",
                    user_id,
                )
                finish_reminder(
                    user_id,
                    success=False,
                    blocked=True,
                )

            except TelegramError:
                logger.exception(
                    "Failed to send reminder to user %s",
                    user_id,
                )
                finish_reminder(user_id, success=False)

        except asyncio.CancelledError:
            logger.info("Reminder loop cancelled")
            raise

        except Exception:
            logger.exception("Unexpected error in reminder loop")
            await asyncio.sleep(REMINDER_CHECK_INTERVAL)


async def post_init(application: Application):
    initialize_database()

    application.bot_data["reminder_task"] = asyncio.create_task(
        reminder_loop(application)
    )

    logger.info("Lemvest Trading Lab bot started successfully")


async def post_stop(application: Application):
    task = application.bot_data.get("reminder_task")

    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# --------------------------------------------------
# START APPLICATION
# --------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is missing from the Render environment."
        )

    if ADMIN_USER_ID == 0:
        logger.warning(
            "ADMIN_USER_ID is not configured. Admin commands will be denied."
        )

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

    logger.info("Starting Telegram polling")
    application.run_polling()


if __name__ == "__main__":
    main()
