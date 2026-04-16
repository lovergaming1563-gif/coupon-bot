import os
import json
import sqlite3
import logging
import threading
import html
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from flask import Flask, redirect, request as flask_request
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

# ─────────────── Logging ───────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(), logging.FileHandler("bot.log")],
)
logger = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)   # silence Flask access logs

# ─────────────── Config ───────────────
BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID   = int(os.environ.get("TELEGRAM_ADMIN_ID", "6724474397"))

UPI_ID         = "BHARATPE.8B0L1T2H8C56136@fbpe"  # UPI ID
SUPPORT_HANDLE = "@MyntraCouponsupport_bot"  # Support Telegram handle
QR_IMAGE_PATH  = os.path.join(os.path.dirname(__file__), "qr_code.jpg")

# ─────────────── Referral & Channel Config ───────────────
CHANNEL_USERNAME    = "@withoutanyinvestmentwork"
CHANNEL_LINK        = "https://t.me/withoutanyinvestmentwork"
REFERRAL_GOAL       = 5                          # referrals needed for free coupon
REFERRAL_REWARD_KEY = "coupon_bigbasket_150"     # free coupon product key

# ── Referral redirect base URL (set env var REFERRAL_BASE_URL to enable IP tracking) ──
# Example: https://yourapp.replit.app/api-server
# Leave empty → direct t.me link (no IP check)
REFERRAL_BASE_URL   = os.environ.get("REFERRAL_BASE_URL", "").rstrip("/")
BOT_USERNAME        = os.environ.get("BOT_USERNAME", "")   # e.g. MyntraCouponBot

# ─────────────── Multi-Channel Config ───────────────
# Users must join ALL of these channels before using the bot.
# For private channels (t.me/+xxx), membership cannot be verified via API.
# We show join buttons and trust the user joined.
REQUIRED_CHANNELS = [
    {"name": "Channel 1", "link": "https://t.me/+_diGJbgVkeQzNzFl",         "username": None},
    {"name": "Channel 2", "link": "https://t.me/+OnJsUcXtvoRkMjg9",         "username": None},
    {"name": "Channel 3", "link": "https://t.me/withoutanyinvestmentwork",   "username": "@withoutanyinvestmentwork"},
]

# ─────────────── SQLite Referral DB ───────────────
_DEFAULT_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
REFERRAL_DB = os.path.join(
    os.environ.get("BOT_DATA_DIR", _DEFAULT_DATA_DIR), "referrals.db"
)

def init_referral_db():
    con = sqlite3.connect(REFERRAL_DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            user_id          TEXT PRIMARY KEY,
            referred_by      TEXT NOT NULL,
            reward_given     INTEGER NOT NULL DEFAULT 0,
            joined_at        TEXT,
            referral_status  TEXT NOT NULL DEFAULT 'active'
        )
    """)
    # Safe migrations for existing DBs (ignore errors = column already exists)
    for migration in [
        "ALTER TABLE referrals ADD COLUMN referral_status TEXT NOT NULL DEFAULT 'active'",
        "ALTER TABLE referrals ADD COLUMN ip_token TEXT",
    ]:
        try:
            con.execute(migration)
            con.commit()
        except Exception:
            pass
    # Rewards catalogue (admin-managed)
    con.execute("""
        CREATE TABLE IF NOT EXISTS rewards (
            reward_name     TEXT PRIMARY KEY,
            points_required INTEGER NOT NULL DEFAULT 5,
            created_at      TEXT
        )
    """)
    # Coupon codes for each reward
    con.execute("""
        CREATE TABLE IF NOT EXISTS reward_coupons (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            reward_name TEXT NOT NULL,
            code        TEXT NOT NULL UNIQUE,
            used        INTEGER NOT NULL DEFAULT 0,
            used_by     TEXT,
            used_at     TEXT
        )
    """)
    con.commit()
    con.close()

def db_get_referral(user_id: str):
    """Returns (user_id, referred_by, reward_given) or None."""
    con = sqlite3.connect(REFERRAL_DB)
    row = con.execute(
        "SELECT user_id, referred_by, reward_given FROM referrals WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    con.close()
    return row

def db_insert_referral(user_id: str, referred_by: str, token: str = None) -> bool:
    """Insert referral. Returns True if new row inserted."""
    try:
        con = sqlite3.connect(REFERRAL_DB)
        con.execute(
            "INSERT INTO referrals (user_id, referred_by, reward_given, joined_at, ip_token) VALUES (?,?,0,?,?)",
            (user_id, referred_by, datetime.now().isoformat(), token)
        )
        con.commit()
        con.close()
        return True
    except sqlite3.IntegrityError:
        return False

def db_get_referral_token(user_id: str) -> str | None:
    """Get the IP token stored for this referred user (may be None if old link used)."""
    con = sqlite3.connect(REFERRAL_DB)
    row = con.execute("SELECT ip_token FROM referrals WHERE user_id = ?", (user_id,)).fetchone()
    con.close()
    return row[0] if row else None

def db_mark_reward_given(user_id: str):
    con = sqlite3.connect(REFERRAL_DB)
    con.execute("UPDATE referrals SET reward_given = 1 WHERE user_id = ?", (user_id,))
    con.commit()
    con.close()

def db_successful_referral_count(referrer_id: str) -> int:
    """Count of ACTIVE verified referrals (reward_given=1, status=active) — this is the points value."""
    con = sqlite3.connect(REFERRAL_DB)
    count = con.execute(
        "SELECT COUNT(*) FROM referrals WHERE referred_by = ? AND reward_given = 1 AND referral_status = 'active'",
        (referrer_id,)
    ).fetchone()[0]
    con.close()
    return count

def db_set_referral_status(user_id: str, status: str):
    """Set referral_status = 'active' or 'removed' for the referred user."""
    con = sqlite3.connect(REFERRAL_DB)
    con.execute(
        "UPDATE referrals SET referral_status = ? WHERE user_id = ?",
        (status, user_id)
    )
    con.commit()
    con.close()

def db_get_all_verified_referrals() -> list:
    """Return all verified referrals: [(user_id, referred_by, referral_status), ...]"""
    con = sqlite3.connect(REFERRAL_DB)
    rows = con.execute(
        "SELECT user_id, referred_by, referral_status FROM referrals WHERE reward_given = 1"
    ).fetchall()
    con.close()
    return rows

def db_total_referral_count(referrer_id: str) -> int:
    """Count of all referrals (pending + verified) for this referrer."""
    con = sqlite3.connect(REFERRAL_DB)
    count = con.execute(
        "SELECT COUNT(*) FROM referrals WHERE referred_by = ?",
        (referrer_id,)
    ).fetchone()[0]
    con.close()
    return count

def db_get_referral_leaderboard() -> list:
    """Return list of (referrer_id, active_count, total_count) sorted by active_count desc."""
    con = sqlite3.connect(REFERRAL_DB)
    rows = con.execute("""
        SELECT
            referred_by,
            SUM(CASE WHEN reward_given=1 AND referral_status='active' THEN 1 ELSE 0 END) AS active,
            COUNT(*) AS total
        FROM referrals
        GROUP BY referred_by
        ORDER BY active DESC, total DESC
    """).fetchall()
    con.close()
    return rows  # list of (referrer_id, active, total)

def db_get_referred_users_detail(referrer_id: str) -> list:
    """Return rows of (user_id, reward_given, referral_status, joined_at) for a referrer."""
    con = sqlite3.connect(REFERRAL_DB)
    rows = con.execute("""
        SELECT user_id, reward_given, referral_status, joined_at
        FROM referrals
        WHERE referred_by = ?
        ORDER BY joined_at DESC
    """, (referrer_id,)).fetchall()
    con.close()
    return rows

# ── IP tracking helpers (shared file with api-server) ──
_IP_FILE = os.path.join(os.environ.get("BOT_DATA_DIR", _DEFAULT_DATA_DIR), "referral_ips.json")

def _load_ip_data() -> dict:
    try:
        if os.path.exists(_IP_FILE):
            with open(_IP_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"tokens": {}, "used_ips": []}

def _save_ip_data(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_IP_FILE), exist_ok=True)
        with open(_IP_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error(f"_save_ip_data error: {e}")

def check_referral_ip(token: str) -> tuple:
    """Returns (ip, is_duplicate). is_duplicate=True means IP already used for referral."""
    data = _load_ip_data()
    entry = data.get("tokens", {}).get(token)
    if not entry:
        return None, False
    ip = entry.get("ip", "unknown")
    is_dup = ip in data.get("used_ips", [])
    return ip, is_dup

def claim_referral_ip(token: str, ip: str) -> None:
    """Mark IP as used + token as claimed."""
    data = _load_ip_data()
    if ip and ip not in data.setdefault("used_ips", []):
        data["used_ips"].append(ip)
    if token in data.get("tokens", {}):
        data["tokens"][token]["claimed"] = True
    _save_ip_data(data)

# ── Points = earned referrals MINUS spent points ──
def db_get_points(user_id: str) -> int:
    earned = db_successful_referral_count(user_id)
    try:
        con = sqlite3.connect(REFERRAL_DB)
        con.execute("""CREATE TABLE IF NOT EXISTS points_spent (
            user_id TEXT, points INTEGER, reward_name TEXT, spent_at TEXT
        )""")
        spent = con.execute(
            "SELECT COALESCE(SUM(points),0) FROM points_spent WHERE user_id=?", (user_id,)
        ).fetchone()[0] or 0
        con.close()
    except Exception:
        spent = 0
    return max(0, earned - spent)


def db_deduct_points(user_id: str, points: int, reason: str = "admin_deduction") -> None:
    """Admin: manually deduct points from a user."""
    con = sqlite3.connect(REFERRAL_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS points_spent (
        user_id TEXT, points INTEGER, reward_name TEXT, spent_at TEXT
    )""")
    con.execute(
        "INSERT INTO points_spent (user_id, points, reward_name, spent_at) VALUES (?,?,?,?)",
        (user_id, points, reason, datetime.now().isoformat())
    )
    con.commit()
    con.close()


def db_delete_reward(reward_name: str) -> bool:
    """Admin: delete a reward and all its unclaimed coupons."""
    con = sqlite3.connect(REFERRAL_DB)
    con.execute("DELETE FROM rewards WHERE reward_name=?", (reward_name,))
    con.execute("DELETE FROM reward_coupons WHERE reward_name=? AND used=0", (reward_name,))
    con.commit()
    deleted = con.total_changes > 0
    con.close()
    return deleted

# ── Rewards CRUD ──
def db_add_reward(reward_name: str, points_required: int) -> bool:
    try:
        con = sqlite3.connect(REFERRAL_DB)
        con.execute(
            "INSERT OR REPLACE INTO rewards (reward_name, points_required, created_at) VALUES (?,?,?)",
            (reward_name, points_required, datetime.now().isoformat())
        )
        con.commit()
        con.close()
        return True
    except Exception:
        return False

def db_list_rewards() -> list:
    """Returns list of (reward_name, points_required, stock) sorted by points_required."""
    con = sqlite3.connect(REFERRAL_DB)
    rows = con.execute("SELECT reward_name, points_required FROM rewards ORDER BY points_required").fetchall()
    result = []
    for name, pts in rows:
        stock = con.execute(
            "SELECT COUNT(*) FROM reward_coupons WHERE reward_name=? AND used=0", (name,)
        ).fetchone()[0]
        result.append({"name": name, "points": pts, "stock": stock})
    con.close()
    return result

def db_add_reward_coupon(reward_name: str, code: str) -> bool:
    try:
        con = sqlite3.connect(REFERRAL_DB)
        con.execute(
            "INSERT INTO reward_coupons (reward_name, code) VALUES (?,?)",
            (reward_name, code)
        )
        con.commit()
        con.close()
        return True
    except sqlite3.IntegrityError:
        return False

def db_delete_reward_coupon(reward_name: str, code: str) -> bool:
    """Admin: delete a specific coupon code from a reward pool."""
    con = sqlite3.connect(REFERRAL_DB)
    cur = con.execute(
        "DELETE FROM reward_coupons WHERE reward_name=? AND code=? AND used=0",
        (reward_name, code)
    )
    con.commit()
    deleted = cur.rowcount > 0
    con.close()
    return deleted

def db_list_reward_coupons(reward_name: str) -> list:
    """Admin: list all unused coupon codes for a reward."""
    con = sqlite3.connect(REFERRAL_DB)
    rows = con.execute(
        "SELECT code FROM reward_coupons WHERE reward_name=? AND used=0 ORDER BY id",
        (reward_name,)
    ).fetchall()
    con.close()
    return [r[0] for r in rows]

def db_redeem_reward(user_id: str, reward_name: str) -> str | None:
    """Redeem 1 coupon for the user. Returns the code or None if no stock."""
    con = sqlite3.connect(REFERRAL_DB)
    row = con.execute(
        "SELECT id, code FROM reward_coupons WHERE reward_name=? AND used=0 ORDER BY id LIMIT 1",
        (reward_name,)
    ).fetchone()
    if not row:
        con.close()
        return None
    con.execute(
        "UPDATE reward_coupons SET used=1, used_by=?, used_at=? WHERE id=?",
        (user_id, datetime.now().isoformat(), row[0])
    )
    con.commit()
    con.close()
    return row[1]

# Data files stored inside workspace/telegram-bot/data/ — persists across restarts
_DATA_DIR = os.environ.get("BOT_DATA_DIR", _DEFAULT_DATA_DIR)
os.makedirs(_DATA_DIR, exist_ok=True)

# One-time migration: if data exists in old telegram-bot/ location, copy it here
_OLD_DIR = os.path.dirname(os.path.abspath(__file__))
if _OLD_DIR != _DATA_DIR:
    import shutil
    for _f in ["coupons.json", "users.json", "orders.json", "pending_orders.json"]:
        _src = os.path.join(_OLD_DIR, _f)
        _dst = os.path.join(_DATA_DIR, _f)
        if os.path.exists(_src) and not os.path.exists(_dst):
            shutil.copy2(_src, _dst)

COUPONS_FILE = os.path.join(_DATA_DIR, "coupons.json")
USERS_FILE   = os.path.join(_DATA_DIR, "users.json")
ORDERS_FILE  = os.path.join(_DATA_DIR, "orders.json")
PENDING_FILE = os.path.join(_DATA_DIR, "pending_orders.json")

ORDER_TIMEOUT_SECONDS  = 300   # 5 min auto-cancel
EXIT_TRAP_SECONDS      = 180   # 3 min urgency nudge
FAST_PAYMENT_THRESHOLD = 120   # < 2 min = 🔥 fast
LOW_STOCK_THRESHOLD    = 5

