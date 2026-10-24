import asyncio
import logging
import os
import sqlite3
import time
import uuid
from contextlib import closing, suppress
from datetime import timedelta
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes

# -------------------- Configuration --------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("lemvest_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent
IMAGE_PATH = BASE_DIR / "welcome.jpg"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "").strip()
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/+CaYjuNbPEFA2NWU0").strip()
DB_FILE = os.getenv("DB_FILE", str(BASE_DIR / "users.db")).strip() or str(BASE_DIR / "users.db")

REMINDER_INTERVAL = 6 * 60 * 60       # six hours
CAMPAIGN_DURATION = 15 * 24 * 60 * 60  # fifteen days
CHECK_INTERVAL = 60                    # check schedule every minute; does NOT send every minute
BATCH_SIZE = 50
SEND_DELAY = 1
RETRY_DELAY = 15 * 60
WORKER_ID = str(uuid.uuid4())
WORKER_LEASE_SECONDS = 180

WELCOME_TEXT = (
    "Welcome to Lemvest Trading Lab!\n\n"
    "Here, you'll find trading insights, market analysis, educational content, "
    "and practical tools to help you better understand the markets.\n\n"
    "Tap the button below to visit our Telegram channel.\n\n"
    "You'll receive updates every six hours for up to 15 days. "
    "Send /stop at any time to stop reminders."
)
PROMO_TEXT = (
    "Welcome to Lemvest Trading Lab!\n\n"
    "Explore our trading insights, market analysis, educational content, "
    "and practical tools.\n\n"
    "Tap the button below to visit our channel.\n\n"
    "To stop these reminders at any time, send /stop."
)

# -------------------- Database --------------------

def connect_db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    Path(DB_FILE).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    with closing(connect_db()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reminder_schedule (
                user_id INTEGER PRIMARY KEY,
                started_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                next_send_at REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                last_sent_at REAL,
                claim_until REAL
            )
        """)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(reminder_schedule)")}
        if "last_sent_at" not in columns:
            conn.execute("ALTER TABLE reminder_schedule ADD COLUMN last_sent_at REAL")
        if "claim_until" not in columns:
            conn.execute("ALTER TABLE reminder_schedule ADD COLUMN claim_until REAL")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_reminder_due
            ON reminder_schedule(active, next_send_at, expires_at)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS worker_lock (
                lock_name TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                lease_until REAL NOT NULL
            )
        """)
