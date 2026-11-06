import asyncio
import logging
import os
import sqlite3
import time
from contextlib import closing, suppress
from pathlib import Path

import qrcode
import barcode
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageFont
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
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
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("mode_switch_bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "").strip()
CHANNEL_LINK = os.getenv(
    "CHANNEL_LINK",
    "https://t.me/+CaYjuNbPEFA2NWU0",
).strip()
WELCOME_IMAGE = Path(
    os.getenv("WELCOME_IMAGE_PATH", str(BASE_DIR / "welcome.jpg"))
).expanduser()
DB_FILE = os.getenv("DB_FILE", str(BASE_DIR / "users.db")).strip()

MODE_REDIRECT = "REDIRECT"
MODE_SOFTWARE = "REVERSE"
DEFAULT_MODE = MODE_REDIRECT

WELCOME_TEXT = os.getenv(
    "WELCOME_TEXT",
    "Welcome to Lemvest Trading Lab!\n\n"
    "Get updates, information, and useful resources from our channel.\n\n"
    "Tap Join Channel below to visit us.",
)
PROMO_TEXT = os.getenv(
    "PROMO_TEXT",
    "Welcome to Lemvest Trading Lab!\n\n"
    "Stay connected with our latest updates, information, and resources.\n\n"
    "Tap the button below to visit our Telegram channel.",
)

REMINDER_INTERVAL = 6 * 60 * 60
CAMPAIGN_DURATION = 15 * 24 * 60 * 60
CHECK_INTERVAL = 60
SOFTWARE_MENU_TEXT = (
    "Welcome to the Software Tools Bot.\n\n"
    "Choose a tool below:\n"
    "• Logo Maker\n"
    "• QR Code Generator\n"
    "• Barcode Generator"
)

# ============================================================
# DATABASE: users, current mode, and opt-in reminders
# ============================================================

def connect_db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db():
    Path(DB_FILE).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    with closing(connect_db()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at REAL NOT NULL
            )
        """)