# ─────────────── Products Config (dynamic — admin can edit via /set_name etc) ───────────────

PRODUCTS_FILE = os.path.join(_DATA_DIR, "products_config.json")

_DEFAULT_PRODUCTS: dict = {
    "bigbasket":  {"name": "BigBasket ₹150 Cashback",  "price": 15, "emoji": "🛒", "desc": "₹150 cashback on orders above ₹149"},
    "myntra_199": {"name": "Myntra ₹100 OFF (199)",     "price": 35, "emoji": "🟢", "desc": "₹100 off on orders above ₹199"},
    "myntra_399": {"name": "Myntra ₹100 OFF (399)",     "price": 35, "emoji": "🔵", "desc": "₹100 off on orders above ₹399"},
    "myntra_499": {"name": "Myntra ₹100 OFF (499)",     "price": 35, "emoji": "💛", "desc": "₹100 off on orders above ₹499"},
    "myntra_649": {"name": "Myntra ₹150 OFF (649)",     "price": 35, "emoji": "🟣", "desc": "₹150 off on orders above ₹649"},
    "combo":      {"name": "Myntra Combo (199+399)",    "price": 60, "emoji": "🎁", "desc": "₹100 OFF (199) + ₹100 OFF (399) — 2 codes"},
    "combo2":     {"name": "Myntra Combo (100+150)",    "price": 60, "emoji": "💝", "desc": "₹100 OFF on ₹199+ & ₹150 OFF on ₹649+ — 2 premium codes"},
    "chatgpt":    {"name": "ChatGPT 1 Month",           "price": 49, "emoji": "🤖", "desc": "1 month ChatGPT subscription"},
    "youtube":    {"name": "YouTube 1 Month",           "price": 30, "emoji": "▶️", "desc": "1 month YouTube Premium"},
    # Legacy keys — backward compat for existing orders (hidden from store)
    "coupon_100":           {"name": "₹100 Myntra Coupon",      "price": 35, "emoji": "🟢", "desc": "", "hidden": True},
    "coupon_150":           {"name": "₹150 Myntra Coupon",      "price": 30, "emoji": "🔵", "desc": "", "hidden": True},
    "coupon_bigbasket_150": {"name": "₹150 BigBasket Cashback", "price": 30, "emoji": "🛒", "desc": "", "hidden": True},
}

# Order in which products appear in the store (dynamic — loaded from file)
STORE_PRODUCT_ORDER: list = []  # filled after _load_product_order_from_file() is defined

# Combo product → sub-products whose stock it draws from
COMBO_PARTS: dict = {
    "combo":  ["myntra_199", "myntra_399"],
    "combo2": ["myntra_199", "myntra_649"],
}


def _load_products_from_file() -> dict:
    """Load products config from file; merge with defaults for any missing keys."""
    try:
        data = json.load(open(PRODUCTS_FILE, encoding="utf-8"))
        if data:
            merged = dict(_DEFAULT_PRODUCTS)
            merged.update({k: v for k, v in data.items() if k != "__order__"})
            return merged
    except Exception:
        pass
    return dict(_DEFAULT_PRODUCTS)


def _load_product_order_from_file() -> list:
    """Load STORE_PRODUCT_ORDER from file, fallback to default."""
    try:
        data = json.load(open(PRODUCTS_FILE, encoding="utf-8"))
        if "__order__" in data:
            return data["__order__"]
    except Exception:
        pass
    return [
        "bigbasket", "myntra_199", "myntra_399", "myntra_499",
        "myntra_649", "combo", "combo2", "chatgpt", "youtube",
    ]


PRODUCTS: dict = _load_products_from_file()
STORE_PRODUCT_ORDER = _load_product_order_from_file()


def save_products_config() -> None:
    """Persist current PRODUCTS dict + order to file."""
    data = dict(PRODUCTS)
    data["__order__"] = list(STORE_PRODUCT_ORDER)
    _save(PRODUCTS_FILE, data)


def get_unit_price(product_key: str, quantity: int) -> int:
    """Returns unit price. Uses flash sale price if active."""
    if product_key == "coupon_100":
        if quantity >= 20:
            return 30
        elif quantity >= 10:
            return 32
    sale = _get_active_flash_sale(product_key)
    if sale:
        return sale["sale_price"]
    return PRODUCTS.get(product_key, {}).get("price", 0)


# ─────────────── Flash Sale ───────────────

import time as _time_mod

FLASH_SALES: dict = {}   # {product_key: {sale_price, original_price, expires_at}}


def _get_active_flash_sale(pk: str) -> dict | None:
    """Return flash sale dict if active, else None."""
    sale = FLASH_SALES.get(pk)
    if sale and sale["expires_at"] > _time_mod.time():
        return sale
    if pk in FLASH_SALES:
        del FLASH_SALES[pk]
    return None


def _parse_duration(s: str) -> int | None:
    """Parse duration string like '30m','1h','2h30m' → seconds. Returns None on error."""
    import re as _re
    s = s.strip().lower()
    m = _re.fullmatch(r'(?:(\d+)h)?(?:(\d+)m(?:in)?)?', s)
    if not m or not m.group(0):
        try:
            return int(s) * 60
        except ValueError:
            return None
    hours   = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    secs    = hours * 3600 + minutes * 60
    return secs if secs > 0 else None


def _flash_countdown(expires_at: float) -> str:
    """Human-readable countdown string."""
    left = max(0, int(expires_at - _time_mod.time()))
    h, rem = divmod(left, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m}m baki"
    if m:
        return f"{m}m {s}s baki"
    return f"{s}s baki"


QTY_EMOJIS = {
    1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣",
    6: "6️⃣", 7: "7️⃣", 8: "8️⃣", 9: "9️⃣", 10: "🔟",
}


# ─────────────── Flask keep-alive ───────────────

flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "Bot is running ✅"

@flask_app.route("/api/ref/<uid>")
def referral_redirect(uid):
    """Track referral click IP and redirect to Telegram bot (token in URL)."""
    import secrets as _secrets
    ip = (
        flask_request.headers.get("X-Real-User-IP", "").strip()
        or flask_request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or flask_request.remote_addr
        or "unknown"
    )
    token = f"{uid}_{_secrets.token_hex(8)}"
    data = _load_ip_data()
    data.setdefault("tokens", {})[token] = {"uid": uid, "ip": ip, "claimed": False}
    _save_ip_data(data)
    bot_uname = BOT_USERNAME or "MyntraCouponStores_bot"
    return redirect(f"https://t.me/{bot_uname}?start=ref_{uid}_tk_{token}", code=302)


@flask_app.route("/api/store_ref", methods=["POST"])
def store_ref_ip():
    """Called by Cloudflare Worker: stores IP+token, returns Telegram deep link."""
    import secrets as _secrets
    from flask import jsonify
    try:
        body = flask_request.get_json(force=True) or {}
        uid  = str(body.get("uid", "")).strip()
        ip   = str(body.get("ip", "unknown")).strip() or "unknown"
        if not uid:
            return jsonify({"error": "uid required"}), 400
        token = f"{uid}_{_secrets.token_hex(8)}"
        data  = _load_ip_data()
        data.setdefault("tokens", {})[token] = {"uid": uid, "ip": ip, "claimed": False}
        _save_ip_data(data)
        bot_uname   = BOT_USERNAME or "MyntraCouponStores_bot"
        redirect_url = f"https://t.me/{bot_uname}?start=ref_{uid}_tk_{token}"
        return jsonify({"token": token, "redirect_url": redirect_url})
    except Exception as e:
        logger.error(f"store_ref_ip error: {e}")
        return jsonify({"error": str(e)}), 500

def keep_alive():
    port = int(os.environ.get("PORT", 3000))
    flask_app.run(host="0.0.0.0", port=port)


# ─────────────── Replit DB (persistent KV — survives deployments) ───────────────

_REPLIT_DB_URL = os.environ.get("REPLIT_DB_URL", "")

# Map file path → Replit DB key
_DB_KEYS = {
    "users":   "bot_users_json",
    "coupons": "bot_coupons_json",
    "orders":  "bot_orders_json",
    "pending": "bot_pending_json",
}

def _file_to_db_key(fp: str) -> str:
    for name, key in _DB_KEYS.items():
        if name in fp:
            return key
    return None

def _repldb_set(key: str, value: str) -> None:
    """Store a value in Replit DB."""
    if not _REPLIT_DB_URL:
        return
    try:
        data = urllib.parse.urlencode({key: value}).encode()
        req  = urllib.request.Request(_REPLIT_DB_URL, data=data, method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        logger.error(f"Replit DB set error [{key}]: {e}")

def _repldb_get(key: str) -> str:
    """Retrieve a value from Replit DB. Returns '' if missing."""
    if not _REPLIT_DB_URL:
        return ""
    try:
        url = f"{_REPLIT_DB_URL}/{urllib.parse.quote(key, safe='')}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        logger.error(f"Replit DB get error [{key}]: {e}")
        return ""

_SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "initial_users.json")


def _write_data_file(fp: str, data: dict) -> None:
    """Atomic write helper used during restore."""
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, fp)


def restore_data_from_repldb() -> None:
    """On startup:
    - users.json: ALWAYS merge seed file + Replit DB + local (no user ever lost)
    - other files: restore from Replit DB if local is empty
    """
    # ── USERS: always merge seed + Replit DB + existing local ──
    merged = {}

    # 1. Start with seed file (the 36 known users)
    if os.path.exists(_SEED_FILE):
        try:
            seed = json.load(open(_SEED_FILE, encoding="utf-8"))
            merged.update(seed)
            logger.info(f"Seed file: {len(seed)} users loaded")
        except Exception as e:
            logger.error(f"Seed load error: {e}")

    # 2. Merge Replit DB backup (newer users added after seed)
    raw = _repldb_get(_DB_KEYS["users"])
    if raw:
        try:
            db_data = json.loads(raw)
            merged.update(db_data)  # DB users override seed for same IDs
            logger.info(f"Replit DB: {len(db_data)} users merged")
        except Exception as e:
            logger.error(f"Replit DB merge error: {e}")

    # 3. Merge existing local file (users added since last restart)
    try:
        local = json.load(open(USERS_FILE))
        before = len(merged)
        merged.update(local)
        logger.info(f"Local file: {len(local)} users merged (total now {len(merged)})")
    except Exception:
        pass  # file missing or empty — OK

    # 4. Write merged result
    if merged:
        _write_data_file(USERS_FILE, merged)
        # Push merged data to Replit DB for next deploy
        raw_merged = json.dumps(merged, ensure_ascii=False)
        threading.Thread(target=_repldb_set, args=(_DB_KEYS["users"], raw_merged), daemon=True).start()
        logger.info(f"✅ users.json final: {len(merged)} users")

    # ── OTHER FILES: restore from Replit DB if local is empty ──
    for fp, name in [(COUPONS_FILE, "coupons"), (ORDERS_FILE, "orders"), (PENDING_FILE, "pending")]:
        try:
            existing = json.load(open(fp))
            if existing:
                continue
        except Exception:
            pass

        raw = _repldb_get(_DB_KEYS[name])
        if raw:
            try:
                data = json.loads(raw)
                if data:
                    _write_data_file(fp, data)
                    logger.info(f"✅ Restored {fp} from Replit DB ({len(data)} entries)")
            except Exception as e:
                logger.error(f"Replit DB restore failed for {fp}: {e}")


def backup_data_to_repldb() -> None:
    """On startup: push current local data to Replit DB so next deploy can restore it."""
    mapping = {
        USERS_FILE:   ("users",   {}),
        COUPONS_FILE: ("coupons", {"coupon_100": [], "coupon_150": [], "coupon_bigbasket_150": []}),
        ORDERS_FILE:  ("orders",  {}),
        PENDING_FILE: ("pending", {}),
    }
    for fp, (name, default) in mapping.items():
        try:
            data = json.load(open(fp))
        except Exception:
            data = default
        if data:
            db_key = _DB_KEYS[name]
            raw    = json.dumps(data, ensure_ascii=False)
            _repldb_set(db_key, raw)
            logger.info(f"✅ Backed up {fp} to Replit DB ({len(data)} entries)")


# ─────────────── JSON helpers ───────────────

def load_json(fp: str, default):
    try:
        with open(fp, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(fp: str, data) -> None:
    """Atomic write + Replit DB backup so data survives deployments."""
    # 1. Write locally (atomic)
    tmp = fp + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, fp)
    # 2. Backup to Replit DB in background thread (non-blocking)
    db_key = _file_to_db_key(fp)
    if db_key:
        raw = json.dumps(data, ensure_ascii=False)
        threading.Thread(target=_repldb_set, args=(db_key, raw), daemon=True).start()


def get_coupons()  -> dict:
    default = {k: [] for k in PRODUCTS}
    return load_json(COUPONS_FILE, default)


def save_coupons(d):        _save(COUPONS_FILE, d)
def get_users()    -> dict: return load_json(USERS_FILE,   {})
def save_users(d):          _save(USERS_FILE,   d)
def get_orders()   -> dict: return load_json(ORDERS_FILE,  {})
def save_orders(d):         _save(ORDERS_FILE,  d)
def get_pending()  -> dict: return load_json(PENDING_FILE, {})
def save_pending(d):        _save(PENDING_FILE, d)


# ─────────────── Utilities ───────────────

def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def register_user(user) -> bool:
    """Register user in JSON store. Returns True if newly registered."""
    users = get_users()
    uid   = str(user.id)
    if uid not in users:
        users[uid] = {
            "id":               user.id,
            "username":         user.username,
            "first_name":       user.first_name,
            "joined":           datetime.now().isoformat(),
            "channel_verified": False,
        }
        save_users(users)
        return True
    return False


def is_channel_verified(uid: str) -> bool:
    """Check if user has completed channel join verification."""
    users = get_users()
    return users.get(uid, {}).get("channel_verified", False)


def mark_channel_verified(uid: str) -> None:
    """Mark user as channel-verified in users.json."""
    users = get_users()
    if uid in users:
        users[uid]["channel_verified"] = True
        save_users(users)


async def check_channel_membership(bot, user_id: int) -> bool:
    """Returns True if user has joined the required channel."""
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        logger.info(f"check_channel_membership: user={user_id} status={member.status}")
        return member.status not in ("left", "kicked", "banned")
    except Exception as e:
        logger.error(f"check_channel_membership ERROR: user={user_id} error={e}")
        return None  # None = could not check (bot not admin / API error)


async def send_referral_reward(context, referrer_id: str, referrer_name: str, total_verified: int) -> None:
    """Auto-deliver rewards from rewards DB when user has enough points."""
    net_points = db_get_points(referrer_id)
    rewards    = db_list_rewards()

    # Find cheapest eligible reward
    eligible = [r for r in sorted(rewards, key=lambda x: x["points"]) if net_points >= x["points"]]
    if not eligible:
        return   # not enough points for any reward yet

    reward = eligible[0]
    code   = db_redeem_reward(referrer_id, reward["name"])

    if code:
        # Record points spent
        db_deduct_points(referrer_id, reward["points"], reward["name"])
        try:
            await context.bot.send_message(
                chat_id=int(referrer_id),
                text=(
                    f"🎁 *Reward Mila! Free Coupon!*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"*{reward['name']}*\n\n"
                    f"🔑 *Tumhara free coupon code:*\n\n"
                    f"`{code}`\n\n"
                    f"*{total_verified} verified referrals* complete! 🔥\n"
                    f"Aur refer karo, aur rewards pao! 🚀"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.error(f"Referral reward send failed: {e}")
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"🎁 Reward auto-delivered!\nUser: {referrer_id} ({referrer_name})\nReward: {reward['name']}\nCode: {code}\nPoints used: {reward['points']}",
            )
        except Exception:
            pass
    else:
        # No stock — notify admin
        try:
            await context.bot.send_message(
                chat_id=int(referrer_id),
                text=(
                    f"🎁 *Badhai ho! Reward earn kiya!*\n\n"
                    f"🎟 *{reward['name']}*\n\n"
                    f"⏳ Stock abhi khatam hai — admin jaldi bhej dega!\n"
                    f"Support: {SUPPORT_HANDLE}"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"⚠️ Referral reward PENDING (stock empty)!\nUser: {referrer_id} ({referrer_name}) earned {reward['name']} after {total_verified} referrals.\nPlease add coupon stock: /add_coupon {reward['name']} CODE",
            )
        except Exception:
            pass


def get_stock(pk: str) -> int:
    if pk in COMBO_PARTS:
        coupons = get_coupons()
        return min(len(coupons.get(part, [])) for part in COMBO_PARTS[pk])
    return len(get_coupons().get(pk, []))


def get_stats() -> dict:
    orders = get_orders()
    if isinstance(orders, list):
        orders = {}
    completed = [o for o in orders.values() if o.get("status") == "approved"]
    pending   = [o for o in orders.values() if o.get("status") == "pending"]
    revenue   = sum(
        o.get("total", PRODUCTS[o["product"]]["price"] * o.get("quantity", 1))
        for o in completed if o.get("product") in PRODUCTS
    )
    return {
        "total_sold":    sum(o.get("quantity", 1) for o in completed),
        "total_revenue": revenue,
        "total_users":   len(get_users()),
        "pending_count": len(pending),
    }


def low_stock_alert() -> str:
    a = []
    for k in STORE_PRODUCT_ORDER:
        p = PRODUCTS.get(k)
        if not p:
            continue
        s = get_stock(k)
        if s < LOW_STOCK_THRESHOLD:
            a.append(f"⚠️ *{p['name']}*: only {s} left!")
    return "\n".join(a)


def cancel_user_timers(context: ContextTypes.DEFAULT_TYPE, uid: int) -> None:
    for key in (f"order_timer_{uid}", f"exit_trap_{uid}"):
        job = context.user_data.pop(key, None)
        if job:
            job.schedule_removal()


def clear_user_order_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    for k in ("pending_product", "pending_quantity", "order_start_ts",
              "awaiting_custom_qty", "selected_product"):
        context.user_data.pop(k, None)


# ─────────────── Timer/trap callbacks ───────────────

async def order_expired(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    uid = job.data["user_id"]
    pk  = job.data["product_key"]
    ud  = context.application.user_data.get(uid, {})
    if ud.get("pending_product") != pk:
        return
    clear_user_order_state(context)
    try:
        await context.bot.send_message(
            chat_id=uid,
            text=(
                "❌ *Order Expired!*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "⏰ Your 5-minute payment window has closed.\n"
                "Your order has been *automatically cancelled*.\n\n"
                "💡 Use /start to create a new order."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"order_expired notify: {e}")


async def exit_trap_nudge(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    uid = job.data["user_id"]
    pk  = job.data["product_key"]
    ud  = context.application.user_data.get(uid, {})
    if ud.get("pending_product") != pk:
        return
    remaining   = int(ORDER_TIMEOUT_SECONDS - (now_ts() - ud.get("order_start_ts", now_ts())))
    mins, secs  = divmod(max(remaining, 0), 60)
    try:
        await context.bot.send_message(
            chat_id=uid,
            text=(
                "⚡ *Hurry! Your order is about to expire!*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"😱 Only *{mins}m {secs}s* left!\n\n"
                "💳 Send your payment screenshot *right now*\n"
                "or lose this deal! 🔥"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"exit_trap_nudge: {e}")


# ─────────────── Store menu helper ───────────────

def _store_menu_text_and_keyboard():
    """Returns (text, keyboard) for the main store menu — fully dynamic."""
    lines = [
        "🎉 *Welcome to Coupon Store*",
        "━━━━━━━━━━━━━━━━━━━━",
        "🔥 *Best Deals Available*\n",
    ]
    keyboard = []
    for pk in STORE_PRODUCT_ORDER:
        p = PRODUCTS.get(pk)
        if not p:
            continue
        s    = get_stock(pk)
        desc = f" — {p['desc']}" if p.get("desc") else ""
        sale = _get_active_flash_sale(pk)
        if sale:
            countdown = _flash_countdown(sale["expires_at"])
            lines.append(
                f"⚡ *FLASH SALE!* {p['emoji']} *{p['name']}*{desc}\n"
                f"   ~~₹{sale['original_price']}~~ → *₹{sale['sale_price']}*  ⏳ {countdown}"
            )
            btn_price = sale["sale_price"]
            btn_label = (
                f"⚡ {p['emoji']} {p['name']} – ₹{btn_price} SALE! [{s} left]"
                if s > 0 else
                f"{p['emoji']} {p['name']} – Out of Stock"
            )
        else:
            lines.append(f"{p['emoji']} *{p['name']}*{desc} — *₹{p['price']}*")
            btn_price = p['price']
            btn_label = (
                f"{p['emoji']} {p['name']} – ₹{btn_price}  [{s} left]"
                if s > 0 else
                f"{p['emoji']} {p['name']} – Out of Stock"
            )
        keyboard.append([InlineKeyboardButton(btn_label, callback_data=f"buy_{pk}")])

    lines.append("\n━━━━━━━━━━━━━━━━━━━━")
    lines.append("⚡ Instant Delivery  |  ✅ Trusted  |  💬 24/7 Support")
    keyboard.append([InlineKeyboardButton("🔗 Refer & Earn Free Coupon", callback_data="referral_menu")])
    keyboard.append([InlineKeyboardButton("📞 Contact Support", url="tg://openmessage?user_id=6724474397")])
    return "\n".join(lines), keyboard


# ─────────────── /start ───────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    uid  = str(user.id)

    # ── Handle referral arg: /start ref_12345 OR ref_12345_tk_TOKEN ──
    args        = context.args or []
    referrer_id = None
    ip_token    = None
    if args and args[0].startswith("ref_"):
        raw = args[0][4:]          # strip "ref_"
        if "_tk_" in raw:
            referrer_id, ip_token = raw.split("_tk_", 1)
        else:
            referrer_id = raw      # old-format link (no IP tracking)

    is_new = register_user(user)

    # ── Record referral in SQLite (only for genuinely new users, no self-referral) ──
    if is_new and referrer_id and referrer_id != uid:
        db_insert_referral(uid, referrer_id, token=ip_token)

    cancel_user_timers(context, user.id)
    clear_user_order_state(context)

    # ── Real-time referral validity check (non-blocking, fire-and-forget) ──
    if is_channel_verified(uid):
        context.application.create_task(realtime_referral_check(context.bot, uid))

    # ── Channel Gate: unverified users must join all channels first ──
    if not is_channel_verified(uid):
        has_referral = db_get_referral(uid) is not None
        intro = (
            "🎁 *You were referred by a friend!*\n\n"
            "Join ALL our channels to activate the referral reward\nand unlock the store:\n\n"
            if has_referral else
            "👋 *Welcome!*\n\n"
            "To use this bot, please join *all 3 channels* first:\n\n"
        )
        channel_buttons = [
            [InlineKeyboardButton(f"📢 Join {ch['name']}", url=ch["link"])]
            for ch in REQUIRED_CHANNELS
        ]
        channel_buttons.append([InlineKeyboardButton("✅ I've Joined All — Verify", callback_data="verify_channel_join")])
        await update.message.reply_text(
            intro + "After joining all 3, tap ✅ *I've Joined All* below.",
            reply_markup=InlineKeyboardMarkup(channel_buttons),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── Already verified → show main store ──
    text, keyboard = _store_menu_text_and_keyboard()
    await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Channel gate verify (all new users) ───────────────

async def verify_channel_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Verify channel membership for the mandatory join gate."""
    query = update.callback_query
    await query.answer()
    user = query.from_user
    uid  = str(user.id)

    # Check the one verifiable channel (Channel 3 — public @withoutanyinvestmentwork)
    # Private channels (1 & 2) cannot be verified via API — we trust the user joined them.
    is_member = await check_channel_membership(context.bot, user.id)

    if is_member is False:
        channel_buttons = [
            [InlineKeyboardButton(f"📢 Join {ch['name']}", url=ch["link"])]
            for ch in REQUIRED_CHANNELS
        ]
        channel_buttons.append([InlineKeyboardButton("✅ Verify Again", callback_data="verify_channel_join")])
        try:
            await query.edit_message_text(
                "❌ *You haven't joined Channel 3 yet!*\n\n"
                "Please join *all 3 channels* then tap Verify Again.",
                reply_markup=InlineKeyboardMarkup(channel_buttons),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass  # Message already showing same content (double-click) — ignore
        return

    if is_member is None:
        # API check failed — warn admin but let user through (bot not admin of channel)
        logger.warning(f"Channel membership check failed for {user.id} — letting through")
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"⚠️ Channel gate verify failed for {user.id} ({user.first_name})\nBot needs to be admin of {CHANNEL_USERNAME}!",
            )
        except Exception:
            pass

    # ✅ All channels joined — mark verified
    mark_channel_verified(uid)

    # Also activate referral reward if this user was referred
    ref_row = db_get_referral(uid)
    if ref_row and not ref_row[2]:  # referred + reward not yet given
        # ── IP check ──
        ip_token   = db_get_referral_token(uid)
        ip_blocked = False
        if ip_token:
            ip, is_dup = check_referral_ip(ip_token)
            if is_dup:
                ip_blocked = True
                logger.info(f"Referral IP duplicate blocked for {uid} (token={ip_token}, ip={ip})")

        if ip_blocked:
            # Allow bot access — but do NOT count referral
            await query.edit_message_text(
                "✅ *Welcome to Coupon Store!*\n\n"
                "⚠️ Referral not counted (same network detected)\n"
                "But you can still use the bot ✅",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            db_mark_reward_given(uid)
            referrer_id   = ref_row[1]
            referrer_info = get_users().get(referrer_id, {})
            referrer_name = referrer_info.get("first_name", "Friend")
            total_verified = db_successful_referral_count(referrer_id)
            await send_referral_reward(context, referrer_id, referrer_name, total_verified)
            if ip_token and ip:
                claim_referral_ip(ip_token, ip)
            await query.edit_message_text(
                "✅ *Verified! Channel joined successfully.*\n\n"
                "🎁 Referral reward activated for your friend!\n\n"
                "Now explore the store 👇",
                parse_mode=ParseMode.MARKDOWN,
            )
    else:
        await query.edit_message_text(
            "✅ *Verified! Welcome to Coupon Store!*\n\n"
            "Explore the best deals below 👇",
            parse_mode=ParseMode.MARKDOWN,
        )

    # Show main store as a new message
    text, keyboard = _store_menu_text_and_keyboard()
    await context.bot.send_message(
        chat_id=user.id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Support ───────────────

async def support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    text = (
        "💬 *Customer Support*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Having any issue? Contact us 👇\n\n"
        f"📩 Telegram: `{SUPPORT_HANDLE}`\n\n"
        "We usually reply within a few minutes.\n\n"
        "For order issues, please share your Order ID."
    )
    keyboard = [
        [InlineKeyboardButton("📩 Contact Support", url=f"https://t.me/{SUPPORT_HANDLE.lstrip('@')}")],
        [InlineKeyboardButton("◀️ Back", callback_data="back_to_start")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)


# ─────────────── Verify Referral (channel join check for referral reward) ───────────────

async def verify_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User taps 'Verify' after joining channel. Check membership, then reward referrer."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    uid  = str(user.id)

    # Check if this user has a pending referral
    row = db_get_referral(uid)
    if not row:
        await query.edit_message_text(
            "❌ No referral found for your account.\n\nUse /start normally.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    _, referrer_id, reward_given = row

    # Already verified — prevent duplicate reward
    if reward_given == 1:
        await query.edit_message_text(
            "✅ *Already verified!*\n\nYour referral was already counted. Thank you!",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Check the verifiable channel (Channel 3 — public)
    is_member = await check_channel_membership(context.bot, user.id)

    if is_member is False:
        channel_buttons = [
            [InlineKeyboardButton(f"📢 Join {ch['name']}", url=ch["link"])]
            for ch in REQUIRED_CHANNELS
        ]
        channel_buttons.append([InlineKeyboardButton("✅ Verify Again", callback_data="verify_referral")])
        await query.edit_message_text(
            "❌ *You haven't joined all channels yet!*\n\n"
            "Please join *all 3 channels* then tap Verify Again.",
            reply_markup=InlineKeyboardMarkup(channel_buttons),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if is_member is None:
        # API check failed — alert admin but continue
        logger.warning(f"Referral verify: membership check failed for {user.id}")
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"⚠️ Referral verify failed for user {user.id} ({user.first_name})\nBot is NOT admin of {CHANNEL_USERNAME} — please add bot as admin!",
            )
        except Exception:
            pass

    # ✅ Channel joined — IP check before counting referral
    ip_token   = db_get_referral_token(uid)
    ip_blocked = False
    if ip_token:
        ip, is_dup = check_referral_ip(ip_token)
        if is_dup:
            ip_blocked = True
            logger.info(f"verify_referral: IP duplicate for {uid} (ip={ip})")

    if ip_blocked:
        await query.edit_message_text(
            "✅ *Verified!*\n\n"
            "⚠️ Referral not counted (same network detected)\n"
            "But you can still use the bot ✅",
            parse_mode=ParseMode.MARKDOWN,
        )
        text, keyboard = _store_menu_text_and_keyboard()
        await context.bot.send_message(
            chat_id=user.id, text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    db_mark_reward_given(uid)
    if ip_token:
        claim_referral_ip(ip_token, ip)

    # Count how many verified referrals the referrer now has
    total_verified = db_successful_referral_count(referrer_id)
    users          = get_users()
    referrer       = users.get(referrer_id, {})
    referrer_name  = referrer.get("first_name", "User")
    progress       = total_verified % REFERRAL_GOAL
    remaining      = REFERRAL_GOAL - progress if progress > 0 else 0

    # Confirm to the referred user
    await query.edit_message_text(
        "✅ *Verified! Referral activated!*\n\n"
        "Thank you for joining the channel! 🎉\n"
        "Your friend has been rewarded for referring you.",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Notify referrer of progress
    try:
        progress_bar = "✅" * progress + "⬜" * (REFERRAL_GOAL - progress)
        await context.bot.send_message(
            chat_id=int(referrer_id),
            text=(
                f"🎉 *Referral Verified!*\n\n"
                f"Your friend joined and verified the channel!\n\n"
                f"📊 Progress: *{progress}/{REFERRAL_GOAL}*\n"
                f"{progress_bar}\n\n"
                f"{'🏆 *Reward unlocked!*' if progress == 0 else f'⬜ *{REFERRAL_GOAL - progress} more to earn free coupon!*'}"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass

    # If milestone reached → send reward
    if total_verified > 0 and total_verified % REFERRAL_GOAL == 0:
        await send_referral_reward(context, referrer_id, referrer_name, total_verified)


# ─────────────── Referral menu ───────────────

async def referral_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    uid  = str(user.id)

    total_verified = db_successful_referral_count(uid)
    total_pending  = db_total_referral_count(uid)
    progress       = total_verified % REFERRAL_GOAL
    bot_username   = BOT_USERNAME or (await context.bot.get_me()).username
    if REFERRAL_BASE_URL:
        ref_link = f"{REFERRAL_BASE_URL}/api/ref/{uid}"
    else:
        ref_link = f"https://t.me/{bot_username}?start=ref_{uid}"
    progress_bar   = "✅" * progress + "⬜" * (REFERRAL_GOAL - progress)

    text = (
        "🎁 *Refer & Earn*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 *1 Referral = 1 Point*\n"
        f"📊 *Your Points: {total_verified}*\n\n"
        f"🎯 Goal: *{REFERRAL_GOAL} points* = 1 free coupon\n"
        f"{progress_bar}  {progress}/{REFERRAL_GOAL}\n\n"
        f"📥 Total referred: *{total_pending}*  |  ✅ Verified: *{total_verified}*\n\n"
        f"🔗 *Your Referral Link:*\n"
        f"`{ref_link}`\n\n"
        f"⚠️ *Rules:*\n"
        f"• Share this link with friends\n"
        f"• Friend must join all 3 channels & tap Verify\n"
        f"• Fake referrals are not allowed"
    )
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎯 My Points", callback_data="my_points"),
             InlineKeyboardButton("🎁 Redeem Points", callback_data="redeem_points")],
            [InlineKeyboardButton("◀️ Back", callback_data="back_to_start")],
        ]),
        parse_mode=ParseMode.MARKDOWN,
    )


async def referral_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /referral command — show referral stats as a message."""
    user           = update.effective_user
    uid            = str(user.id)
    total_verified = db_successful_referral_count(uid)
    total_pending  = db_total_referral_count(uid)
    progress       = total_verified % REFERRAL_GOAL
    bot_username   = BOT_USERNAME or (await context.bot.get_me()).username
    if REFERRAL_BASE_URL:
        ref_link = f"{REFERRAL_BASE_URL}/api/ref/{uid}"
    else:
        ref_link = f"https://t.me/{bot_username}?start=ref_{uid}"
    progress_bar   = "✅" * progress + "⬜" * (REFERRAL_GOAL - progress)

    await update.message.reply_text(
        "🔗 *Refer & Earn Free Coupon*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎁 Reward: *Free ₹150 BigBasket Cashback Coupon*\n"
        f"🎯 Goal: *{REFERRAL_GOAL} verified referrals* = 1 free coupon\n\n"
        f"📊 Verified: *{progress}/{REFERRAL_GOAL}*\n"
        f"{progress_bar}\n\n"
        f"📥 Total referred: *{total_pending}*  |  ✅ Verified: *{total_verified}*\n\n"
        f"📤 *Your Referral Link:*\n"
        f"`{ref_link}`\n\n"
        f"_Share this link. Your friend must join the channel & tap Verify to count._",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Back to Store", callback_data="back_to_start")],
        ]),
        parse_mode=ParseMode.MARKDOWN,
    )


async def back_to_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    text, keyboard = _store_menu_text_and_keyboard()
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Referral Validity: single-user check ───────────────

async def _update_referral_validity(bot, user_id: str, referred_by: str, current_status: str) -> str:
    """
    Check if 'user_id' is still in the verifiable channel (Channel 3).
    Returns new status: 'active' or 'removed'.
    Notifies referrer if status changed.
    NOTE: Private channels (1 & 2) cannot be checked via Telegram API.
    """
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=int(user_id))
        still_in = member.status not in ("left", "kicked", "banned")
    except Exception as e:
        logger.debug(f"referral validity check failed for {user_id}: {e}")
        return current_status  # Can't check — leave as-is

    if still_in and current_status == "removed":
        # User rejoined → restore
        db_set_referral_status(user_id, "active")
        new_pts = db_successful_referral_count(referred_by)
        try:
            await bot.send_message(
                chat_id=int(referred_by),
                text=(
                    "✅ *Referral Restored!*\n\n"
                    "Your friend rejoined the channel. 🎉\n"
                    f"📊 *Your Points: {new_pts}*"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        return "active"

    if not still_in and current_status == "active":
        # User left → remove
        db_set_referral_status(user_id, "removed")
        new_pts = db_successful_referral_count(referred_by)
        try:
            await bot.send_message(
                chat_id=int(referred_by),
                text=(
                    "⚠️ *Referral Removed!*\n\n"
                    "Your referred friend left the channel. −1 point.\n"
                    f"📊 *Your Points: {new_pts}*"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        return "removed"

    return current_status


# ─────────────── Periodic Job: check all referrals every 6 min ───────────────

async def periodic_referral_check(context) -> None:
    """Runs every 6 minutes. Checks all verified referrals against Channel 3 membership."""
    rows = db_get_all_verified_referrals()
    if not rows:
        return
    logger.info(f"periodic_referral_check: checking {len(rows)} referrals")
    for user_id, referred_by, current_status in rows:
        await _update_referral_validity(context.bot, user_id, referred_by, current_status)


# ─────────────── Real-time check when a referred user opens bot ───────────────

async def realtime_referral_check(bot, user_id: str) -> None:
    """Call this whenever a verified referred user interacts with the bot."""
    row = db_get_referral(user_id)
    if not row:
        return
    _, referred_by, reward_given = row
    if not reward_given:
        return  # Not yet verified — skip
    # Get current status
    con = sqlite3.connect(REFERRAL_DB)
    status_row = con.execute(
        "SELECT referral_status FROM referrals WHERE user_id = ?", (user_id,)
    ).fetchone()
    con.close()
    current_status = status_row[0] if status_row else "active"
    await _update_referral_validity(bot, user_id, referred_by, current_status)


# ─────────────── My Points ───────────────

async def my_points(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user   = update.effective_user
    uid    = str(user.id)
    points = db_get_points(uid)
    rewards = db_list_rewards()

    lines = [
        "🎯 *My Points*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"📊 *Total Points: {points}*",
        f"",
        "🎁 *Available Rewards:*",
    ]
    if rewards:
        for r in rewards:
            status = "✅ Claimable" if points >= r["points"] and r["stock"] > 0 else (
                "❌ No Stock" if r["stock"] == 0 else f"⬜ Need {r['points'] - points} more"
            )
            lines.append(f"  • *{r['name']}* — {r['points']} pts  [{status}]")
    else:
        lines.append("  _No rewards added yet_")

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🎁 Redeem Points", callback_data="redeem_points")],
            [InlineKeyboardButton("◀️ Back", callback_data="referral_menu")],
        ]),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Redeem Points ───────────────

async def redeem_points(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user    = update.effective_user
    uid     = str(user.id)
    points  = db_get_points(uid)
    rewards = db_list_rewards()

    if not rewards:
        await query.edit_message_text(
            "🎁 *Redeem Points*\n\n"
            "No rewards available right now.\nCheck back later!",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Back", callback_data="referral_menu")],
            ]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = [
        "🎁 *Redeem Points*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 *Your Points: {points}*",
        "",
        "Select a reward to redeem:",
    ]
    buttons = []
    for r in rewards:
        can = points >= r["points"] and r["stock"] > 0
        label = f"{'✅' if can else '🔒'} {r['name']} — {r['points']} pts  [Stock: {r['stock']}]"
        if can:
            buttons.append([InlineKeyboardButton(label, callback_data=f"do_redeem_{r['name']}")])
        else:
            lines.append(f"  🔒 *{r['name']}* — {r['points']} pts  [Stock: {r['stock']}]")
    buttons.append([InlineKeyboardButton("◀️ Back", callback_data="referral_menu")])

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN,
    )


async def do_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle redeem button press — do_redeem_<reward_name>."""
    query = update.callback_query
    await query.answer()
    user        = update.effective_user
    uid         = str(user.id)
    reward_name = query.data[len("do_redeem_"):]

    # Check points
    reward_row = next((r for r in db_list_rewards() if r["name"] == reward_name), None)
    if not reward_row:
        await query.answer("Reward not found!", show_alert=True)
        return

    points = db_get_points(uid)
    if points < reward_row["points"]:
        await query.answer(f"Not enough points! You have {points}, need {reward_row['points']}.", show_alert=True)
        return

    # Deduct points — we use a separate table to track deductions
    code = db_redeem_reward(uid, reward_name)
    if not code:
        await query.answer("No stock available for this reward!", show_alert=True)
        return

    # Record points spent in DB
    con = sqlite3.connect(REFERRAL_DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS points_spent (
            user_id TEXT, points INTEGER, reward_name TEXT, spent_at TEXT
        )
    """)
    con.execute(
        "INSERT INTO points_spent (user_id, points, reward_name, spent_at) VALUES (?,?,?,?)",
        (uid, reward_row["points"], reward_name, datetime.now().isoformat())
    )
    con.commit()
    con.close()

    await query.edit_message_text(
        f"🎉 *Redemption Successful!*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎁 *{reward_name}*\n\n"
        f"🔑 *Your Code:*\n\n"
        f"`{code}`\n\n"
        f"✅ {reward_row['points']} points used.\n"
        f"📊 Remaining Points: {max(0, points - reward_row['points'])}\n\n"
        f"💬 Help: {SUPPORT_HANDLE}",
        parse_mode=ParseMode.MARKDOWN,
    )
    # Notify admin
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🎁 Reward Redeemed!\nUser: {user.id} ({user.first_name})\nReward: {reward_name}\nCode: {code}",
        )
    except Exception:
        pass


# ─────────────── Admin: /add_reward ───────────────

async def cmd_add_reward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /add_reward POINTS NAME"""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/add_reward POINTS NAME`\n\nExample:\n`/add_reward 10 BigBasket150`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        pts = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Points must be a number.", parse_mode=ParseMode.MARKDOWN)
        return
    name = " ".join(args[1:])
    db_add_reward(name, pts)
    await update.message.reply_text(
        f"✅ Reward added!\n*{name}* — {pts} points required.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Admin: /add_coupon (reward coupons) ───────────────

async def cmd_add_reward_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /add_coupon REWARD_NAME CODE1 CODE2 CODE3 ..."""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/add_coupon REWARD_NAME CODE1 CODE2 ...`\n\nExample:\n`/add_coupon Bigbasket_Free ABC123 DEF456 GHI789`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    reward_name = args[0]
    codes       = args[1:]   # har space-separated word ek alag code hai

    added, skipped = [], []
    for code in codes:
        if db_add_reward_coupon(reward_name, code):
            added.append(code)
        else:
            skipped.append(code)

    total_stock = len(db_list_reward_coupons(reward_name))
    lines = [f"✅ *{len(added)} code(s) added to {reward_name}*", f"Stock ab: *{total_stock}*"]
    if added:
        lines.append("\n➕ Added:\n" + "\n".join(f"• `{c}`" for c in added))
    if skipped:
        lines.append("\n⚠️ Already exists (skipped):\n" + "\n".join(f"• `{c}`" for c in skipped))
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ─────────────── Admin: /del_coupon ───────────────

async def cmd_del_reward_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /del_coupon REWARD_NAME CODE"""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/del_coupon REWARD_NAME CODE`\n\nExample:\n`/del_coupon Bigbasket_Free 5677`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    reward_name = args[0]
    code        = args[1]
    ok = db_delete_reward_coupon(reward_name, code)
    if ok:
        remaining = db_list_reward_coupons(reward_name)
        await update.message.reply_text(
            f"✅ Code `{code}` deleted from *{reward_name}*\n"
            f"Remaining stock: *{len(remaining)}*",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            f"❌ Code `{code}` not found in *{reward_name}* (or already used)",
            parse_mode=ParseMode.MARKDOWN,
        )


# ─────────────── Admin: /list_coupons ───────────────

async def cmd_list_reward_coupons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /list_coupons REWARD_NAME — show all unused codes"""
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: `/list_coupons REWARD_NAME`\n\nExample:\n`/list_coupons Bigbasket_Free`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    reward_name = args[0]
    codes = db_list_reward_coupons(reward_name)
    if not codes:
        await update.message.reply_text(
            f"📭 No unused codes in *{reward_name}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    code_list = "\n".join(f"• `{c}`" for c in codes)
    await update.message.reply_text(
        f"📋 *{reward_name}* — {len(codes)} unused codes:\n\n{code_list}",
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Step 1: Product selected → quantity grid ───────────────

async def buy_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query       = update.callback_query
    logger.info(f"buy_product called: data={query.data!r} user={query.from_user.id}")
    await query.answer()
    product_key = query.data.replace("buy_", "")
    product     = PRODUCTS.get(product_key)
    if not product:
        await query.edit_message_text("❌ Product not found.")
        return

    stock = get_stock(product_key)
    if stock == 0:
        await query.edit_message_text(
            "😔 *Out of Stock!*\n\nThis product is currently unavailable. Check back soon!",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    context.user_data["selected_product"] = product_key

    # Build 1–10 grid in two rows of 5, then Custom Quantity
    row1 = [
        InlineKeyboardButton(QTY_EMOJIS[q], callback_data=f"qty_{q}")
        for q in range(1, 6) if q <= stock
    ]
    row2 = [
        InlineKeyboardButton(QTY_EMOJIS[q], callback_data=f"qty_{q}")
        for q in range(6, 11) if q <= stock
    ]
    keyboard = []
    if row1: keyboard.append(row1)
    if row2: keyboard.append(row2)
    keyboard.append([InlineKeyboardButton("✏️ Custom Quantity", callback_data="qty_custom")])
    keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="back_to_start")])

    display_price = get_unit_price(product_key, 1)
    sale          = _get_active_flash_sale(product_key)
    if sale:
        price_line = (
            f"💰 Price per unit: ~~₹{sale['original_price']}~~ → *₹{display_price}* ⚡ FLASH SALE! ({_flash_countdown(sale['expires_at'])})\n"
        )
    else:
        price_line = f"💰 Price per unit: *₹{display_price}*\n"

    await query.edit_message_text(
        f"📦 *Select Quantity*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{product['emoji']} *{product['name']}*\n"
        + price_line
        + (
            f"🏷 *Bulk Discount:* 10+ = ₹32/each | 20+ = ₹30/each\n"
            if product_key == "coupon_100" else ""
        ) +
        f"📊 Stock available: *{stock}*\n\n"
        f"How many do you want?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Custom quantity prompt ───────────────

async def custom_qty_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query       = update.callback_query
    await query.answer()
    product_key = context.user_data.get("selected_product")
    product     = PRODUCTS.get(product_key) if product_key else None
    if not product:
        await query.edit_message_text("❌ Session expired. Please use /start.", parse_mode=ParseMode.MARKDOWN)
        return

    stock = get_stock(product_key)
    context.user_data["awaiting_custom_qty"] = True

    await query.edit_message_text(
        f"✏️ *Enter Custom Quantity*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{product['emoji']} *{product['name']}*\n"
        f"📊 Max available: *{stock}*\n\n"
        f"👇 *Type the number of coupons you want:*",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_order")]]),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Step 2: Confirm quantity → start timers & show payment ───────────────

async def _confirm_quantity(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    product_key: str,
    quantity: int,
) -> None:
    """Shared logic after quantity is known — show order summary + start timers."""
    product = PRODUCTS[product_key]
    stock   = get_stock(product_key)

    if quantity <= 0:
        msg = "❌ Quantity must be greater than 0. Please try again."
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    if quantity > stock:
        msg = (
            f"😔 *Not enough stock!*\n\n"
            f"You requested *{quantity}* but only *{stock}* available.\n"
            f"Use /start to choose again."
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    cancel_user_timers(context, update.effective_user.id)

    ts         = now_ts()
    unit_price = get_unit_price(product_key, quantity)
    total      = unit_price * quantity
    context.user_data["pending_product"]  = product_key
    context.user_data["pending_quantity"] = quantity
    context.user_data["order_start_ts"]   = ts
    context.user_data.pop("awaiting_custom_qty", None)
    context.user_data.pop("selected_product", None)

    uid = update.effective_user.id

    if context.job_queue is not None:
        try:
            timer_job = context.job_queue.run_once(
                order_expired, when=ORDER_TIMEOUT_SECONDS,
                data={"user_id": uid, "product_key": product_key}, name=f"order_{uid}",
            )
            trap_job = context.job_queue.run_once(
                exit_trap_nudge, when=EXIT_TRAP_SECONDS,
                data={"user_id": uid, "product_key": product_key}, name=f"trap_{uid}",
            )
            context.user_data[f"order_timer_{uid}"] = timer_job
            context.user_data[f"exit_trap_{uid}"]   = trap_job
        except Exception as e:
            logger.warning(f"Timer setup failed (non-fatal): {e}")
    else:
        logger.warning("JobQueue not available — timers disabled")

    qty_disp = QTY_EMOJIS.get(quantity, f"×{quantity}")

    # Combo note — show what the buyer gets
    combo_note = ""
    if product_key in COMBO_PARTS:
        parts_names = " + ".join(
            PRODUCTS.get(p, {}).get("name", p) for p in COMBO_PARTS[product_key]
        )
        combo_note = f"\n🎁 *Combo:* {parts_names} (2 codes per unit)\n"

    extra_tnc = ""
    if product_key in ("coupon_bigbasket_150", "bigbasket"):
        extra_tnc = (
            "\n━━━━━━━━━━━━━━━━━━━━\n"
            "📜 *BigBasket – ₹150 Cashback on ₹149 cart*\n\n"
            "📋 *Terms & Conditions:*\n"
            "• Applicable only for *new users* on BigBasket.\n"
            "• Cashback of ₹150 credited to bbwallet within *24–48 hours* after delivery.\n"
            "• Minimum cart value must be *₹149*.\n"
            "• Can be used *once per customer per device*.\n"
            "• Not applicable on Paan corner, Baby food, Electronics, Oils, Atta, Ghee, etc.\n"
            "• Benefits removed if items are returned or refunded.\n"
            "• Expires on *30 Apr 2026, 11:59 PM*.\n"
            "• 🔐 Codes are unique, non-refundable & non-returnable.\n"
            "• 💸 Payments once made cannot be reversed.\n\n"
            "🛒 *How to Redeem:*\n"
            "1. Go to checkout page.\n"
            "2. Tap *Apply Voucher*.\n"
            "3. Paste your unique voucher code.\n"
            "4. Click Apply and complete payment.\n"
            "5. Cashback credited after delivery."
        )

    summary_text = (
        f"🧾 *Order Summary*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎟 Product:    *{product['name']}*\n"
        f"{combo_note}"
        f"📦 Quantity:   *{qty_disp} × {quantity}*\n"
        f"💰 Unit Price: ₹{unit_price}\n"
        f"💵 *Total:     ₹{total}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📲 *Payment Details*\n"
        f"🏦 UPI ID: `{UPI_ID}`\n"
        f"💵 Pay exactly: *₹{total}*\n\n"
        f"📸 *After payment, send your screenshot here.*\n\n"
        f"⏳ You have *5 minutes* to complete payment.\n"
        f"After that your order will be cancelled automatically."
        f"{extra_tnc}"
    )
    cancel_btn = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Order", callback_data="cancel_order")]])

    # Try to send QR code image alongside the summary
    qr_sent = False
    if os.path.exists(QR_IMAGE_PATH):
        try:
            with open(QR_IMAGE_PATH, "rb") as qr_file:
                if update.callback_query:
                    await context.bot.send_photo(
                        chat_id=uid, photo=qr_file,
                        caption=summary_text, reply_markup=cancel_btn,
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    await update.callback_query.edit_message_text(
                        "👆 *See the message above for your order details.*",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                else:
                    await update.message.reply_photo(
                        photo=qr_file, caption=summary_text,
                        reply_markup=cancel_btn, parse_mode=ParseMode.MARKDOWN,
                    )
            qr_sent = True
        except Exception as e:
            logger.warning(f"QR send failed: {e}")

    if not qr_sent:
        if update.callback_query:
            await update.callback_query.edit_message_text(
                summary_text, reply_markup=cancel_btn, parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
                summary_text, reply_markup=cancel_btn, parse_mode=ParseMode.MARKDOWN,
            )


async def select_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle quantity button 1–10. callback_data is qty_<number>."""
    query = update.callback_query
    await query.answer()

    product_key = context.user_data.get("selected_product")
    if not product_key:
        await query.edit_message_text("❌ Session expired. Please use /start.", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        quantity = int(query.data.split("_")[1])   # "qty_3" → 3
    except (IndexError, ValueError):
        await query.edit_message_text("❌ Invalid selection. Please use /start.", parse_mode=ParseMode.MARKDOWN)
        return

    logger.info(f"Quantity selected: {quantity} for {product_key} by user {update.effective_user.id}")
    print(f"Quantity selected: {quantity} for {product_key}")

    await _confirm_quantity(update, context, product_key, quantity)


# ─────────────── Cancel order ───────────────

async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    cancel_user_timers(context, query.from_user.id)
    clear_user_order_state(context)
    await query.edit_message_text(
        "❌ *Order Cancelled.*\n\nUse /start to browse again.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Screenshot / payment handler ───────────────

async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user.id == ADMIN_ID:
        return

    product_key = context.user_data.get("pending_product")
    quantity    = context.user_data.get("pending_quantity", 1)
    if not product_key:
        return

    product = PRODUCTS.get(product_key)
    if not product:
        return

    stock = get_stock(product_key)
    if stock < quantity:
        await update.message.reply_text(
            f"😔 *Stock changed!* Only *{stock}* left — you ordered {quantity}.\nUse /start to retry.",
            parse_mode=ParseMode.MARKDOWN,
        )
        cancel_user_timers(context, user.id)
        clear_user_order_state(context)
        return

    start_ts  = context.user_data.get("order_start_ts", now_ts())
    elapsed   = now_ts() - start_ts
    is_fast   = elapsed < FAST_PAYMENT_THRESHOLD
    priority  = "fast" if is_fast else "normal"
    plabel    = "🔥 FAST PAYMENT" if is_fast else "🐢 NORMAL PAYMENT"

    cancel_user_timers(context, user.id)
    clear_user_order_state(context)

    order_id   = f"{user.id}_{int(now_ts())}"
    unit_price = get_unit_price(product_key, quantity)
    total      = unit_price * quantity

    order = {
        "order_id":    order_id,
        "user_id":     user.id,
        "username":    user.username or "",
        "first_name":  user.first_name,
        "product":     product_key,
        "quantity":    quantity,
        "total":       total,
        "status":      "pending",
        "priority":    priority,
        "elapsed_sec": round(elapsed),
        "timestamp":   datetime.now().isoformat(),
        "file_id":     update.message.photo[-1].file_id if update.message.photo else None,
    }

    orders = get_orders()
    orders[order_id] = order
    save_orders(orders)

    # Save to pending_orders keyed by user_id for fast /approve lookup
    pending = get_pending()
    pending[str(user.id)] = order_id
    save_pending(pending)

    me, se = divmod(int(elapsed), 60)
    await update.message.reply_text(
        "📸 *Screenshot received!*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⏳ Waiting for admin approval...\n\n"
        "🕐 You will be notified once approved.\n"
        "━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Escape all user-controlled strings so HTML mode never breaks
    safe_name     = html.escape(user.first_name or "Unknown")
    safe_username = html.escape(f"@{user.username}") if user.username else "No username"
    safe_product  = html.escape(product["name"])
    safe_order_id = html.escape(order_id)

    admin_text = (
        f"{plabel}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Name: <a href='tg://user?id={user.id}'>{safe_name}</a>\n"
        f"🔗 Username: {safe_username}\n"
        f"🆔 User ID: <code>{user.id}</code>\n"
        f"📦 Product: <b>{safe_product}</b>\n"
        f"🔢 Quantity: <b>{quantity}</b>\n"
        f"💰 Total: <b>₹{total}</b>\n"
        f"⏱ Paid in: <b>{me}m {se}s</b>\n"
        f"🕐 {datetime.now().strftime('%d %b %Y, %I:%M %p')}\n"
        f"🔑 Order: <code>{safe_order_id}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Use: <code>/approve {user.id}</code> to approve"
    )
    approve_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{order_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{order_id}"),
    ]])

    print(f"Forwarding order to admin: {ADMIN_ID}  |  order: {order_id}  |  user: {user.id}")
    logger.info(f"Forwarding screenshot to admin {ADMIN_ID} for order {order_id}")

    try:
        if update.message.photo:
            await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=update.message.photo[-1].file_id,
                caption=admin_text,
                reply_markup=approve_kb,
                parse_mode=ParseMode.HTML,
            )
            logger.info(f"Admin notified successfully for order {order_id}")
        elif update.message.document:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=update.message.document.file_id,
                caption=admin_text,
                reply_markup=approve_kb,
                parse_mode=ParseMode.HTML,
            )
            logger.info(f"Admin notified (document) for order {order_id}")
        else:
            # Fallback: send text-only notification if no image
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=admin_text,
                reply_markup=approve_kb,
                parse_mode=ParseMode.HTML,
            )
            logger.info(f"Admin notified (text-only) for order {order_id}")
    except Exception as e:
        logger.error(f"Forward to admin failed: {e}")
        print(f"ERROR forwarding to admin: {e}")
        # Last-resort: plain text, no parse mode
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"NEW ORDER\nUser: {user.id} ({user.first_name})\nProduct: {product['name']} x{quantity}\nTotal: ₹{total}\nOrder: {order_id}\n\nUse /approve {user.id}",
            )
        except Exception as e2:
            logger.error(f"Fallback admin notify also failed: {e2}")


# ─────────────── Shared approve logic ───────────────

async def _execute_approve(context, order_id: str) -> tuple:
    """Assign coupons and notify user. Returns (order, coupon_list) or raises."""
    orders  = get_orders()
    order   = orders.get(order_id)
    if not order:
        return None, "not_found"
    if order["status"] != "pending":
        return order, "already_done"

    pk       = order["product"]
    quantity = order.get("quantity", 1)
    coupons  = get_coupons()

    # ── Combo: dispense from each sub-product ──
    if pk in COMBO_PARTS:
        parts = COMBO_PARTS[pk]
        for part in parts:
            if len(coupons.get(part, [])) < quantity:
                return order, f"low_stock:{len(coupons.get(part, []))}"
        assigned = []
        for part in parts:
            pool = coupons.get(part, [])
            assigned.extend(pool[:quantity])
            coupons[part] = pool[quantity:]
        save_coupons(coupons)
    else:
        pool = coupons.get(pk, [])
        if len(pool) < quantity:
            return order, f"low_stock:{len(pool)}"
        assigned    = pool[:quantity]
        coupons[pk] = pool[quantity:]
        save_coupons(coupons)

    order["status"]       = "approved"
    order["coupon_codes"] = assigned
    order["approved_at"]  = datetime.now().isoformat()
    orders[order_id]      = order
    save_orders(orders)

    # Remove from pending
    pending = get_pending()
    pending.pop(str(order["user_id"]), None)
    save_pending(pending)

    product      = PRODUCTS.get(pk, {"name": pk})
    coupon_lines = "\n".join(f"<code>{html.escape(str(c))}</code>" for c in assigned)

    # ── Step 1: Send ONLY coupon code(s) — HTML mode (safer than Markdown) ──
    try:
        await context.bot.send_message(
            chat_id=order["user_id"],
            text=(
                f"✅ <b>Payment Confirmed!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 <b>{html.escape(product.get('name', pk))}</b>  ×{quantity}\n\n"
                f"🎟 <b>Your Coupon Code(s):</b>\n\n"
                f"{coupon_lines}\n\n"
                f"🙏 Thank you! Come back for more deals.\n"
                f"💬 Help: {html.escape(SUPPORT_HANDLE)}"
            ),
            parse_mode=ParseMode.HTML,
        )
        logger.info(f"Coupon delivered to {order['user_id']} — codes: {assigned}")
    except Exception as e:
        logger.error(f"Coupon delivery failed for {order['user_id']}: {e}")
        # Fallback: plain text, no formatting
        try:
            plain_codes = "\n".join(str(c) for c in assigned)
            await context.bot.send_message(
                chat_id=order["user_id"],
                text=f"✅ Payment Confirmed!\n\nYour Coupon Code(s):\n\n{plain_codes}\n\nThank you!",
            )
            logger.info(f"Coupon delivered (plain fallback) to {order['user_id']}")
        except Exception as e2:
            logger.error(f"Coupon delivery fallback also failed: {e2}")

    return order, assigned


# ─────────────── /approve <user_id> command ───────────────

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return

    if not context.args:
        await update.message.reply_text(
            "❌ *Usage:* `/approve <user_id>`", parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        target_uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.", parse_mode=ParseMode.MARKDOWN)
        return

    pending  = get_pending()
    order_id = pending.get(str(target_uid))
    if not order_id:
        await update.message.reply_text(
            f"❌ No pending order found for user `{target_uid}`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    order, result = await _execute_approve(context, order_id)

    if result == "not_found":
        await update.message.reply_text("❌ Order not found.", parse_mode=ParseMode.MARKDOWN)
        return
    if result == "already_done":
        await update.message.reply_text("⚠️ Order already processed.", parse_mode=ParseMode.MARKDOWN)
        return
    if isinstance(result, str) and result.startswith("low_stock"):
        avail = result.split(":")[1]
        await update.message.reply_text(
            f"⚠️ Only *{avail}* coupon(s) in stock, need {order.get('quantity',1)}. Add more first.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    assigned = result
    product  = PRODUCTS[order["product"]]
    plabel   = "🔥 FAST" if order.get("priority") == "fast" else "🐢 NORMAL"
    codes    = ", ".join(f"`{c}`" for c in assigned)
    await update.message.reply_text(
        f"✅ *Approved!* [{plabel}]\n"
        f"👤 User: `{target_uid}` — {order.get('first_name','')}\n"
        f"📦 {product['name']} × {order.get('quantity',1)}\n"
        f"🎟 Codes sent: {codes}",
        parse_mode=ParseMode.MARKDOWN,
    )

    alert = low_stock_alert()
    if alert:
        await update.message.reply_text(
            f"⚠️ *Low Stock Alert!*\n━━━━━━━━━━━━━━\n{alert}",
            parse_mode=ParseMode.MARKDOWN,
        )


# ─────────────── Button-based approve (from photo caption) ───────────────

async def approve_order_btn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Not authorized.", show_alert=True)
        return
    await query.answer()

    order_id      = query.data.replace("approve_", "")
    order, result = await _execute_approve(context, order_id)

    if result == "not_found":
        await query.edit_message_caption("❌ Order not found.", parse_mode=ParseMode.MARKDOWN)
        return
    if result == "already_done":
        await query.answer("Already processed.", show_alert=True)
        return
    if isinstance(result, str) and result.startswith("low_stock"):
        avail = result.split(":")[1]
        await query.answer(f"⚠️ Only {avail} left, need {order.get('quantity',1)}.", show_alert=True)
        return

    assigned = result
    product  = PRODUCTS[order["product"]]
    plabel   = "🔥 FAST" if order.get("priority") == "fast" else "🐢 NORMAL"
    codes    = ", ".join(f"`{c}`" for c in assigned)
    await query.edit_message_caption(
        f"✅ *Approved!* [{plabel}]\n"
        f"🆔 Order: `{order_id}`\n"
        f"👤 {order.get('first_name','N/A')}\n"
        f"📦 {product['name']} × {order.get('quantity',1)}\n"
        f"🎟 {codes}",
        parse_mode=ParseMode.MARKDOWN,
    )

    alert = low_stock_alert()
    if alert:
        await context.bot.send_message(
            chat_id=ADMIN_ID, text=f"⚠️ *Low Stock Alert!*\n━━━━━━━━━━━━━━\n{alert}",
            parse_mode=ParseMode.MARKDOWN,
        )


# ─────────────── Reject ───────────────

async def _do_reject(context, order_id: str):
    orders = get_orders()
    order  = orders.get(order_id)
    if not order or order["status"] != "pending":
        return None, "not_found"
    order["status"]      = "rejected"
    order["rejected_at"] = datetime.now().isoformat()
    orders[order_id]     = order
    save_orders(orders)
    pending = get_pending()
    pending.pop(str(order["user_id"]), None)
    save_pending(pending)
    try:
        await context.bot.send_message(
            chat_id=order["user_id"],
            text=(
                "❌ *Payment Not Verified!*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "😔 We could not verify your payment.\n\n"
                "📌 *Possible reasons:*\n"
                "• Screenshot unclear\n"
                "• Wrong amount paid\n"
                "• Payment not completed\n\n"
                f"💡 Retry with /start or contact `{SUPPORT_HANDLE}`"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"Reject notify failed: {e}")
    return order, "ok"


async def reject_order_btn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Not authorized.", show_alert=True)
        return
    await query.answer()
    order_id      = query.data.replace("reject_", "")
    order, status = await _do_reject(context, order_id)
    if status == "not_found":
        await query.edit_message_caption("❌ Not found or already processed.", parse_mode=ParseMode.MARKDOWN)
        return
    product = PRODUCTS.get(order["product"], {})
    await query.edit_message_caption(
        f"❌ *Rejected*\n🆔 `{order_id}`\n👤 {order.get('first_name','')}\n"
        f"📦 {product.get('name','N/A')} × {order.get('quantity',1)}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def reject_text_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    order_id = query.data.replace("reject_text_", "")
    _, status = await _do_reject(context, order_id)
    msg = "✅ Rejected." if status == "ok" else "Not found or already processed."
    await query.answer(msg, show_alert=True)


# ─────────────── /broadcast <message> command ───────────────

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return

    # Extract full message text after the command, preserving newlines
    raw_text     = update.message.text or ""
    parts        = raw_text.split(None, 1)
    message_text = parts[1].strip() if len(parts) > 1 else ""

    if not message_text:
        await update.message.reply_text(
            "❌ *Usage:* `/broadcast Your message here`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    users   = get_users()
    success = failed = 0

    for uid in users:
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=message_text,
            )
            success += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"📢 *Broadcast Complete!*\n\n✅ Sent: *{success}*\n❌ Failed: *{failed}*",
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Admin panel ───────────────

def _admin_text() -> str:
    stats = get_stats()
    text  = (
        f"📊 *ADMIN DASHBOARD*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 Total Coupons Sold: *{stats['total_sold']}*\n"
        f"💰 Total Earnings: *₹{stats['total_revenue']}*\n"
        f"👥 Total Users: *{stats['total_users']}*\n"
        f"⏳ Pending Orders: *{stats['pending_count']}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    alert = low_stock_alert()
    if alert:
        text += f"\n\n{alert}"
    return text


def _admin_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏳ Pending Orders", callback_data="admin_pending"),
            InlineKeyboardButton("📦 Stock",          callback_data="admin_stock"),
        ],
        [
            InlineKeyboardButton("📊 Stats",          callback_data="admin_stats"),
            InlineKeyboardButton("➕ Add Coupon",     callback_data="admin_add_coupon"),
        ],
        [
            InlineKeyboardButton("📋 Products",       callback_data="admin_products"),
            InlineKeyboardButton("📤 Export Users",   callback_data="admin_export_users"),
        ],
        [
            InlineKeyboardButton("👥 Users",          callback_data="admin_users"),
            InlineKeyboardButton("📢 Broadcast",      callback_data="admin_broadcast"),
        ],
        [
            InlineKeyboardButton("🔗 Referral Tracker", callback_data="admin_referrals"),
            InlineKeyboardButton("🎁 Rewards",          callback_data="admin_rewards"),
        ],
    ])


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    await update.message.reply_text(
        _admin_text(), reply_markup=_admin_kb(), parse_mode=ParseMode.MARKDOWN,
    )


async def admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    context.user_data.pop("broadcast_mode", None)
    await query.edit_message_text(
        _admin_text(), reply_markup=_admin_kb(), parse_mode=ParseMode.MARKDOWN,
    )


async def admin_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    lines   = ["📦 *Current Stock*\n━━━━━━━━━━━━━━━━━━━━\n"]
    for k in STORE_PRODUCT_ORDER:
        p = PRODUCTS.get(k)
        if not p:
            continue
        s      = get_stock(k)
        status = "🔴 Out of Stock" if s == 0 else ("⚠️ Low" if s < LOW_STOCK_THRESHOLD else "✅ In Stock")
        lines.append(f"{p['emoji']} *{p['name']}*\n   Count: *{s}* — {status}")
    await query.edit_message_text(
        "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]]),
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_products_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all products with name, price, stock — with edit instructions."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    lines = ["📋 *Products — Name & Price*", "━━━━━━━━━━━━━━━━━━━━", ""]
    for pk in STORE_PRODUCT_ORDER:
        p = PRODUCTS.get(pk, {})
        s = get_stock(pk)
        lines.append(f"{p.get('emoji','🔹')} `{pk}`\n   *{p.get('name', pk)}* — ₹{p.get('price', 0)}  [Stock: {s}]")
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "*Naam change:*",
        "`/set_name ID Naya Naam`",
        "",
        "*Price change:*",
        "`/set_price ID 39`",
        "",
        "*Description change:*",
        "`/set_desc ID description`",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "➕ *Nai service add karo:*",
        "`/add_service ID PRICE EMOJI Naam`",
        "_Example: /add_service amazon\\_100 20 🛍️ Amazon Gift Card_",
        "",
        "🗑️ *Service delete karo:*",
        "`/del_service ID`",
        "_Example: /del\\_service amazon\\_100_",
    ]
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]]),
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    orders    = get_orders()
    completed = [o for o in orders.values() if o.get("status") == "approved"]
    rejected  = [o for o in orders.values() if o.get("status") == "rejected"]
    pending   = [o for o in orders.values() if o.get("status") == "pending"]
    fast      = [o for o in completed if o.get("priority") == "fast"]
    sold_rev  = {}
    for o in completed:
        pk = o.get("product", "?")
        sold_rev.setdefault(pk, [0, 0])
        sold_rev[pk][0] += o.get("quantity", 1)
        sold_rev[pk][1] += o.get("total", 0)
    stats = get_stats()
    lines = [
        "📊 *Detailed Statistics*", "━━━━━━━━━━━━━━━━━━━━",
        f"👥 Total Users:   *{stats['total_users']}*",
        f"📦 Total Orders:  *{len(orders)}*",
        f"✅ Completed:     *{len(completed)}*",
        f"🔥 Fast Payers:   *{len(fast)}*",
        f"❌ Rejected:      *{len(rejected)}*",
        f"⏳ Pending:       *{len(pending)}*",
        f"💰 Revenue:       *₹{stats['total_revenue']}*",
        "", "📈 *By Product:*",
    ]
    for k, p in PRODUCTS.items():
        d = sold_rev.get(k, [0, 0])
        lines.append(f"  {p['emoji']} {p['name']}: *{d[0]} sold* | ₹{d[1]}")
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]]),
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_referrals(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: Referral Tracker — User ID | Points left | Total referrals."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return

    leaderboard = db_get_referral_leaderboard()
    users       = get_users()

    if not leaderboard:
        await query.edit_message_text(
            "🔗 <b>Referral Tracker</b>\n\nKoi referral abhi nahi hua.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]]),
            parse_mode=ParseMode.HTML,
        )
        return

    import html as _html

    lines = [
        "🔗 <b>Referral Tracker</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"👥 Total referrers: <b>{len(leaderboard)}</b>",
        "",
    ]

    buttons = []
    for referrer_id, active, total in leaderboard[:20]:
        u      = users.get(str(referrer_id), {})
        name   = _html.escape(u.get("first_name", "Unknown"))
        uname  = f"@{_html.escape(u['username'])}" if u.get("username") else ""
        pts    = db_get_points(str(referrer_id))

        # One clean block per user
        lines.append(
            f"┌ 🆔 <code>{referrer_id}</code>  {('— ' + uname) if uname else ''}\n"
            f"├ 👤 <b>{name}</b>\n"
            f"├ 💎 Points bacha: <b>{pts}</b>\n"
            f"└ 📨 Total refer: <b>{total}</b>  (✅ Active: <b>{active}</b>)"
        )

        # Button label: ID + points + total
        buttons.append([InlineKeyboardButton(
            f"🆔 {referrer_id}  |  💎{pts}pts  |  📨{total} refer",
            callback_data=f"admin_ref_detail_{referrer_id}"
        )])

    buttons.append([InlineKeyboardButton("◀️ Back", callback_data="admin_back")])

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n\n<i>...aur bhi hain, scroll karo ya detail dekho</i>"

    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def admin_ref_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: Show detail of who a specific referrer referred."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return

    import html as _html
    referrer_id = query.data.replace("admin_ref_detail_", "")
    users       = get_users()
    u           = users.get(referrer_id, {})
    name        = _html.escape(u.get("first_name", "Unknown"))
    uname       = f"@{_html.escape(u.get('username'))}" if u.get("username") else f"ID:{referrer_id}"

    referred = db_get_referred_users_detail(referrer_id)
    pts      = db_get_points(referrer_id)

    lines = [
        f"🔗 <b>Referral Detail</b>",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"🆔 User ID: <code>{referrer_id}</code>",
        f"👤 Name: <b>{name}</b>  {uname}",
        f"💎 Points bacha: <b>{pts}</b>",
        f"📨 Total referred: <b>{len(referred)}</b>",
        f"",
        f"<b>Referred Users:</b>",
    ]

    for uid, reward_given, status, joined_at in referred:
        ru      = users.get(str(uid), {})
        rname   = _html.escape(ru.get("first_name", "Unknown"))
        runame  = f"@{_html.escape(ru['username'])}" if ru.get("username") else f"ID:{uid}"
        date    = joined_at[:10] if joined_at else "?"
        if reward_given and status == "active":
            badge = "✅"
        elif reward_given and status == "removed":
            badge = "🚫"
        else:
            badge = "⏳"
        lines.append(f"{badge} <b>{rname}</b> ({runame}) — {date}")

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3900] + "\n\n<i>(list bahut badi hai)</i>"

    await query.edit_message_text(
        text + "\n\n✅=Active  ⏳=Pending  🚫=Left Channel",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("◀️ Referral List", callback_data="admin_referrals")],
            [InlineKeyboardButton("🏠 Admin Home",    callback_data="admin_back")],
        ]),
        parse_mode=ParseMode.HTML,
    )


async def admin_rewards_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: Show all rewards with stock + manage buttons."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    rewards = db_list_rewards()
    if not rewards:
        await query.edit_message_text(
            "🎁 *Rewards Panel*\n\nAbhi koi reward set nahi hai.\n\n"
            "Reward banane ke liye:\n"
            "`/add_reward POINTS NAAM`\n"
            "Example: `/add_reward 1 Bigbasket_Free`\n\n"
            "Phir codes add karo:\n"
            "`/add_coupon Bigbasket_Free CODE1 CODE2`",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    lines = ["🎁 *Rewards Panel*", "━━━━━━━━━━━━━━━━━━━━", ""]
    buttons = []
    for r in rewards:
        stock_emoji = "✅" if r["stock"] > 0 else "❌"
        lines.append(f"{stock_emoji} *{r['name']}*\n   🎯 Points required: *{r['points']}* | 📦 Stock: *{r['stock']}*")
        buttons.append([
            InlineKeyboardButton(f"✏️ {r['name'][:20]} ({r['stock']} codes)", callback_data=f"admin_rwd_{r['name']}"),
        ])
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "➕ *Naya reward add:*",
        "`/add_reward POINTS NAAM`",
        "",
        "📦 *Stock add:*",
        "`/add_coupon NAAM CODE1 CODE2`",
    ]
    buttons.append([InlineKeyboardButton("◀️ Back", callback_data="admin_back")])
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_reward_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: Detail of one reward — edit points + delete."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    reward_name = query.data.replace("admin_rwd_", "")
    rewards     = db_list_rewards()
    r = next((x for x in rewards if x["name"] == reward_name), None)
    if not r:
        await query.answer("Reward nahi mila!", show_alert=True)
        return
    codes = db_list_reward_coupons(reward_name)
    code_preview = "\n".join(f"• `{c}`" for c in codes[:10])
    if len(codes) > 10:
        code_preview += f"\n... aur {len(codes)-10} codes hain"
    await query.edit_message_text(
        f"🎁 *{reward_name}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎯 Points required: *{r['points']}*\n"
        f"📦 Stock: *{r['stock']} codes*\n\n"
        f"*Unused Codes:*\n{code_preview if code_preview else 'Koi code nahi hai!'}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Points change: `/add_reward {r['points']} {reward_name}`\n"
        f"Code add: `/add_coupon {reward_name} CODE1 CODE2`\n"
        f"Code delete: `/del_coupon {reward_name} CODE`",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(f"🗑️ Delete Reward '{reward_name}'", callback_data=f"admin_rwd_del_{reward_name}")],
            [InlineKeyboardButton("◀️ Back to Rewards", callback_data="admin_rewards")],
        ]),
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_reward_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: Confirm delete a reward."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    reward_name = query.data.replace("admin_rwd_del_", "")
    ok = db_delete_reward(reward_name)
    if ok:
        await query.edit_message_text(
            f"🗑️ *Reward delete ho gaya!*\n\n`{reward_name}` aur uske saare unused codes hata diye gaye.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back to Rewards", callback_data="admin_rewards")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await query.answer("Delete fail hua!", show_alert=True)


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show last 20 registered users sorted by join date (newest first)."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    users = get_users()
    if not users:
        await query.edit_message_text(
            "👥 *No users yet.*",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    # Sort by join date, newest first
    sorted_users = sorted(
        users.values(),
        key=lambda u: u.get("joined", ""),
        reverse=True,
    )[:20]
    lines = [f"👥 Recent Users ({len(users)} total)", "━━━━━━━━━━━━━━━━━━━━"]
    for i, u in enumerate(sorted_users, 1):
        uname  = f"@{u['username']}" if u.get("username") else "-"
        name   = u.get("first_name", "?")
        joined = u.get("joined", "?")[:10]
        lines.append(f"{i}. {u['id']} | {name} {uname} | {joined}")
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]]),
    )


async def admin_export_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send users.json as a file document to the admin."""
    query = update.callback_query
    await query.answer("Preparing file…", show_alert=False)
    if query.from_user.id != ADMIN_ID:
        return
    users = get_users()
    if not users:
        await context.bot.send_message(chat_id=ADMIN_ID, text="👥 No users yet.")
        return
    import io
    data_bytes = json.dumps(users, indent=2, ensure_ascii=False).encode("utf-8")
    bio = io.BytesIO(data_bytes)
    bio.seek(0)
    await context.bot.send_document(
        chat_id=ADMIN_ID,
        document=bio,
        filename="users.json",
        caption=f"users.json — {len(users)} total users",
    )


async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    orders  = get_orders()
    pending = {oid: o for oid, o in orders.items() if o.get("status") == "pending"}
    if not pending:
        await query.edit_message_text(
            "✅ *No pending orders!*",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    sorted_orders = sorted(
        pending.items(),
        key=lambda x: (0 if x[1].get("priority") == "fast" else 1, x[1].get("timestamp", "")),
    )
    lines  = [f"⏳ *Pending Orders ({len(pending)})*", "━━━━━━━━━━━━━━━━━━━━"]
    kbrows = []
    for oid, o in sorted_orders[:10]:
        p     = PRODUCTS.get(o["product"], {})
        name  = o.get("first_name", "?")
        label = "🔥" if o.get("priority") == "fast" else "🐢"
        qty   = o.get("quantity", 1)
        me, se = divmod(o.get("elapsed_sec", 0), 60)
        lines.append(f"{label} *{name}* — {p.get('name','?')} ×{qty}  ⏱{me}m{se}s")
        kbrows.append([
            InlineKeyboardButton(f"✅ {label}{name} ×{qty}", callback_data=f"approve_{oid}"),
            InlineKeyboardButton("❌ Reject",                 callback_data=f"reject_text_{oid}"),
        ])
    kbrows.append([InlineKeyboardButton("◀️ Back", callback_data="admin_back")])
    await query.edit_message_text(
        "\n".join(lines), reply_markup=InlineKeyboardMarkup(kbrows), parse_mode=ParseMode.MARKDOWN,
    )


async def admin_add_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    lines = ["➕ *Add Coupons*\n━━━━━━━━━━━━━━━━━━━━\n"]
    for pk in STORE_PRODUCT_ORDER:
        p = PRODUCTS.get(pk, {})
        lines.append(f"*{p.get('name', pk)}* (`{pk}`):\n`/addcoupon {pk} CODE1 CODE2`\n")
    lines.append("_Multiple codes: space se alag karo._")
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]]),
        parse_mode=ParseMode.MARKDOWN,
    )


async def add_coupon_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    args = context.args
    if len(args) < 2:
        keys = ", ".join(f"`{k}`" for k in STORE_PRODUCT_ORDER)
        await update.message.reply_text(
            f"❌ *Usage:*\n`/addcoupon PRODUCT_ID CODE1 CODE2`\n\nValid IDs: {keys}",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    pk = args[0]
    if pk not in PRODUCTS:
        valid = ", ".join(f"`{k}`" for k in STORE_PRODUCT_ORDER)
        await update.message.reply_text(
            f"❌ Invalid product ID.\n\nValid IDs: {valid}", parse_mode=ParseMode.MARKDOWN,
        )
        return
    if pk in COMBO_PARTS:
        await update.message.reply_text(
            f"⚠️ *{PRODUCTS[pk]['name']}* is a combo — add codes to its parts:\n"
            + "\n".join(f"`/addcoupon {p} CODE1 CODE2`" for p in COMBO_PARTS[pk]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    new_codes = args[1:]
    coupons   = get_coupons()
    coupons.setdefault(pk, []).extend(new_codes)
    save_coupons(coupons)
    await update.message.reply_text(
        f"✅ *{len(new_codes)} coupon(s) added!*\n\n"
        f"📦 Product: *{PRODUCTS[pk]['name']}*\n"
        f"📊 Total Stock: *{len(coupons[pk])}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_broadcast_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    context.user_data["broadcast_mode"] = True
    await query.edit_message_text(
        "📢 *Broadcast Message*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        "📝 *Type your message now.*\n"
        "Or use: `/broadcast Your message here`",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Cancel", callback_data="admin_back")]]),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Text message handler (custom qty + broadcast) ───────────────

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    # Admin broadcast via panel
    if user.id == ADMIN_ID and context.user_data.get("broadcast_mode"):
        context.user_data.pop("broadcast_mode", None)
        users   = get_users()
        success = failed = 0
        for uid in users:
            try:
                await context.bot.send_message(
                    chat_id=int(uid),
                    text=update.message.text,
                )
                success += 1
            except Exception:
                failed += 1
        await update.message.reply_text(
            f"📢 *Broadcast Done!*\n\n✅ Sent: *{success}*\n❌ Failed: *{failed}*",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Custom quantity input
    if context.user_data.get("awaiting_custom_qty"):
        product_key = context.user_data.get("selected_product")
        if not product_key:
            context.user_data.pop("awaiting_custom_qty", None)
            return

        text = update.message.text.strip()
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text(
                "❌ *Invalid input!*\n\nPlease enter a *positive whole number* (e.g. `3`).",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        context.user_data.pop("awaiting_custom_qty", None)
        await _confirm_quantity(update, context, product_key, int(text))
        return

    # User has a pending order but sent text instead of a photo
    if context.user_data.get("pending_product"):
        await update.message.reply_text(
            "📸 *Please send a payment screenshot* (photo) to proceed.\n\n"
            "Tap ❌ Cancel on the order message or use /start to restart.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ─────────────── Admin product management commands ───────────────

async def products_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/products — show all products with id, name, price."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    lines = ["📋 *Products List*", "━━━━━━━━━━━━━━━━━━━━", ""]
    for pk in STORE_PRODUCT_ORDER:
        p = PRODUCTS.get(pk, {})
        s = get_stock(pk)
        lines.append(f"`{pk}` → {p.get('name', pk)} → ₹{p.get('price', 0)}  [Stock: {s}]")
    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━━━",
        "*Edit commands:*",
        "`/set_name ID new name`",
        "`/set_price ID amount`",
        "`/set_desc ID description`",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def set_name_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/set_name PRODUCT_ID NEW NAME — update product display name."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ *Usage:* `/set_name PRODUCT_ID New Name Here`", parse_mode=ParseMode.MARKDOWN
        )
        return
    pk       = args[0]
    new_name = " ".join(args[1:])
    if pk not in PRODUCTS:
        await update.message.reply_text(f"❌ Unknown product: `{pk}`", parse_mode=ParseMode.MARKDOWN)
        return
    PRODUCTS[pk]["name"] = new_name
    save_products_config()
    await update.message.reply_text(
        f"✅ Name updated!\n\n`{pk}` → *{new_name}*", parse_mode=ParseMode.MARKDOWN
    )


async def set_price_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/set_price PRODUCT_ID PRICE — update product price."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "❌ *Usage:* `/set_price PRODUCT_ID 49`", parse_mode=ParseMode.MARKDOWN
        )
        return
    pk = args[0]
    if pk not in PRODUCTS:
        await update.message.reply_text(f"❌ Unknown product: `{pk}`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        new_price = int(args[1])
        if new_price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Price must be a positive integer.", parse_mode=ParseMode.MARKDOWN)
        return
    PRODUCTS[pk]["price"] = new_price
    save_products_config()
    await update.message.reply_text(
        f"✅ Price updated!\n\n`{pk}` → *₹{new_price}*", parse_mode=ParseMode.MARKDOWN
    )


async def set_desc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/set_desc PRODUCT_ID description text — update product description."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ *Usage:* `/set_desc PRODUCT_ID Description text here`", parse_mode=ParseMode.MARKDOWN
        )
        return
    pk       = args[0]
    new_desc = " ".join(args[1:])
    if pk not in PRODUCTS:
        await update.message.reply_text(f"❌ Unknown product: `{pk}`", parse_mode=ParseMode.MARKDOWN)
        return
    PRODUCTS[pk]["desc"] = new_desc
    save_products_config()
    await update.message.reply_text(
        f"✅ Description updated!\n\n`{pk}` → _{new_desc}_", parse_mode=ParseMode.MARKDOWN
    )


async def add_service_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/add_service ID PRICE EMOJI Name — add a new product to the store."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    args = context.args
    if len(args) < 4:
        await update.message.reply_text(
            "❌ *Usage:*\n`/add_service ID PRICE EMOJI Name`\n\n"
            "*Example:*\n`/add_service amazon_100 20 🛍️ Amazon ₹100 Gift Card`\n\n"
            "• ID: letters/numbers/underscore only (e.g. `amazon_100`)\n"
            "• PRICE: number in ₹\n"
            "• EMOJI: single emoji\n"
            "• Name: display name",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    pk    = args[0].strip().lower()
    price_str = args[1]
    emoji = args[2]
    name  = " ".join(args[3:])

    import re as _re
    if not _re.match(r'^[a-z0-9_]+$', pk):
        await update.message.reply_text(
            "❌ ID mein sirf *lowercase letters, numbers aur underscore* allowed hai.\n"
            "Example: `amazon_100`", parse_mode=ParseMode.MARKDOWN
        )
        return
    try:
        price = int(price_str)
        if price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Price ek positive number hona chahiye.", parse_mode=ParseMode.MARKDOWN)
        return
    if pk in PRODUCTS:
        await update.message.reply_text(
            f"⚠️ Service `{pk}` pehle se exist karti hai!\n"
            f"Price/name change karne ke liye `/set_price` ya `/set_name` use karo.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    PRODUCTS[pk] = {"name": name, "price": price, "emoji": emoji, "desc": ""}
    if pk not in STORE_PRODUCT_ORDER:
        STORE_PRODUCT_ORDER.append(pk)

    # Add empty coupon stock entry
    coupons = _load(COUPONS_FILE) or {}
    if pk not in coupons:
        coupons[pk] = []
        _save(COUPONS_FILE, coupons)

    save_products_config()
    await update.message.reply_text(
        f"✅ *Nai service add ho gayi!*\n\n"
        f"{emoji} `{pk}`\n"
        f"*Naam:* {name}\n"
        f"*Price:* ₹{price}\n\n"
        f"Stock add karne ke liye:\n`/addcoupon {pk} CODE1 CODE2 ...`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def del_service_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/del_service ID — remove a product from the store."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    args = context.args
    if len(args) != 1:
        await update.message.reply_text(
            "❌ *Usage:* `/del_service ID`\n\nExample: `/del_service amazon_100`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    pk = args[0].strip().lower()
    if pk not in PRODUCTS:
        await update.message.reply_text(
            f"❌ Service `{pk}` mil nahi rahi.\n\nSaari services dekhne ke liye `/products` use karo.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    default_keys = set(_DEFAULT_PRODUCTS.keys())
    if pk in default_keys:
        await update.message.reply_text(
            f"⛔ Default service `{pk}` delete nahi ho sakti.\n"
            "Agar store se hatani hai toh admin se manually edit karwao.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    del PRODUCTS[pk]
    if pk in STORE_PRODUCT_ORDER:
        STORE_PRODUCT_ORDER.remove(pk)

    save_products_config()
    await update.message.reply_text(
        f"🗑️ *Service delete ho gayi!*\n\n`{pk}` ab store mein nahi dikhegi.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Reward & Points Admin Commands ───────────────

async def del_reward_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/del_reward NAAM — delete entire reward + all unclaimed codes."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ *Usage:* `/del_reward REWARD_NAAM`\nExample: `/del_reward Bigbasket_Free`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    name = args[0]
    ok   = db_delete_reward(name)
    if ok:
        await update.message.reply_text(
            f"🗑️ *Reward delete ho gaya!*\n\n`{name}` aur uske saare unused codes hata diye gaye.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            f"❌ Reward `{name}` nahi mila.\nSaare rewards: `/admin` → 🎁 Rewards",
            parse_mode=ParseMode.MARKDOWN,
        )


async def deduct_points_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/deduct_points USER_ID POINTS — admin manually deduct points from a user."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "❌ *Usage:* `/deduct_points USER_ID POINTS`\n"
            "Example: `/deduct_points 6724474397 3`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    uid = args[0]
    try:
        pts = int(args[1])
        if pts <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Points ek positive number hona chahiye.", parse_mode=ParseMode.MARKDOWN)
        return
    before = db_get_points(uid)
    db_deduct_points(uid, pts)
    after = db_get_points(uid)
    await update.message.reply_text(
        f"✅ *Points deduct ho gaye!*\n\n"
        f"User: `{uid}`\n"
        f"Before: *{before} pts* → After: *{after} pts*\n"
        f"Deducted: *{pts} pts*",
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Flash Sale Commands ───────────────

async def flash_sale_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/flash_sale ID SALE_PRICE DURATION — start a flash sale. Duration: 30m, 1h, 2h30m"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    args = context.args
    if len(args) != 3:
        await update.message.reply_text(
            "❌ *Usage:*\n`/flash_sale ID SALE_PRICE DURATION`\n\n"
            "*Examples:*\n"
            "`/flash_sale myntra_199 25 30m`\n"
            "`/flash_sale chatgpt 39 1h`\n"
            "`/flash_sale bigbasket 10 2h30m`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    pk         = args[0].strip().lower()
    price_str  = args[1]
    dur_str    = args[2]

    if pk not in PRODUCTS:
        await update.message.reply_text(
            f"❌ Service `{pk}` nahi mili.\nSaari services: `/products`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    try:
        sale_price = int(price_str)
        if sale_price <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Sale price ek positive number hona chahiye.", parse_mode=ParseMode.MARKDOWN)
        return

    secs = _parse_duration(dur_str)
    if not secs:
        await update.message.reply_text(
            "❌ Duration format galat hai.\nExamples: `30m`, `1h`, `2h30m`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    original_price = PRODUCTS[pk]["price"]
    if sale_price >= original_price:
        await update.message.reply_text(
            f"⚠️ Sale price (₹{sale_price}) original price (₹{original_price}) se kam honi chahiye!",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    expires_at = _time_mod.time() + secs
    FLASH_SALES[pk] = {
        "sale_price":     sale_price,
        "original_price": original_price,
        "expires_at":     expires_at,
    }

    product_name = PRODUCTS[pk]["name"]
    countdown    = _flash_countdown(expires_at)

    # Schedule auto-end job
    if context.job_queue:
        context.job_queue.run_once(
            _flash_sale_expire_job,
            when=secs,
            data={"pk": pk, "original_price": original_price},
            name=f"flash_expire_{pk}",
        )

    # Broadcast to all users
    users = _load(USERS_FILE) or {}
    p = PRODUCTS[pk]
    broadcast_text = (
        f"⚡ *FLASH SALE SHURU!*\n\n"
        f"{p['emoji']} *{product_name}*\n"
        f"Normal Price: ₹{original_price}\n"
        f"*SALE Price: ₹{sale_price}* 🔥\n\n"
        f"⏳ Sirf *{countdown}* ke liye!\n\n"
        f"Jaldi karo — /start"
    )
    sent = 0
    for uid in list(users.keys()):
        try:
            await context.bot.send_message(chat_id=int(uid), text=broadcast_text, parse_mode=ParseMode.MARKDOWN)
            sent += 1
        except Exception:
            pass

    await update.message.reply_text(
        f"✅ *Flash Sale Live!*\n\n"
        f"{p['emoji']} *{product_name}*\n"
        f"₹{original_price} → *₹{sale_price}*\n"
        f"⏳ Duration: {countdown}\n\n"
        f"📢 {sent} users ko notification bheja gaya!",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _flash_sale_expire_job(context) -> None:
    """Called by job_queue when flash sale expires."""
    data = context.job.data
    pk   = data.get("pk")
    if pk and pk in FLASH_SALES:
        del FLASH_SALES[pk]
    logger.info(f"Flash sale expired: {pk}")


async def end_flash_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/end_flash ID — end a running flash sale early."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    args = context.args
    if len(args) != 1:
        await update.message.reply_text(
            "❌ *Usage:* `/end_flash SERVICE_ID`\nExample: `/end_flash myntra_199`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    pk = args[0].strip().lower()
    if pk not in FLASH_SALES:
        await update.message.reply_text(
            f"⚠️ `{pk}` ka koi active flash sale nahi hai.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    del FLASH_SALES[pk]
    # Remove pending expire job
    if context.job_queue:
        for job in context.job_queue.get_jobs_by_name(f"flash_expire_{pk}"):
            job.schedule_removal()
    await update.message.reply_text(
        f"✅ *Flash sale khatam!*\n`{pk}` wapis normal price pe aa gaya.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def list_flash_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/flash_list — show all active flash sales."""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    active = {pk: s for pk, s in FLASH_SALES.items() if s["expires_at"] > _time_mod.time()}
    if not active:
        await update.message.reply_text("ℹ️ Abhi koi active flash sale nahi hai.", parse_mode=ParseMode.MARKDOWN)
        return
    lines = ["⚡ *Active Flash Sales:*\n"]
    for pk, s in active.items():
        p = PRODUCTS.get(pk, {})
        lines.append(
            f"{p.get('emoji','🔹')} `{pk}` — *{p.get('name', pk)}*\n"
            f"   ₹{s['original_price']} → *₹{s['sale_price']}* | ⏳ {_flash_countdown(s['expires_at'])}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ─────────────── Main ───────────────

def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set!")
    if not ADMIN_ID:
        raise ValueError("TELEGRAM_ADMIN_ID environment variable not set!")

    # Restore JSON data from Replit DB if local files are missing (fresh deployment)
    restore_data_from_repldb()

    # Backup current local data to Replit DB (so next deploy can restore it)
    backup_data_to_repldb()

    # Initialise SQLite referral database
    init_referral_db()

    # Ensure data files exist
    coupon_default = {k: [] for k in PRODUCTS}
    for fp, default in [
        (COUPONS_FILE,  coupon_default),
        (USERS_FILE,    {}),
        (ORDERS_FILE,   {}),
        (PENDING_FILE,  {}),
        (PRODUCTS_FILE, PRODUCTS),
    ]:
        if not os.path.exists(fp):
            _save(fp, default)

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("referral",   referral_command))
    app.add_handler(CommandHandler("admin",      admin_panel))
    app.add_handler(CommandHandler("addcoupon",  add_coupon_command))
    app.add_handler(CommandHandler("approve",    approve_command))
    app.add_handler(CommandHandler("broadcast",  broadcast_command))
    # Product management
    app.add_handler(CommandHandler("products",   products_command))
    app.add_handler(CommandHandler("set_name",    set_name_command))
    app.add_handler(CommandHandler("set_price",   set_price_command))
    app.add_handler(CommandHandler("set_desc",    set_desc_command))
    app.add_handler(CommandHandler("add_service",  add_service_command))
    app.add_handler(CommandHandler("del_service",  del_service_command))
    app.add_handler(CommandHandler("flash_sale",   flash_sale_command))
    app.add_handler(CommandHandler("end_flash",    end_flash_command))
    app.add_handler(CommandHandler("flash_list",   list_flash_command))
    # Rewards system (points-based referral)
    app.add_handler(CommandHandler("add_reward",     cmd_add_reward))
    app.add_handler(CommandHandler("del_reward",     del_reward_command))
    app.add_handler(CommandHandler("deduct_points",  deduct_points_command))
    app.add_handler(CommandHandler("add_coupon",     cmd_add_reward_coupon))
    app.add_handler(CommandHandler("del_coupon",     cmd_del_reward_coupon))
    app.add_handler(CommandHandler("list_coupons",   cmd_list_reward_coupons))

    # Inline buttons — buy & quantity
    app.add_handler(CallbackQueryHandler(support,              pattern="^support$"))
    app.add_handler(CallbackQueryHandler(back_to_start,        pattern="^back_to_start$"))
    app.add_handler(CallbackQueryHandler(verify_channel_join,  pattern="^verify_channel_join$"))
    app.add_handler(CallbackQueryHandler(verify_referral,      pattern="^verify_referral$"))
    app.add_handler(CallbackQueryHandler(referral_menu,        pattern="^referral_menu$"))
    app.add_handler(CallbackQueryHandler(my_points,            pattern="^my_points$"))
    app.add_handler(CallbackQueryHandler(redeem_points,        pattern="^redeem_points$"))
    app.add_handler(CallbackQueryHandler(do_redeem,            pattern="^do_redeem_"))
    app.add_handler(CallbackQueryHandler(buy_product,         pattern="^buy_"))
    app.add_handler(CallbackQueryHandler(custom_qty_prompt, pattern="^qty_custom$"))
    app.add_handler(CallbackQueryHandler(select_quantity,   pattern=r"^qty_\d+$"))
    app.add_handler(CallbackQueryHandler(cancel_order,      pattern="^cancel_order$"))

    # Approve / reject buttons
    app.add_handler(CallbackQueryHandler(approve_order_btn,  pattern="^approve_"))
    app.add_handler(CallbackQueryHandler(reject_order_btn,   pattern="^reject_(?!text_)"))
    app.add_handler(CallbackQueryHandler(reject_text_order,  pattern="^reject_text_"))

    # Admin panel navigation
    app.add_handler(CallbackQueryHandler(admin_stock,            pattern="^admin_stock$"))
    app.add_handler(CallbackQueryHandler(admin_stats,            pattern="^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_users,            pattern="^admin_users$"))
    app.add_handler(CallbackQueryHandler(admin_referrals,        pattern="^admin_referrals$"))
    app.add_handler(CallbackQueryHandler(admin_ref_detail,       pattern="^admin_ref_detail_"))
    app.add_handler(CallbackQueryHandler(admin_export_users,     pattern="^admin_export_users$"))
    app.add_handler(CallbackQueryHandler(admin_pending,          pattern="^admin_pending$"))
    app.add_handler(CallbackQueryHandler(admin_add_coupon,       pattern="^admin_add_coupon$"))
    app.add_handler(CallbackQueryHandler(admin_products_panel,   pattern="^admin_products$"))
    app.add_handler(CallbackQueryHandler(admin_broadcast_prompt, pattern="^admin_broadcast$"))
    app.add_handler(CallbackQueryHandler(admin_back,             pattern="^admin_back"))
    app.add_handler(CallbackQueryHandler(admin_rewards_panel,    pattern="^admin_rewards$"))
    app.add_handler(CallbackQueryHandler(admin_reward_delete,    pattern="^admin_rwd_del_"))
    app.add_handler(CallbackQueryHandler(admin_reward_detail,    pattern="^admin_rwd_"))

    # Media & text
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_screenshot))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    # Global error handler — logs every exception that occurs inside any handler
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Unhandled exception in handler", exc_info=context.error)
        if update and hasattr(update, "callback_query") and update.callback_query:
            try:
                await update.callback_query.answer("❌ An error occurred. Please try again.")
            except Exception:
                pass
    app.add_error_handler(error_handler)

    # ── Periodic referral validity check (every 6 minutes) ──
    if app.job_queue:
        app.job_queue.run_repeating(
            periodic_referral_check,
            interval=360,   # 6 minutes in seconds
            first=60,       # start 1 minute after bot launch
            name="referral_validity_check",
        )
        logger.info("✅ Periodic referral validity check job registered (every 6 min)")
    else:
        logger.warning("⚠️ JobQueue not available — periodic referral check disabled")

    logger.info("🤖 Bot fully started — all systems active!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


def acquire_pid_lock():
    """Ensure only one bot instance runs. Kill any previous instance."""
    import signal, atexit
    PID_FILE = "/tmp/coupon_bot.pid"
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            logger.info(f"Killing previous bot instance (PID {old_pid})")
            os.kill(old_pid, signal.SIGTERM)
            import time; time.sleep(3)
        except (ProcessLookupError, ValueError, PermissionError, OSError):
            pass
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.unlink(PID_FILE) if os.path.exists(PID_FILE) else None)
    logger.info(f"Bot PID lock acquired: {os.getpid()}")


if __name__ == "__main__":
    # Start Flask FIRST so Railway's port check passes immediately
    flask_port = int(os.environ.get("PORT", os.environ.get("FLASK_PORT", 3000)))
    threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=flask_port, threaded=True, use_reloader=False),
        daemon=True
    ).start()
    logger.info(f"🌐 Keep-alive server started on port {flask_port}")

    import time as _time; _time.sleep(1)  # Give Flask 1 sec to bind before bot starts

    # Kill any duplicate instance first — prevents 409 Conflict
    acquire_pid_lock()

    main()
