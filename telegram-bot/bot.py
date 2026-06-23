import os
import json
import sqlite3
from pymongo import MongoClient
import logging
import threading
import html
import urllib.request
import urllib.parse
import io
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
ADMIN_ID_2 = os.environ.get("TELEGRAM_ADMIN_ID_2", "")
ADMIN_IDS  = {ADMIN_ID} | ({int(ADMIN_ID_2)} if ADMIN_ID_2 else set())

UPI_ID         = "BHARATPE.8B0L1T2H8C56136@fbpe"  # UPI ID
SUPPORT_HANDLE = "@HypesSupport_bot"  # Support Telegram handle
QR_IMAGE_PATH  = os.path.join(os.path.dirname(__file__), "qr_code.jpg")

# ─────────────── ALOO BharatPe API Config ───────────────
ALOO_API_KEY     = os.environ.get("ALOO_API_KEY", "")
ALOO_MERCHANT_ID = os.environ.get("ALOO_MERCHANT_ID", "")
ALOO_BASE_URL    = "https://bharataalu.animeverse23.in/api/v1"
ALOO_POLL_INTERVAL = 5    # seconds between each poll
ALOO_MAX_POLLS     = 60   # max attempts (60 x 5s = 5 min timeout)

# ─────────────── ZapUPI API Config ───────────────
ZAPUPI_KEY          = os.environ.get("ZAPUPI_KEY", "")
ZAPUPI_BASE_URL     = "https://pay.zapupi.com"
ZAPUPI_POLL_INTERVAL = 5    # seconds between each poll
ZAPUPI_MAX_POLLS     = 60   # max attempts (60 x 5s = 5 min timeout)

# ─────────────── Dynamic UPI QR Generator ───────────────

def _generate_upi_qr(upi_id: str, amount: float, name: str = "Store") -> bytes:
    """Generate a QR code image (PNG bytes) for a UPI payment link with exact amount."""
    try:
        import qrcode
        # Use PIL backend (qrcode[pil] installed) — PyPNGImage fails silently on Render
        upi_link = (
            f"upi://pay?pa={urllib.parse.quote(upi_id, safe='@.')}"
            f"&am={amount:.2f}"
            f"&cu=INR"
        )
        logger.info(f"[QR] Generating dynamic QR for amount={amount:.2f} upi={upi_id}")
        qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
        qr.add_data(upi_link)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        logger.info(f"[QR] Dynamic QR generated successfully ({buf.getbuffer().nbytes} bytes)")
        return buf.read()
    except Exception as e:
        logger.error(f"[QR] generation FAILED: {type(e).__name__}: {e}")
        return None

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
RENDER_URL          = os.environ.get("RENDER_URL", "")      # e.g. https://yourapp.onrender.com

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
    """Initialize MongoDB collections (no-op — collections created on demand)."""
    try:
        db = _get_db()
        # Create indexes for performance
        db["referrals"].create_index("referred_by")
        db["waitlist"].create_index([("user_id", 1), ("product_key", 1)], unique=True)
        db["reward_coupons"].create_index([("reward_name", 1), ("code", 1)], unique=True)
        db["reward_coupons"].create_index([("reward_name", 1), ("used", 1)])
        logger.info("✅ MongoDB collections initialized!")
    except Exception as e:
        logger.error(f"MongoDB init error: {e}")


def db_is_banned(user_id: str) -> bool:
    return bool(_get_db()["banned_users"].find_one({"_id": str(user_id)}))

def db_ban_user(user_id: str, reason: str = "") -> None:
    _get_db()["banned_users"].replace_one(
        {"_id": str(user_id)},
        {"_id": str(user_id), "reason": reason, "banned_at": datetime.now(timezone.utc).isoformat()},
        upsert=True
    )

def db_unban_user(user_id: str) -> bool:
    result = _get_db()["banned_users"].delete_one({"_id": str(user_id)})
    return result.deleted_count > 0

def db_add_to_waitlist(user_id: str, product_key: str) -> bool:
    """Returns True if newly added, False if already on waitlist."""
    try:
        _get_db()["waitlist"].insert_one({
            "user_id": str(user_id), "product_key": product_key,
            "added_at": datetime.now(timezone.utc).isoformat()
        })
        return True
    except Exception:
        return False

def db_get_waitlist(product_key: str) -> list:
    return [doc["user_id"] for doc in _get_db()["waitlist"].find({"product_key": product_key})]

def db_clear_waitlist(product_key: str) -> int:
    result = _get_db()["waitlist"].delete_many({"product_key": product_key})
    return result.deleted_count

def db_get_referral(user_id: str):
    """Returns (user_id, referred_by, reward_given) or None."""
    doc = _get_db()["referrals"].find_one({"_id": str(user_id)})
    if doc:
        return (doc["_id"], doc.get("referred_by"), doc.get("reward_given", 0))
    return None

def db_insert_referral(user_id: str, referred_by: str, token: str = None) -> bool:
    """Insert referral. Returns True if new doc inserted."""
    try:
        _get_db()["referrals"].insert_one({
            "_id": str(user_id), "referred_by": str(referred_by),
            "reward_given": 0, "joined_at": datetime.now().isoformat(),
            "referral_status": "active", "ip_token": token
        })
        return True
    except Exception:
        return False

def db_get_referral_token(user_id: str) -> str | None:
    doc = _get_db()["referrals"].find_one({"_id": str(user_id)}, {"ip_token": 1})
    return doc.get("ip_token") if doc else None

def db_mark_reward_given(user_id: str):
    _get_db()["referrals"].update_one({"_id": str(user_id)}, {"$set": {"reward_given": 1}})

def db_successful_referral_count(referrer_id: str) -> int:
    """Count of ACTIVE verified referrals (reward_given=1, status=active) — this is the points value."""
    return _get_db()["referrals"].count_documents({
        "referred_by": str(referrer_id), "reward_given": 1, "referral_status": "active"
    })

def db_set_referral_status(user_id: str, status: str):
    """Set referral_status = 'active' or 'removed' for the referred user."""
    _get_db()["referrals"].update_one({"_id": str(user_id)}, {"$set": {"referral_status": status}})

def db_get_all_verified_referrals() -> list:
    """Return all verified referrals: [(user_id, referred_by, referral_status), ...]"""
    docs = _get_db()["referrals"].find({"reward_given": 1}, {"_id": 1, "referred_by": 1, "referral_status": 1})
    return [(d["_id"], d.get("referred_by"), d.get("referral_status", "active")) for d in docs]

def db_total_referral_count(referrer_id: str) -> int:
    """Count of all referrals (pending + verified) for this referrer."""
    return _get_db()["referrals"].count_documents({"referred_by": str(referrer_id)})

def db_get_referral_leaderboard() -> list:
    """Return list of (referrer_id, active_count, total_count) sorted by active_count desc."""
    pipeline = [
        {"$group": {
            "_id": "$referred_by",
            "active": {"$sum": {"$cond": [{"$and": [{"$eq": ["$reward_given", 1]}, {"$eq": ["$referral_status", "active"]}]}, 1, 0]}},
            "total": {"$sum": 1}
        }},
        {"$sort": {"active": -1, "total": -1}}
    ]
    docs = _get_db()["referrals"].aggregate(pipeline)
    return [(d["_id"], d["active"], d["total"]) for d in docs]

def db_get_referred_users_detail(referrer_id: str) -> list:
    """Return rows of (user_id, reward_given, referral_status, joined_at) for a referrer."""
    docs = _get_db()["referrals"].find(
        {"referred_by": str(referrer_id)},
        {"_id": 1, "reward_given": 1, "referral_status": 1, "joined_at": 1}
    ).sort("joined_at", -1)
    return [(d["_id"], d.get("reward_given", 0), d.get("referral_status", "active"), d.get("joined_at")) for d in docs]

# ── IP tracking helpers (shared file with api-server) ──
_IP_FILE = os.path.join(os.environ.get("BOT_DATA_DIR", _DEFAULT_DATA_DIR), "referral_ips.json")

def _load_ip_data() -> dict:
    doc = _get_db()["kv_store"].find_one({"_id": "ip_data"})
    return doc["data"] if doc else {"tokens": {}, "used_ips": []}

def _save_ip_data(data: dict) -> None:
    try:
        _get_db()["kv_store"].replace_one({"_id": "ip_data"}, {"_id": "ip_data", "data": data}, upsert=True)
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
        pipeline = [
            {"$match": {"user_id": str(user_id)}},
            {"$group": {"_id": None, "total": {"$sum": "$points"}}}
        ]
        result = list(_get_db()["points_spent"].aggregate(pipeline))
        spent = result[0]["total"] if result else 0
    except Exception:
        spent = 0
    return max(0, earned - spent)


def db_deduct_points(user_id: str, points: int, reason: str = "admin_deduction") -> None:
    """Admin: manually deduct points from a user."""
    _get_db()["points_spent"].insert_one({
        "user_id": str(user_id), "points": points,
        "reward_name": reason, "spent_at": datetime.now().isoformat()
    })


def db_delete_reward(reward_name: str) -> bool:
    """Admin: delete a reward and all its unclaimed coupons."""
    db = _get_db()
    r1 = db["rewards"].delete_one({"_id": reward_name})
    r2 = db["reward_coupons"].delete_many({"reward_name": reward_name, "used": 0})
    return (r1.deleted_count + r2.deleted_count) > 0

# ── Rewards CRUD ──
def db_add_reward(reward_name: str, points_required: int) -> bool:
    try:
        _get_db()["rewards"].replace_one(
            {"_id": reward_name},
            {"_id": reward_name, "points_required": points_required, "created_at": datetime.now().isoformat()},
            upsert=True
        )
        return True
    except Exception:
        return False

def db_list_rewards() -> list:
    """Returns list of (reward_name, points_required, stock) sorted by points_required."""
    db = _get_db()
    docs = db["rewards"].find({}, {"_id": 1, "points_required": 1}).sort("points_required", 1)
    result = []
    for doc in docs:
        stock = db["reward_coupons"].count_documents({"reward_name": doc["_id"], "used": 0})
        result.append({"name": doc["_id"], "points": doc.get("points_required", 0), "stock": stock})
    return result

def db_add_reward_coupon(reward_name: str, code: str) -> bool:
    try:
        _get_db()["reward_coupons"].insert_one({"reward_name": reward_name, "code": code, "used": 0})
        return True
    except Exception:
        return False

def db_delete_reward_coupon(reward_name: str, code: str) -> bool:
    """Admin: delete a specific coupon code from a reward pool."""
    result = _get_db()["reward_coupons"].delete_one({"reward_name": reward_name, "code": code, "used": 0})
    return result.deleted_count > 0

def db_list_reward_coupons(reward_name: str) -> list:
    """Admin: list all unused coupon codes for a reward."""
    docs = _get_db()["reward_coupons"].find({"reward_name": reward_name, "used": 0})
    return [d["code"] for d in docs]

def db_redeem_reward(user_id: str, reward_name: str) -> tuple | None:
    """Reserve 1 coupon for the user. Returns (code_id, code) or None if no stock."""
    from bson import ObjectId
    doc = _get_db()["reward_coupons"].find_one_and_update(
        {"reward_name": reward_name, "used": 0},
        {"$set": {"used": 1, "used_by": str(user_id), "used_at": datetime.now().isoformat()}},
        return_document=True
    )
    if doc:
        return (str(doc["_id"]), doc["code"])
    return None


def db_rollback_redeem(code_id) -> None:
    """Rollback a reserved coupon — mark it unused again."""
    try:
        from bson import ObjectId
        _get_db()["reward_coupons"].update_one(
            {"_id": ObjectId(str(code_id))},
            {"$set": {"used": 0, "used_by": None, "used_at": None}}
        )
    except Exception as e:
        logger.warning(f"db_rollback_redeem: {e}")

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

# ─────────────── Payment files ───────────────
SETTINGS_FILE         = os.path.join(_DATA_DIR, "auto_settings.json")
USED_AMOUNTS_FILE     = os.path.join(_DATA_DIR, "used_amounts.json")      # Anti-fraud: track used unique amounts
DEPOSITS_LOG_FILE     = os.path.join(_DATA_DIR, "deposits_log.json")      # Last 50 deposits
CUSTOM_QR_PATH        = os.path.join(_DATA_DIR, "custom_qr.jpg")          # Admin-uploaded QR

ORDER_TIMEOUT_SECONDS  = 600   # 10 min auto-cancel
EXIT_TRAP_SECONDS      = 180   # (DISABLED — kept for backward compat)
FAST_PAYMENT_THRESHOLD = 120   # < 2 min = 🔥 fast
LOW_STOCK_THRESHOLD    = 5

# ─────────────── Products Config (dynamic — admin can edit via /set_name etc) ───────────────

PRODUCTS_FILE = os.path.join(_DATA_DIR, "products_config.json")

_DEFAULT_PRODUCTS: dict = {}

# Order in which products appear in the store (dynamic — loaded from file)
STORE_PRODUCT_ORDER: list = []  # filled after _load_product_order_from_file() is defined

# Combo product → sub-products whose stock it draws from (dynamic — loaded from file)
COMBO_PARTS: dict = {}


def _load_products_from_file() -> dict:
    """Load products config from MongoDB."""
    data = load_json(PRODUCTS_FILE, {})
    if data:
        return {k: v for k, v in data.items() if k not in ("__order__", "__combos__")}
    return {}


def _load_product_order_from_file() -> list:
    """Load STORE_PRODUCT_ORDER from MongoDB."""
    data = load_json(PRODUCTS_FILE, {})
    return data.get("__order__", []) if data else []


def _load_combo_parts_from_file() -> dict:
    """Load COMBO_PARTS from MongoDB."""
    data = load_json(PRODUCTS_FILE, {})
    return data.get("__combos__", {}) if data else {}


PRODUCTS: dict = {}
STORE_PRODUCT_ORDER: list = []
COMBO_PARTS: dict = {}

def load_startup_data():
    global PRODUCTS, STORE_PRODUCT_ORDER, COMBO_PARTS
    # Load from MongoDB / fallback to local file cache
    loaded_products = _load_products_from_file()
    PRODUCTS.clear()
    PRODUCTS.update(loaded_products)

    loaded_order = _load_product_order_from_file()
    STORE_PRODUCT_ORDER.clear()
    STORE_PRODUCT_ORDER.extend(loaded_order)

    loaded_combos = _load_combo_parts_from_file()
    COMBO_PARTS.clear()
    COMBO_PARTS.update(loaded_combos)
    logger.info(f"✅ Loaded {len(PRODUCTS)} products from storage.")


def save_products_config() -> None:
    """Persist current PRODUCTS dict + order + combos to file."""
    data = dict(PRODUCTS)
    data["__order__"] = list(STORE_PRODUCT_ORDER)
    data["__combos__"] = dict(COMBO_PARTS)
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


def get_min_qty(product_key: str) -> int:
    """Returns minimum order quantity for a product. Default = 1.

    Admin can override per-product via the Min Quantity admin panel.
    Stored in PRODUCTS[product_key]["min_qty"].
    """
    try:
        v = int(PRODUCTS.get(product_key, {}).get("min_qty", 1) or 1)
        return max(1, v)
    except (TypeError, ValueError):
        return 1


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

def _self_ping():
    """Ping self every 14 minutes to prevent Render free-tier sleep."""
    import time as _time
    _time.sleep(60)  # wait 1 min after startup
    while True:
        try:
            if RENDER_URL:
                req = urllib.request.Request(
                    RENDER_URL.rstrip("/") + "/",
                    headers={"User-Agent": "SelfPing/1.0"},
                )
                urllib.request.urlopen(req, timeout=10)
                logger.info("[SelfPing] ✅ Pinged self successfully")
            else:
                logger.warning("[SelfPing] RENDER_URL not set — skipping ping")
        except Exception as e:
            logger.warning(f"[SelfPing] failed: {e}")
        _time.sleep(14 * 60)  # 14 minutes


def keep_alive():
    port = int(os.environ.get("PORT", 3000))
    threading.Thread(target=_self_ping, daemon=True).start()
    flask_app.run(host="0.0.0.0", port=port)


# ─────────────── Replit DB (persistent KV — survives deployments) ───────────────

# ─────────────── MongoDB Storage Layer ───────────────
_MONGO_URI = os.environ.get("MONGODB_URI", "")
_mongo_db  = None

def _get_db():
    global _mongo_db
    if _mongo_db is None:
        if not _MONGO_URI:
            logger.error("MONGODB_URI not set! Data will not persist across restarts.")
            raise RuntimeError("MONGODB_URI is required")
        client = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=3000)
        _mongo_db = client["hypes_zone_bot"]
        logger.info("✅ MongoDB connected!")
    return _mongo_db

def _fp_to_key(fp: str) -> str:
    """Convert file path to a short MongoDB doc key."""
    base = os.path.basename(fp)
    name = base.replace(".json", "").replace("_orders", "_orders").replace("pending_orders", "pending")
    return name

def _file_to_db_key(fp: str) -> str:
    return _fp_to_key(fp)

def _file_to_db_key_OLD(fp: str) -> str:
    for name, key in {}.items():
        if name in fp:
            return key
    return None

def _repldb_set(key: str, value: str) -> None:
    pass  # replaced by MongoDB

def _repldb_set_OLD(key: str, value: str) -> None:
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
    return None  # replaced by MongoDB

def _repldb_get_OLD(key: str) -> str:
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
    # Try MongoDB first if URI is configured
    if _MONGO_URI:
        try:
            key = _fp_to_key(fp)
            doc = _get_db()["kv_store"].find_one({"_id": key})
            if doc:
                mongodb_data = doc["data"]
                # Sync cache to local JSON file
                try:
                    tmp = fp + ".tmp"
                    os.makedirs(os.path.dirname(fp), exist_ok=True)
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(mongodb_data, f, indent=2, ensure_ascii=False)
                    os.replace(tmp, fp)
                except Exception as le:
                    logger.warning(f"Failed to sync MongoDB data to local cache {fp}: {le}")
                return mongodb_data
        except Exception as e:
            logger.warning(f"[MongoDB] load_json failed for {fp}: {e}. Falling back to local file.")

    # Fallback to local file if MongoDB fails or is not configured
    try:
        if os.path.exists(fp):
            with open(fp, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"Failed to load local JSON file {fp}: {e}")

    return default


def _save(fp: str, data) -> None:
    """Save data to both local JSON (for cache/fallback) and MongoDB."""
    # 1. Save locally (always)
    try:
        tmp = fp + ".tmp"
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, fp)
    except Exception as e:
        logger.error(f"Failed to save local file {fp}: {e}")

    # 2. Save to MongoDB if URI is configured
    if _MONGO_URI:
        try:
            key = _fp_to_key(fp)
            _get_db()["kv_store"].replace_one({"_id": key}, {"_id": key, "data": data}, upsert=True)
        except Exception as e:
            logger.error(f"[MongoDB] _save failed for {fp}: {e}")


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

    reward  = eligible[0]
    result  = db_redeem_reward(referrer_id, reward["name"])  # returns (code_id, code) or None

    if result:
        code_id, code = result
        # Step 1: Send code to user FIRST
        delivered = False
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
            delivered = True
        except Exception as e:
            logger.error(f"Referral reward send failed for {referrer_id}: {e}")
            # Rollback — mark code as unused again so it can be sent later
            db_rollback_redeem(code_id)
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"⚠️ Reward delivery FAILED!\n"
                        f"User: {referrer_id} ({referrer_name})\n"
                        f"Code: {code} — ROLLED BACK (not deducted)\n"
                        f"Error: {e}\n\n"
                        f"Use /force_reward {referrer_id} to retry."
                    ),
                )
            except Exception:
                pass
            return

        # Step 2: Only deduct points AFTER successful delivery
        if delivered:
            db_deduct_points(referrer_id, reward["points"], reward["name"])
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=(
                        f"🎁 Reward delivered!\n"
                        f"User: {referrer_id} ({referrer_name})\n"
                        f"Reward: {reward['name']}\n"
                        f"Code: {code}\n"
                        f"Points used: {reward['points']}"
                    ),
                )
            except Exception:
                pass
    else:
        # No stock — notify both
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
                text=(
                    f"⚠️ Reward PENDING — stock khatam!\n"
                    f"User: {referrer_id} ({referrer_name})\n"
                    f"Reward: {reward['name']}\n"
                    f"Stock add karo: /add_coupon {reward['name']} CODE1 CODE2"
                ),
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
        "🛒 *HYPES ZONE*",
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
    keyboard.append([InlineKeyboardButton("📞 Contact Support", url="https://t.me/HypesSupport_bot")])
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

    if db_is_banned(uid):
        await update.message.reply_text(
            "⛔ *Aapka account ban kar diya gaya hai.*\n\nKisi galti ke liye contact karein: " + SUPPORT_HANDLE,
            parse_mode=ParseMode.MARKDOWN,
        )
        return

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
            "🎁 *Dost ne bheja hai tujhe HYPES ZONE pe!*\n\n"
            "Referral reward activate karne ke liye aur store unlock karne ke liye\n*teeno channels join kar* — seedha maal milega! 💸\n\n"
            if has_referral else
            "🔥 *Bhai aa gaya HYPES ZONE pe!*\n\n"
            "Yahan milte hain *sabse saste coupons* — Myntra, BigBasket aur bahut kuch! 💥\n\n"
            "Ek kaam kar, pehle *3 channels join kar* — 30 second ka kaam hai:\n\n"
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
                "✅ *HYPES ZONE pe welcome hai tu!* 🔥\n\n"
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
            "✅ *Sahi hai bhai! HYPES ZONE pe welcome hai tu* 🔥\n\n"
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
    doc = _get_db()["referrals"].find_one({"_id": str(user_id)}, {"referral_status": 1})
    current_status = doc.get("referral_status", "active") if doc else "active"
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
    user  = update.effective_user
    uid   = str(user.id)
    reward_name = query.data[len("do_redeem_"):]

    logger.info(f"do_redeem triggered: user={uid} reward={reward_name}")

    # Check reward exists
    reward_row = next((r for r in db_list_rewards() if r["name"] == reward_name), None)
    if not reward_row:
        await query.answer("Reward nahi mila!", show_alert=True)
        return

    # Check points
    points = db_get_points(uid)
    if points < reward_row["points"]:
        await query.answer(
            f"Points kam hain! Tumhare paas {points} hain, chahiye {reward_row['points']}.",
            show_alert=True
        )
        return

    # Reserve coupon
    result = db_redeem_reward(uid, reward_name)
    if not result:
        await query.answer("Stock khatam! Admin se contact karo.", show_alert=True)
        return
    code_id, code = result

    logger.info(f"do_redeem: code reserved code_id={code_id} code={code} for user={uid}")

    # Deduct points FIRST (before send, so we don't double-send on retry)
    db_deduct_points(uid, reward_row["points"], reward_name)

    # Answer callback query once
    await query.answer("✅ Code mil gaya!")

    # Send via new message (more reliable than edit)
    remaining = max(0, points - reward_row["points"])
    msg_text = (
        f"🎉 Redemption Successful!\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎁 {reward_name}\n\n"
        f"🔑 Your Code:\n\n"
        f"{code}\n\n"
        f"✅ {reward_row['points']} points use hue.\n"
        f"📊 Bacha points: {remaining}\n\n"
        f"💬 Help: {SUPPORT_HANDLE}"
    )
    try:
        await context.bot.send_message(chat_id=int(uid), text=msg_text)
        logger.info(f"do_redeem: delivered to user={uid}")
    except Exception as e:
        logger.error(f"do_redeem: send_message failed user={uid} err={e}")
        # Rollback both code and points on failure
        db_rollback_redeem(code_id)
        db_deduct_points(uid, -reward_row["points"], f"rollback_{reward_name}")
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text="❌ Code deliver nahi hua. Dobara try karo ya support se contact karo.",
            )
        except Exception:
            pass

    # Admin notify
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🎁 Reward Redeemed!\nUser: {uid} ({user.first_name})\nReward: {reward_name}\nCode: {code}",
        )
    except Exception:
        pass


# ─────────────── Admin: /add_reward ───────────────

async def cmd_add_reward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /add_reward POINTS NAME"""
    if update.effective_user.id not in ADMIN_IDS:
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
    if update.effective_user.id not in ADMIN_IDS:
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
    if update.effective_user.id not in ADMIN_IDS:
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
    if update.effective_user.id not in ADMIN_IDS:
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
        waitlist_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔔 Notify Me When Available", callback_data=f"waitlist_{product_key}")],
            [InlineKeyboardButton("◀️ Back", callback_data="back_to_start")],
        ])
        await query.edit_message_text(
            f"😔 *Out of Stock!*\n\n"
            f"*{PRODUCTS[product_key]['name']}* abhi available nahi hai.\n\n"
            f"🔔 *Notify Me* press karo — jab stock aayega toh hum turant batayenge!",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=waitlist_kb,
        )
        return

    context.user_data["selected_product"] = product_key

    min_qty = get_min_qty(product_key)

    # Build quantity grid. Skip buttons below min_qty so user can't tap an
    # invalid amount. If min_qty > 10, show only the Custom Quantity button.
    row1 = [
        InlineKeyboardButton(QTY_EMOJIS[q], callback_data=f"qty_{product_key}_{q}")
        for q in range(max(1, min_qty), 6) if q <= stock
    ]
    row2 = [
        InlineKeyboardButton(QTY_EMOJIS[q], callback_data=f"qty_{product_key}_{q}")
        for q in range(max(6, min_qty), 11) if q <= stock
    ]
    keyboard = []
    if row1: keyboard.append(row1)
    if row2: keyboard.append(row2)
    keyboard.append([InlineKeyboardButton("✏️ Custom Quantity", callback_data=f"qty_custom_{product_key}")])
    keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="back_to_start")])

    display_price = get_unit_price(product_key, 1)
    sale          = _get_active_flash_sale(product_key)
    if sale:
        price_line = (
            f"💰 Price per unit: ~~₹{sale['original_price']}~~ → *₹{display_price}* ⚡ FLASH SALE! ({_flash_countdown(sale['expires_at'])})\n"
        )
    else:
        price_line = f"💰 Price per unit: *₹{display_price}*\n"

    min_line  = f"📐 Min order: *{min_qty}*\n" if min_qty > 1 else ""
    desc_line = f"📝 {product.get('desc', '')}\n" if product.get("desc") else ""
    terms_line = (
        f"📜 *T&C:* _{product.get('terms', '')}_\n"
        if product.get("terms") else ""
    )
    combo_info = ""
    if product_key in COMBO_PARTS:
        parts_names = " + ".join(PRODUCTS.get(p, {}).get("name", p) for p in COMBO_PARTS[product_key])
        combo_info = f"🎁 *Combo:* {parts_names}\n"

    await query.edit_message_text(
        f"📦 *Select Quantity*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{product['emoji']} *{product['name']}*\n"
        + desc_line
        + combo_info
        + price_line
        + min_line
        + f"📊 Stock available: *{stock}*\n"
        + (terms_line if terms_line else "")
        + f"\nHow many do you want?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Custom quantity prompt ───────────────

async def custom_qty_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query       = update.callback_query
    await query.answer()
    # Parse product_key from callback_data: "qty_custom_{product_key}"
    raw = query.data  # e.g. "qty_custom_coupon_100"
    product_key = raw[len("qty_custom_"):] if raw.startswith("qty_custom_") else context.user_data.get("selected_product")
    product     = PRODUCTS.get(product_key) if product_key else None
    if not product:
        await query.edit_message_text("❌ Session expired. Please use /start.", parse_mode=ParseMode.MARKDOWN)
        return
    # Keep user_data in sync for text input handler
    context.user_data["selected_product"] = product_key

    stock = get_stock(product_key)
    min_qty = get_min_qty(product_key)
    context.user_data["awaiting_custom_qty"] = True

    min_line = f"📐 Min order: *{min_qty}*\n" if min_qty > 1 else ""

    await query.edit_message_text(
        f"✏️ *Enter Custom Quantity*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{product['emoji']} *{product['name']}*\n"
        + min_line
        + f"📊 Max available: *{stock}*\n\n"
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

    min_qty = get_min_qty(product_key)
    if quantity < min_qty:
        msg = (
            f"⚠️ *Minimum order is {min_qty}*\n\n"
            f"Is product ke liye kam se kam *{min_qty}* coupons lene padenge.\n"
            f"Use /start to choose again."
        )
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
            context.user_data[f"order_timer_{uid}"] = timer_job
            # Exit-trap nudge DISABLED per admin request
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
    if product.get("desc"):
        extra_tnc += f"\n📝 *{product['desc']}*"
    if product.get("terms"):
        extra_tnc += f"\n\n📜 *Terms & Conditions:*\n{product['terms']}"
    if extra_tnc:
        extra_tnc = "\n━━━━━━━━━━━━━━━━━━━━" + extra_tnc

    # ── Determine active payment method ──
    _active_method = get_active_payment_method()

    if _active_method == "none":
        msg = (
            "⚠️ *Payment Unavailable*\n\n"
            "Abhi koi bhi payment method active nahi hai.\n"
            f"Support se contact karo: {SUPPORT_HANDLE}"
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    elif _active_method == "zapupi":
        # ── ZapUPI Payment Flow ──
        order_id = f"{uid}_{int(now_ts())}"
        context.user_data["pending_amount"]   = float(total)
        context.user_data["pending_product"]  = product_key
        context.user_data["pending_quantity"] = quantity

        # Create order via ZapUPI API
        zap_result = _zapupi_create_order(order_id, total)
        if not zap_result or zap_result.get("status") != "success":
            err_msg = (zap_result or {}).get("message", "Unknown error")
            logger.error(f"[ZapUPI] Create order failed: {err_msg}")
            msg = (
                f"❌ *Payment gateway error.*\n\n`{err_msg}`\n\n"
                f"Support se contact karo: {SUPPORT_HANDLE}"
            )
            if update.callback_query:
                await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            return

        payment_url = zap_result.get("payment_url", "")
        context.user_data["zapupi_order_id"] = order_id

        # Save order record
        order_rec = {
            "order_id":   order_id,
            "user_id":    uid,
            "username":   update.effective_user.username or "",
            "first_name": update.effective_user.first_name,
            "product":    product_key,
            "quantity":   quantity,
            "total":      float(total),
            "status":     "pending",
            "priority":   "auto",
            "timestamp":  datetime.now().isoformat(),
            "via":        "zapupi",
        }
        orders = get_orders()
        orders[order_id] = order_rec
        save_orders(orders)
        pending_orders = get_pending()
        pending_orders[str(uid)] = order_id
        save_pending(pending_orders)

        # Start background polling
        if context.job_queue:
            context.bot_data.setdefault("zapupi_pending", {})[str(uid)] = {
                "order_id":  order_id,
                "amount":    float(total),
                "chat_id":   uid,
                "polls_done": 0,
            }
            # Register poll job if not already running
            existing = context.job_queue.get_jobs_by_name("zapupi_poll")
            if not existing:
                context.job_queue.run_repeating(
                    zapupi_poll_job,
                    interval=ZAPUPI_POLL_INTERVAL,
                    first=ZAPUPI_POLL_INTERVAL,
                    name="zapupi_poll",
                )

        summary_text = (
            f"🧾 *Order Summary*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎟 Product:    *{product['name']}*\n"
            f"{combo_note}"
            f"📦 Quantity:   *{qty_disp} × {quantity}*\n"
            f"💰 Unit Price: ₹{unit_price}\n"
            f"💵 *Total:     ₹{total:.2f}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📲 *Neeche button dabao aur payment karo*\n\n"
            f"✅ Payment hote hi coupon automatically deliver ho jayega."
            f"{extra_tnc}"
        )
        zap_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Pay Now (ZapUPI)", url=payment_url)],
            [InlineKeyboardButton("❌ Cancel Order", callback_data="cancel_order")],
        ])
        if update.callback_query:
            await update.callback_query.edit_message_text(
                summary_text, reply_markup=zap_kb, parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
                summary_text, reply_markup=zap_kb, parse_mode=ParseMode.MARKDOWN,
            )

    else:
        # ── ALOO Auto-Payment: generate unique amount, create order, start polling ──
        _settings   = get_settings()
        _timeout_m  = int(_settings.get("timeout_minutes", 5))
        _active_upi = get_active_upi()
        _active_qr  = get_active_qr_path()

        # Generate unique amount (base + paise suffix) for this user
        unique_amount = _generate_unique_amount(total)
        context.user_data["pending_amount"]   = unique_amount
        context.user_data["pending_product"]  = product_key
        context.user_data["pending_quantity"] = quantity

        # Create order record immediately
        order_id = f"{uid}_{int(now_ts())}"
        order_rec = {
            "order_id":   order_id,
            "user_id":    uid,
            "username":   update.effective_user.username or "",
            "first_name": update.effective_user.first_name,
            "product":    product_key,
            "quantity":   quantity,
            "total":      unique_amount,
            "status":     "pending",
            "priority":   "auto",
            "timestamp":  datetime.now().isoformat(),
            "via":        "aloo",
        }
        orders = get_orders()
        orders[order_id] = order_rec
        save_orders(orders)
        pending_orders = get_pending()
        pending_orders[str(uid)] = order_id
        save_pending(pending_orders)

        payment_instructions = (
            f"⚠️ *Exactly ₹{unique_amount:.2f} hi bhejo* — ye unique amount sirf aapke liye hai.\n\n"
            f"👇 Payment karne ke baad neeche *'✅ Maine Pay Kar Diya'* button dabao."
        )

        summary_text = (
            f"🧾 *Order Summary*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎟 Product:    *{product['name']}*\n"
            f"{combo_note}"
            f"📦 Quantity:   *{qty_disp} × {quantity}*\n"
            f"💰 Unit Price: ₹{unit_price}\n"
            f"💵 *Total:     ₹{unique_amount:.2f}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📲 *Payment Details*\n"
            f"🏦 UPI ID: `{_active_upi}`\n"
            f"💵 Pay exactly: *₹{unique_amount:.2f}*\n\n"
            f"{payment_instructions}"
            f"{extra_tnc}"
        )
        cancel_btn = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Maine Pay Kar Diya", callback_data=f"i_paid_{unique_amount:.2f}")],
            [InlineKeyboardButton("❌ Cancel Order",        callback_data="cancel_order")],
        ])

        # ── Generate dynamic UPI QR with exact unique amount ──
        qr_sent = False
        qr_bytes = _generate_upi_qr(_active_upi, unique_amount, "")
        if qr_bytes:
            try:
                qr_bio = io.BytesIO(qr_bytes)
                qr_bio.name = "payment_qr.png"
                if update.callback_query:
                    await context.bot.send_photo(
                        chat_id=uid, photo=qr_bio,
                        caption=summary_text, reply_markup=cancel_btn,
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    await update.callback_query.edit_message_text(
                        "👆 *See the message above for your order details.*",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                else:
                    await update.message.reply_photo(
                        photo=qr_bio, caption=summary_text,
                        reply_markup=cancel_btn, parse_mode=ParseMode.MARKDOWN,
                    )
                qr_sent = True
            except Exception as e:
                logger.warning(f"Dynamic QR send failed: {e}")

        # Fallback: static admin QR or text-only
        if not qr_sent:
            if os.path.exists(_active_qr):
                try:
                    with open(_active_qr, "rb") as qr_file:
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
                    logger.warning(f"Static QR send failed: {e}")

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
    """Handle quantity button. callback_data is qty_<product_key>_<number>."""
    query = update.callback_query
    await query.answer()

    # Parse product_key and quantity from callback_data (e.g. "qty_coupon_100_3")
    try:
        parts = query.data.split("_")  # ["qty", ..product_key parts.., "number"]
        quantity = int(parts[-1])
        product_key = "_".join(parts[1:-1])  # everything between "qty_" and "_number"
    except (IndexError, ValueError):
        await query.edit_message_text("❌ Invalid selection. Please use /start.", parse_mode=ParseMode.MARKDOWN)
        return

    if not product_key or product_key not in PRODUCTS:
        await query.edit_message_text("❌ Session expired. Please use /start.", parse_mode=ParseMode.MARKDOWN)
        return

    # Keep user_data in sync for custom_qty text handler
    context.user_data["selected_product"] = product_key

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

    # ── Admin uploading a custom QR code via panel ──
    if user.id in ADMIN_IDS and context.user_data.get("awaiting_qr_upload"):
        context.user_data.pop("awaiting_qr_upload", None)
        try:
            if update.message.photo:
                f = await update.message.photo[-1].get_file()
            elif update.message.document:
                f = await update.message.document.get_file()
            else:
                await update.message.reply_text("❌ Photo nahi mila.")
                return
            await f.download_to_drive(CUSTOM_QR_PATH)
            await update.message.reply_text(
                f"✅ *QR code updated!*\n\nSaved as custom QR. Ye ab sab orders me show hoga.",
                parse_mode=ParseMode.MARKDOWN,
            )
            logger.info(f"Custom QR uploaded by admin → {CUSTOM_QR_PATH}")
        except Exception as e:
            logger.error(f"QR upload failed: {e}")
            await update.message.reply_text(f"❌ Upload failed: {e}")
        return

    if user.id in ADMIN_IDS:
        return

    # ── Auto-payment ON: no screenshot needed ──
    if context.user_data.get("pending_product"):
        await update.message.reply_text(
            "⚡ *Automatic payment verification chal raha hai!*\n\n"
            "Screenshot ki zarurat nahi — payment receive hote hi coupon automatically deliver ho jayega.\n\n"
            f"Agar 5 minute mein verify nahi hua, support se contact karo: {SUPPORT_HANDLE}",
            parse_mode=ParseMode.MARKDOWN,
        )
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

    # ── Notify admin with exact coupon code(s) delivered ──
    try:
        admin_codes = "\n".join(f"<code>{html.escape(str(c))}</code>" for c in assigned)
        username_part = f"@{html.escape(order.get('username', ''))}" if order.get("username") else ""
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"🎟 <b>Coupon Delivered!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 User: <code>{order['user_id']}</code> {username_part}\n"
                f"📦 Product: <b>{html.escape(product.get('name', pk))}</b>  ×{quantity}\n"
                f"🔑 Code(s) diya:\n\n"
                f"{admin_codes}"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"Admin coupon notification failed: {e}")

    return order, assigned


# ─────────────── /approve <user_id> command ───────────────

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
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
    if query.from_user.id not in ADMIN_IDS:
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
    if query.from_user.id not in ADMIN_IDS:
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
    if query.from_user.id not in ADMIN_IDS:
        return
    order_id = query.data.replace("reject_text_", "")
    _, status = await _do_reject(context, order_id)
    msg = "✅ Rejected." if status == "ok" else "Not found or already processed."
    await query.answer(msg, show_alert=True)


# ─────────────── /broadcast <message> command ───────────────

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
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
            InlineKeyboardButton("🗑️ Remove Coupon",  callback_data="admin_remove_coupon"),
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
        # ─── ALOO BharatPe Payment section ───
        [
            InlineKeyboardButton("💳 Change UPI",      callback_data="admin_set_upi"),
            InlineKeyboardButton("📷 Change QR",       callback_data="admin_set_qr"),
        ],
        [
            InlineKeyboardButton("📊 Recent Deposits", callback_data="admin_recent_deposits"),
            InlineKeyboardButton("⏱ Set Timeout",      callback_data="admin_set_timeout"),
        ],
        [InlineKeyboardButton("📐 Min Quantity",       callback_data="admin_min_qty")],
        # ─── Payment Method Toggles ───
        [InlineKeyboardButton("💳 Payment Methods", callback_data="admin_payment_methods")],
    ])


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    await update.message.reply_text(
        _admin_text(), reply_markup=_admin_kb(), parse_mode=ParseMode.MARKDOWN,
    )


async def admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    context.user_data.pop("broadcast_mode", None)
    await query.edit_message_text(
        _admin_text(), reply_markup=_admin_kb(), parse_mode=ParseMode.MARKDOWN,
    )


async def admin_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
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
    """Show all products with name, price, stock — with action buttons."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return

    lines = ["📋 *Products Panel*", "━━━━━━━━━━━━━━━━━━━━", ""]
    if not STORE_PRODUCT_ORDER:
        lines.append("_(Koi service nahi hai abhi — neeche se add karo)_")
    for pk in STORE_PRODUCT_ORDER:
        p = PRODUCTS.get(pk, {})
        s = get_stock(pk)
        combo_tag = " 🔀COMBO" if pk in COMBO_PARTS else ""
        lines.append(f"{p.get('emoji','🔹')} *{p.get('name', pk)}*{combo_tag}\n   `{pk}` — ₹{p.get('price', 0)}  Stock: {s}")

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ Service Add Karo", callback_data="admin_create_service"),
            InlineKeyboardButton("🎁 Combo Banao",      callback_data="admin_combo_create"),
        ],
        [
            InlineKeyboardButton("✏️ Edit Service",    callback_data="admin_edit_svc_list"),
            InlineKeyboardButton("🗑️ Service Hatao",  callback_data="admin_del_service_list"),
        ],
        [InlineKeyboardButton("◀️ Back", callback_data="admin_back")],
    ])
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
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
    if query.from_user.id not in ADMIN_IDS:
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
    if query.from_user.id not in ADMIN_IDS:
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
    if query.from_user.id not in ADMIN_IDS:
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
    if query.from_user.id not in ADMIN_IDS:
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
    if query.from_user.id not in ADMIN_IDS:
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
    if query.from_user.id not in ADMIN_IDS:
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
    if query.from_user.id not in ADMIN_IDS:
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
    if query.from_user.id not in ADMIN_IDS:
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
    if query.from_user.id not in ADMIN_IDS:
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




# ─────────────── Admin: Remove Coupon (button-based) ───────────────

async def admin_remove_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show list of products with their coupon counts for removal."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    coupons = get_coupons()
    lines = ["🗑️ *Remove Coupon*\n━━━━━━━━━━━━━━━━━━━━\n"]
    lines.append("_Product select karo jiska coupon delete karna hai:_\n")
    kb = []
    for pk in STORE_PRODUCT_ORDER:
        p = PRODUCTS.get(pk, {})
        codes = coupons.get(pk, [])
        count = len(codes)
        label = f"{p.get('emoji','🎫')} {p.get('name', pk)} ({count} codes)"
        if count > 0:
            kb.append([InlineKeyboardButton(label, callback_data=f"admin_rmcoupon_sel_{pk}")])
        else:
            lines.append(f"• _{p.get('name', pk)}: No codes_")
    kb.append([InlineKeyboardButton("◀️ Back", callback_data="admin_back")])
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_rmcoupon_sel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show coupon codes for a selected product so admin can delete one."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    pk = query.data.replace("admin_rmcoupon_sel_", "")
    p = PRODUCTS.get(pk, {})
    coupons = get_coupons()
    codes = coupons.get(pk, [])
    if not codes:
        await query.edit_message_text(
            f"📭 *{p.get('name', pk)}* mein koi coupon nahi hai.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_remove_coupon")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    lines = [f"🗑️ *{p.get('name', pk)} — Coupons*\n━━━━━━━━━━━━━━━━━━━━"]
    lines.append(f"_Total: {len(codes)} codes. Delete karne ke liye select karo:_\n")
    kb = []
    for code in codes[:20]:  # max 20 show karo
        kb.append([InlineKeyboardButton(f"❌ {code}", callback_data=f"admin_rmcoupon_do_{pk}|{code}")])
    if len(codes) > 20:
        lines.append(f"_(Sirf pehle 20 dikh rahe hain, {len(codes)-20} aur hain)_")
    kb.append([InlineKeyboardButton("◀️ Back", callback_data="admin_remove_coupon")])
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_rmcoupon_do(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete the selected coupon code."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    data = query.data.replace("admin_rmcoupon_do_", "")
    pk, code = data.split("|", 1)
    p = PRODUCTS.get(pk, {})
    coupons = get_coupons()
    codes = coupons.get(pk, [])
    if code in codes:
        codes.remove(code)
        coupons[pk] = codes
        save_coupons(coupons)
        remaining = len(codes)
        await query.edit_message_text(
            f"✅ *Coupon Deleted!*\n\n"
            f"Product: *{p.get('name', pk)}*\n"
            f"Code: `{code}`\n"
            f"Remaining: *{remaining} codes*",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ Aur Delete Karo", callback_data=f"admin_rmcoupon_sel_{pk}")],
                [InlineKeyboardButton("◀️ Back", callback_data="admin_remove_coupon")],
            ]),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await query.edit_message_text(
            f"❌ Code `{code}` nahi mila *{p.get('name', pk)}* mein.\n(Shayad pehle se delete ho gaya)",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_remove_coupon")]]),
            parse_mode=ParseMode.MARKDOWN,
        )

async def add_coupon_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
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
    # Notify waitlisted users
    waitlist_users = db_get_waitlist(pk)
    notified = 0
    for wuid in waitlist_users:
        try:
            await context.bot.send_message(
                chat_id=int(wuid),
                text=f"🎉 *Stock Aa Gaya!*\n\n"
                     f"*{PRODUCTS[pk]['name']}* ab available hai!\n"
                     f"Jaldi karo — limited stock hai! 👇",
                parse_mode=ParseMode.MARKDOWN,
            )
            notified += 1
        except Exception:
            pass
    if waitlist_users:
        db_clear_waitlist(pk)
    await update.message.reply_text(
        f"✅ *{len(new_codes)} coupon(s) added!*\n\n"
        f"📦 Product: *{PRODUCTS[pk]['name']}*\n"
        f"📊 Total Stock: *{len(coupons[pk])}*" +
        (f"\n🔔 *{notified} users ko notify kar diya!*" if waitlist_users else ""),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Waitlist handler ───────────────
async def join_waitlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user = query.from_user
    uid  = str(user.id)
    product_key = query.data.replace("waitlist_", "")
    if db_is_banned(uid):
        await query.answer("⛔ Account banned.", show_alert=True)
        return
    product = PRODUCTS.get(product_key)
    if not product:
        await query.answer("Product nahi mila.", show_alert=True)
        return
    added = db_add_to_waitlist(uid, product_key)
    if added:
        await query.edit_message_text(
            f"✅ *Waitlist mein add ho gaye!*\n\n"
            f"Jab *{product['name']}* ka stock aayega, hum aapko turant message karenge. 🔔",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="back_to_start")]]),
        )
    else:
        await query.answer("🔔 Aap pehle se waitlist mein hain!", show_alert=True)


# ─────────────── Ban / Unban admin commands ───────────────
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ *Usage:* `/ban USER\\_ID reason`\n\nExample: `/ban 123456789 fake payment`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    target_id = args[0]
    reason    = " ".join(args[1:]) if len(args) > 1 else "No reason given"
    db_ban_user(target_id, reason)
    try:
        await context.bot.send_message(
            chat_id=int(target_id),
            text=f"⛔ *Aapka account ban kar diya gaya hai.*\nReason: {reason}\n\nContact: {SUPPORT_HANDLE}",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        pass
    await update.message.reply_text(
        f"✅ *User {target_id} ban kar diya gaya!*\nReason: {reason}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    args = context.args
    if not args:
        await update.message.reply_text("❌ *Usage:* `/unban USER\\_ID`", parse_mode=ParseMode.MARKDOWN)
        return
    target_id = args[0]
    success   = db_unban_user(target_id)
    if success:
        try:
            await context.bot.send_message(
                chat_id=int(target_id),
                text="✅ *Aapka ban hata diya gaya hai!* Ab aap bot use kar sakte hain.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        await update.message.reply_text(f"✅ *User {target_id} unban ho gaya!*", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"❌ User {target_id} ban list mein nahi tha.", parse_mode=ParseMode.MARKDOWN)


async def admin_broadcast_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
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

    # ── Admin: handle awaiting prompts for auto-payment settings ──
    if user.id in ADMIN_IDS:
        text = (update.message.text or "").strip()

        if context.user_data.pop("awaiting_upi", False):
            s = get_settings()
            s["custom_upi_id"] = text
            save_settings(s)
            await update.message.reply_text(
                f"✅ UPI ID updated: `{text}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        if context.user_data.pop("awaiting_timeout", False):
            try:
                m = int(text)
                if m < 1 or m > 60:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("❌ Number 1-60 ke beech do.")
                return
            s = get_settings()
            s["timeout_minutes"] = m
            save_settings(s)
            await update.message.reply_text(f"✅ Timeout set: *{m} minutes*", parse_mode=ParseMode.MARKDOWN)
            return

        # ── Min Quantity edit (per-product) ──
        edit_key = context.user_data.pop("awaiting_min_qty_edit", None)
        if edit_key:
            try:
                v = int(text)
                if v < 1 or v > 1000:
                    raise ValueError
            except ValueError:
                await update.message.reply_text(
                    "❌ Number 1-1000 ke beech do (1 = no limit).",
                )
                # Re-arm so admin can retry without re-clicking
                context.user_data["awaiting_min_qty_edit"] = edit_key
                return
            if edit_key not in PRODUCTS:
                await update.message.reply_text("❌ Product nahi mila.")
                return
            PRODUCTS[edit_key]["min_qty"] = v
            save_products_config()
            pname = PRODUCTS[edit_key].get("name", edit_key)
            await update.message.reply_text(
                f"✅ *Min quantity updated*\n\n"
                f"{PRODUCTS[edit_key].get('emoji', '📦')} {pname}\n"
                f"📐 New min: *{v}*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ Back to Min Qty Panel", callback_data="admin_min_qty"),
                ]]),
            )
            return

        if context.user_data.pop("awaiting_search_utr", False):
            await _do_search_utr(update, context, text)
            return

        if context.user_data.pop("awaiting_zapupi_key", False):
            s = get_settings()
            s["zapupi_key"] = text.strip()
            save_settings(s)
            await update.message.reply_text(
                f"✅ *ZapUPI API key save ho gaya!*\n\n"
                f"`{text.strip()[:6]}...{text.strip()[-4:] if len(text.strip()) > 10 else ''}`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # ── Edit service field ──
        edit_field = context.user_data.pop("awaiting_edit_svc_field", None)
        if edit_field:
            pk = context.user_data.get("edit_svc_pk", "")
            p  = PRODUCTS.get(pk, {})
            if not p:
                await update.message.reply_text("❌ Service nahi mili. Dobara try karo.", parse_mode=ParseMode.MARKDOWN)
                return
            back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("✏️ Edit Panel", callback_data=f"admin_edit_svc_sel_{pk}")]])
            if edit_field == "name":
                PRODUCTS[pk]["name"] = text
                save_products_config()
                await update.message.reply_text(f"✅ *Naam update ho gaya!*\n\n`{pk}` → *{text}*", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb)
            elif edit_field == "price":
                try:
                    new_price = int(text.strip())
                    if new_price <= 0:
                        raise ValueError
                except ValueError:
                    await update.message.reply_text("❌ Valid price do (positive number). Dobara type karo:")
                    context.user_data["awaiting_edit_svc_field"] = edit_field
                    return
                PRODUCTS[pk]["price"] = new_price
                save_products_config()
                await update.message.reply_text(f"✅ *Price update ho gaya!*\n\n`{pk}` → ₹{new_price}", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb)
            elif edit_field == "desc":
                PRODUCTS[pk]["desc"] = text.strip()
                save_products_config()
                await update.message.reply_text(f"✅ *Description update ho gayi!*\n\n_{text.strip()}_", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb)
            elif edit_field == "terms":
                PRODUCTS[pk]["terms"] = "" if text.strip().lower() == "none" else text.strip()
                save_products_config()
                await update.message.reply_text(f"✅ *Terms update ho gayi!*", parse_mode=ParseMode.MARKDOWN, reply_markup=back_kb)
            return

        # ── Step-by-step service / combo creation ──
        svc_step = context.user_data.get("awaiting_service_step")
        if svc_step:
            import re as _re
            ns = context.user_data.setdefault("new_service", {})

            cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_products")]])

            if svc_step == "id":
                pk = text.strip().lower()
                if not _re.match(r'^[a-z0-9_]+$', pk):
                    await update.message.reply_text(
                        "❌ ID mein sirf *lowercase letters, numbers aur underscore* allowed hai.\nDobara type karo:",
                        parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb,
                    )
                    return
                if pk in PRODUCTS:
                    await update.message.reply_text(
                        f"❌ `{pk}` pehle se exist karta hai! Alag ID do:",
                        parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb,
                    )
                    return
                ns["id"] = pk
                context.user_data["awaiting_service_step"] = "price"
                await update.message.reply_text(
                    f"✅ ID: `{pk}`\n\n*Step 2/5 — Price (₹)*\n\nKitne rupaye mein sell karni hai? (sirf number)\nExample: `49`",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb,
                )
                return

            if svc_step == "price":
                try:
                    price = int(text.strip())
                    if price <= 0:
                        raise ValueError
                except ValueError:
                    await update.message.reply_text("❌ Valid price do (positive number):", reply_markup=cancel_kb)
                    return
                ns["price"] = price
                context.user_data["awaiting_service_step"] = "name"
                await update.message.reply_text(
                    f"✅ Price: ₹{price}\n\n*Step 3/5 — Service Name*\n\nKya naam rakhna hai?\nExample: `Amazon ₹100 Gift Card`",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb,
                )
                return

            if svc_step == "name":
                ns["name"] = text.strip()
                context.user_data["awaiting_service_step"] = "desc"
                await update.message.reply_text(
                    f"✅ Naam: {text.strip()}\n\n*Step 4/5 — Description*\n\nChhoti si description do:\nExample: `₹100 off on orders above ₹299`",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb,
                )
                return

            if svc_step == "desc":
                ns["desc"] = text.strip()
                context.user_data["awaiting_service_step"] = "terms"
                await update.message.reply_text(
                    f"✅ Description save!\n\n*Step 5/5 — Terms & Conditions*\n\nKoi terms batao (ya 'none' type karo agar nahi hain):\nExample: `Valid on app only. One time use.`",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb,
                )
                return

            if svc_step == "terms":
                ns["terms"] = text.strip() if text.strip().lower() != "none" else ""
                # Create the service now
                pk    = ns["id"]
                PRODUCTS[pk] = {
                    "name":  ns["name"],
                    "price": ns["price"],
                    "emoji": "🔹",
                    "desc":  ns["desc"],
                    "terms": ns.get("terms", ""),
                }
                if pk not in STORE_PRODUCT_ORDER:
                    STORE_PRODUCT_ORDER.append(pk)
                coupons = _load(COUPONS_FILE) or {}
                if pk not in coupons:
                    coupons[pk] = []
                    _save(COUPONS_FILE, coupons)
                save_products_config()
                context.user_data.pop("awaiting_service_step", None)
                context.user_data.pop("new_service", None)
                await update.message.reply_text(
                    f"✅ *Service Create Ho Gayi!*\n\n"
                    f"🔹 `{pk}`\n"
                    f"*Naam:* {ns['name']}\n"
                    f"*Price:* ₹{ns['price']}\n"
                    f"*Desc:* {ns['desc']}\n\n"
                    f"Stock add karne ke liye:\n`/addcoupon {pk} CODE1 CODE2 ...`",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Products Panel", callback_data="admin_products")]]),
                )
                return

            # ── Combo creation steps ──
            if svc_step == "combo_id":
                pk = text.strip().lower()
                if not _re.match(r'^[a-z0-9_]+$', pk):
                    await update.message.reply_text(
                        "❌ ID mein sirf lowercase letters, numbers, underscore allowed hai. Dobara type karo:",
                        parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb,
                    )
                    return
                if pk in PRODUCTS:
                    await update.message.reply_text(
                        f"❌ `{pk}` pehle se exist karta hai! Alag ID do:", parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb,
                    )
                    return
                ns["id"] = pk
                context.user_data["awaiting_service_step"] = "combo_price"
                await update.message.reply_text(
                    f"✅ ID: `{pk}`\n\n*Step 2/4 — Price (₹)*\n\nCombo ka price kya hoga?\nExample: `99`",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb,
                )
                return

            if svc_step == "combo_price":
                try:
                    price = int(text.strip())
                    if price <= 0:
                        raise ValueError
                except ValueError:
                    await update.message.reply_text("❌ Valid price do (positive number):", reply_markup=cancel_kb)
                    return
                ns["price"] = price
                context.user_data["awaiting_service_step"] = "combo_name"
                await update.message.reply_text(
                    f"✅ Price: ₹{price}\n\n*Step 3/4 — Combo Name*\n\nKya naam rakhna hai?\nExample: `Amazon + BigBasket Combo`",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb,
                )
                return

            if svc_step == "combo_name":
                ns["name"] = text.strip()
                context.user_data["awaiting_service_step"] = "combo_desc"
                await update.message.reply_text(
                    f"✅ Naam: {text.strip()}\n\n*Step 4/4 — Description*\n\nCombo ki description do:",
                    parse_mode=ParseMode.MARKDOWN, reply_markup=cancel_kb,
                )
                return

            if svc_step == "combo_desc":
                ns["desc"] = text.strip()
                parts = ns.get("combo_parts", [])
                pk    = ns["id"]
                p1    = PRODUCTS.get(parts[0], {}).get("name", parts[0]) if len(parts) > 0 else ""
                p2    = PRODUCTS.get(parts[1], {}).get("name", parts[1]) if len(parts) > 1 else ""
                PRODUCTS[pk] = {
                    "name":  ns["name"],
                    "price": ns["price"],
                    "emoji": "🎁",
                    "desc":  ns["desc"],
                }
                if pk not in STORE_PRODUCT_ORDER:
                    STORE_PRODUCT_ORDER.append(pk)
                COMBO_PARTS[pk] = parts
                save_products_config()
                context.user_data.pop("awaiting_service_step", None)
                context.user_data.pop("new_service", None)
                context.user_data.pop("combo_selected", None)
                await update.message.reply_text(
                    f"✅ *Combo Create Ho Gaya!*\n\n"
                    f"🎁 `{pk}`\n"
                    f"*Naam:* {ns['name']}\n"
                    f"*Price:* ₹{ns['price']}\n"
                    f"*Parts:* {p1} + {p2}\n\n"
                    f"_Jab user combo buy karega × qty, toh dono services se qty coupons jayenge._",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 Products Panel", callback_data="admin_products")]]),
                )
                return

    # Admin broadcast via panel
    if user.id in ADMIN_IDS and context.user_data.get("broadcast_mode"):
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

        qty_val = int(text)
        min_qty = get_min_qty(product_key)
        if qty_val < min_qty:
            await update.message.reply_text(
                f"⚠️ *Minimum order: {min_qty}*\n\n"
                f"Is product ke liye kam se kam *{min_qty}* coupons lene padenge.\n"
                f"Phir se number bhejo ya ❌ Cancel dabao.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        context.user_data.pop("awaiting_custom_qty", None)
        await _confirm_quantity(update, context, product_key, qty_val)
        return

    # User has a pending order - ALOO payment in progress
    if context.user_data.get("pending_product"):
        await update.message.reply_text(
            "⚡ *Payment verify ho raha hai...*\n\n"
            "Koi action ki zarurat nahi — payment receive hote hi coupon automatically deliver ho jayega.\n\n"
            "Agar cancel karna hai toh ❌ Cancel button press karo.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ─────────────── Admin: Create Service (step-by-step via buttons) ───────────────

async def admin_create_service(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Step 1: Admin clicked 'Add Service' → ask for product ID."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    context.user_data["awaiting_service_step"] = "id"
    context.user_data.pop("new_service", None)
    await query.edit_message_text(
        "➕ *Nai Service Add Karo*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "*Step 1/5 — Product ID*\n\n"
        "Ek unique ID type karo (sirf lowercase letters, numbers, underscore)\n"
        "Example: `amazon_100` ya `netflix_1month`",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_products")]]),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Admin: Edit Service ───────────────

async def admin_edit_svc_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all services as buttons to pick one for editing."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    if not PRODUCTS:
        await query.edit_message_text(
            "❌ Koi service nahi hai edit karne ke liye.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_products")]]),
        )
        return
    buttons = []
    for pk in STORE_PRODUCT_ORDER:
        p = PRODUCTS.get(pk, {})
        buttons.append([InlineKeyboardButton(
            f"{p.get('emoji','🔹')} {p.get('name', pk)}",
            callback_data=f"admin_edit_svc_sel_{pk}"
        )])
    buttons.append([InlineKeyboardButton("◀️ Back", callback_data="admin_products")])
    await query.edit_message_text(
        "✏️ *Kaun si service edit karni hai?*\n\n_(Select karo)_",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_edit_svc_sel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show edit options for the selected service."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    pk = query.data.replace("admin_edit_svc_sel_", "")
    p  = PRODUCTS.get(pk, {})
    if not p:
        await query.edit_message_text("❌ Service nahi mili.")
        return
    context.user_data["edit_svc_pk"] = pk
    await query.edit_message_text(
        f"✏️ *Edit Service*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{p.get('emoji','🔹')} *{p.get('name', pk)}* — ₹{p.get('price', 0)}\n\n"
        f"Kya edit karna hai?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📛 Naam",        callback_data=f"admin_edit_svc_field_name")],
            [InlineKeyboardButton("💰 Price",       callback_data=f"admin_edit_svc_field_price")],
            [InlineKeyboardButton("📝 Description", callback_data=f"admin_edit_svc_field_desc")],
            [InlineKeyboardButton("📜 Terms",       callback_data=f"admin_edit_svc_field_terms")],
            [InlineKeyboardButton("◀️ Back",        callback_data="admin_edit_svc_list")],
        ]),
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_edit_svc_field(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask admin to type the new value for the chosen field."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    field = query.data.replace("admin_edit_svc_field_", "")
    pk    = context.user_data.get("edit_svc_pk", "")
    p     = PRODUCTS.get(pk, {})
    if not p:
        await query.edit_message_text("❌ Session expire ho gaya, dobara try karo.")
        return
    context.user_data["awaiting_edit_svc_field"] = field
    labels = {"name": "Naam", "price": "Price (₹, sirf number)", "desc": "Description", "terms": "Terms & Conditions (ya 'none')"}
    current = {
        "name":  p.get("name", ""),
        "price": str(p.get("price", "")),
        "desc":  p.get("desc", "_(khaali)_"),
        "terms": p.get("terms", "_(khaali)_"),
    }
    await query.edit_message_text(
        f"✏️ *{labels.get(field, field)} Edit Karo*\n\n"
        f"Service: *{p.get('name', pk)}*\n"
        f"Current: `{current.get(field, '')}`\n\n"
        f"Naya {labels.get(field, field)} type karke bhejo:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"admin_edit_svc_sel_{pk}")]]),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Admin: Delete Service (list with buttons) ───────────────

async def admin_del_service_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show all deletable services as buttons."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    if not PRODUCTS:
        await query.edit_message_text(
            "❌ Koi service nahi hai delete karne ke liye.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_products")]]),
        )
        return
    buttons = []
    for pk in STORE_PRODUCT_ORDER:
        p = PRODUCTS.get(pk, {})
        buttons.append([InlineKeyboardButton(
            f"{p.get('emoji','🔹')} {p.get('name', pk)} — ₹{p.get('price',0)}",
            callback_data=f"admin_del_svc_confirm_{pk}"
        )])
    buttons.append([InlineKeyboardButton("◀️ Back", callback_data="admin_products")])
    await query.edit_message_text(
        "🗑️ *Kaun si service delete karni hai?*\n\n_(Select karo)_",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_del_svc_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirm deletion of a service."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    pk = query.data.replace("admin_del_svc_confirm_", "")
    p  = PRODUCTS.get(pk, {})
    if not p:
        await query.edit_message_text("❌ Service nahi mili.")
        return
    await query.edit_message_text(
        f"⚠️ *Confirm Delete*\n\n"
        f"{p.get('emoji','🔹')} *{p.get('name', pk)}*\n"
        f"Price: ₹{p.get('price', 0)}\n\n"
        f"Kya aap pakka delete karna chahte ho?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Haan, Delete Karo", callback_data=f"admin_del_svc_do_{pk}")],
            [InlineKeyboardButton("❌ Cancel",            callback_data="admin_del_service_list")],
        ]),
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_del_svc_do(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Actually delete the service."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    pk = query.data.replace("admin_del_svc_do_", "")
    if pk not in PRODUCTS:
        await query.edit_message_text("❌ Service nahi mili.")
        return
    name = PRODUCTS[pk].get("name", pk)
    del PRODUCTS[pk]
    if pk in STORE_PRODUCT_ORDER:
        STORE_PRODUCT_ORDER.remove(pk)
    if pk in COMBO_PARTS:
        del COMBO_PARTS[pk]
    # Also remove from any combos that use this service as a part
    for ck in list(COMBO_PARTS.keys()):
        if pk in COMBO_PARTS[ck]:
            del COMBO_PARTS[ck]
            if ck in PRODUCTS:
                del PRODUCTS[ck]
            if ck in STORE_PRODUCT_ORDER:
                STORE_PRODUCT_ORDER.remove(ck)
    save_products_config()
    await query.edit_message_text(
        f"✅ *Service delete ho gayi!*\n\n`{pk}` — *{name}* ab store mein nahi hai.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Products Panel", callback_data="admin_products")]]),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Admin: Create Combo (select 2 services) ───────────────

async def admin_combo_create(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show non-combo services for admin to select 2 for a combo."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    non_combo = [pk for pk in STORE_PRODUCT_ORDER if pk not in COMBO_PARTS]
    if len(non_combo) < 2:
        await query.edit_message_text(
            "❌ Combo banane ke liye kam se kam *2 services* chahiye.\n\nPehle services add karo.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_products")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    context.user_data["combo_selected"] = []
    await _show_combo_select(query, context, non_combo)


async def _show_combo_select(query, context, non_combo=None):
    """Re-render the combo selection screen."""
    if non_combo is None:
        non_combo = [pk for pk in STORE_PRODUCT_ORDER if pk not in COMBO_PARTS]
    selected = context.user_data.get("combo_selected", [])
    buttons = []
    for pk in non_combo:
        p    = PRODUCTS.get(pk, {})
        tick = "✅ " if pk in selected else ""
        buttons.append([InlineKeyboardButton(
            f"{tick}{p.get('emoji','🔹')} {p.get('name', pk)}",
            callback_data=f"admin_combo_sel_{pk}"
        )])
    row = []
    if len(selected) >= 2:
        row.append(InlineKeyboardButton("➡️ Aage Badho", callback_data="admin_combo_done"))
    row.append(InlineKeyboardButton("❌ Cancel", callback_data="admin_products"))
    buttons.append(row)
    sel_names = [PRODUCTS.get(pk, {}).get("name", pk) for pk in selected]
    sel_text  = " + ".join(sel_names) if sel_names else "_koi nahi_"
    await query.edit_message_text(
        f"🎁 *Combo Banao*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"2 services select karo jo combo mein shamil hongi:\n\n"
        f"Selected: {sel_text}",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_combo_sel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle a service selection for combo."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    pk       = query.data.replace("admin_combo_sel_", "")
    selected = context.user_data.get("combo_selected", [])
    if pk in selected:
        selected.remove(pk)
    else:
        if len(selected) >= 2:
            await query.answer("⚠️ Sirf 2 services select kar sakte ho!", show_alert=True)
            return
        selected.append(pk)
    context.user_data["combo_selected"] = selected
    await _show_combo_select(query, context)


async def admin_combo_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """2 services selected — now ask for combo details (step by step)."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    selected = context.user_data.get("combo_selected", [])
    if len(selected) < 2:
        await query.answer("⚠️ Pehle 2 services select karo!", show_alert=True)
        return
    context.user_data["awaiting_service_step"] = "combo_id"
    context.user_data["new_service"] = {"combo_parts": selected}
    p1 = PRODUCTS.get(selected[0], {}).get("name", selected[0])
    p2 = PRODUCTS.get(selected[1], {}).get("name", selected[1])
    await query.edit_message_text(
        f"🎁 *Combo Banao*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Selected: *{p1}* + *{p2}*\n\n"
        f"*Step 1/4 — Combo ID*\n\n"
        f"Ek unique ID type karo:\n"
        f"Example: `combo_amazon_bb`",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_products")]]),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Admin product management commands ───────────────

async def products_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/products — show all products with id, name, price."""
    if update.effective_user.id not in ADMIN_IDS:
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
    if update.effective_user.id not in ADMIN_IDS:
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
    if update.effective_user.id not in ADMIN_IDS:
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
    if update.effective_user.id not in ADMIN_IDS:
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
    if update.effective_user.id not in ADMIN_IDS:
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
    if update.effective_user.id not in ADMIN_IDS:
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

async def debug_ref_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/debug_ref USER_ID — show full referral + points + rewards state for a user."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    uid  = args[0] if args else str(update.effective_user.id)

    earned  = db_successful_referral_count(uid)
    total   = db_total_referral_count(uid)
    points  = db_get_points(uid)
    rewards = db_list_rewards()

    lines = [
        f"🔍 *Debug: User `{uid}`*",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"📨 Total referrals in DB: *{total}*",
        f"✅ Active verified (= earned pts): *{earned}*",
        f"💎 Points bacha (earned - spent): *{points}*",
        f"",
        f"🎁 *Rewards Table ({len(rewards)} entries):*",
    ]
    if not rewards:
        lines.append("❌ `rewards` table EMPTY! Run `/add_reward N NAAM` pehle!")
    else:
        for r in rewards:
            codes = db_list_reward_coupons(r["name"])
            eligible = "✅ ELIGIBLE" if points >= r["points"] else "❌ Not enough pts"
            lines.append(
                f"• `{r['name']}` — needs *{r['points']}* pts | stock *{r['stock']}* | {eligible}"
            )
            if codes:
                lines.append(f"  Codes: {', '.join(f'`{c}`' for c in codes[:3])}{'...' if len(codes)>3 else ''}")

    # referrals detail
    import sqlite3 as _sq
    con = _sq.connect(REFERRAL_DB)
    rows = con.execute(
        "SELECT user_id, reward_given, referral_status FROM referrals WHERE referred_by=?", (uid,)
    ).fetchall()
    con.close()
    lines += ["", f"👥 *Referred Users ({len(rows)}):*"]
    for row_uid, rg, rs in rows[:10]:
        lines.append(f"  • `{row_uid}` — reward_given={rg} status={rs}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def force_reward_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/force_reward USER_ID — manually trigger referral reward for a user."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: `/force_reward USER_ID`", parse_mode=ParseMode.MARKDOWN)
        return
    uid   = args[0]
    users = get_users()
    name  = users.get(uid, {}).get("first_name", "User")
    pts   = db_get_points(uid)
    await update.message.reply_text(
        f"🔄 Triggering reward for `{uid}` ({name})... Points bacha: *{pts}*",
        parse_mode=ParseMode.MARKDOWN,
    )
    await send_referral_reward(context, uid, name, db_successful_referral_count(uid))
    await update.message.reply_text("✅ Done! Check user ke Telegram pe.", parse_mode=ParseMode.MARKDOWN)


async def del_reward_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/del_reward NAAM — delete entire reward + all unclaimed codes."""
    if update.effective_user.id not in ADMIN_IDS:
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


async def give_points_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/give_points USER_ID POINTS — admin manually add points to a user."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "❌ *Usage:* `/give_points USER_ID POINTS`\n"
            "Example: `/give_points 6724474397 3`",
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
    # Points add = negative deduction (reverse)
    _get_db()["points_spent"].insert_one({
        "user_id": str(uid), "points": -pts,
        "reward_name": "admin_bonus", "spent_at": datetime.now().isoformat()
    })
    after = db_get_points(uid)
    await update.message.reply_text(
        f"✅ *Points add ho gaye!*\n\n"
        f"User: `{uid}`\n"
        f"Before: *{before} pts* → After: *{after} pts*\n"
        f"Added: *+{pts} pts*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def deduct_points_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/deduct_points USER_ID POINTS — admin manually deduct points from a user."""
    if update.effective_user.id not in ADMIN_IDS:
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
    if update.effective_user.id not in ADMIN_IDS:
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
    if update.effective_user.id not in ADMIN_IDS:
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
    if update.effective_user.id not in ADMIN_IDS:
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

# ═══════════════════════════════════════════════════════════════════════════
# ALOO BHARATPE PAYMENT SYSTEM
# ═══════════════════════════════════════════════════════════════════════════
import re as _re

_DEFAULT_SETTINGS = {
    "custom_upi_id":   None,   # If None, fallback to UPI_ID constant
    "timeout_minutes": 5,      # Polling window (1-60 min)
    "aloo_enabled":    True,   # Toggle ALOO/BharatPe payment on/off
    "zapupi_enabled":  False,  # Toggle ZapUPI payment on/off
    "zapupi_key":      "",     # ZapUPI merchant API key (admin sets via panel)
}

def get_settings() -> dict:
    s = load_json(SETTINGS_FILE, {})
    if not isinstance(s, dict):
        s = {}
    for k, v in _DEFAULT_SETTINGS.items():
        if k not in s:
            s[k] = v
    return s

def save_settings(s: dict) -> None:
    _save(SETTINGS_FILE, s)

def get_active_upi() -> str:
    custom = get_settings().get("custom_upi_id")
    return custom if custom else UPI_ID

def get_active_qr_path() -> str:
    return CUSTOM_QR_PATH if os.path.exists(CUSTOM_QR_PATH) else QR_IMAGE_PATH

def get_active_payment_method() -> str:
    """Returns 'zapupi', 'aloo', or 'none' based on admin toggles.
    ZapUPI takes priority if both are enabled."""
    s = get_settings()
    if s.get("zapupi_enabled"):
        return "zapupi"
    if s.get("aloo_enabled", True):
        return "aloo"
    return "none"

def get_zapupi_key() -> str:
    """Get active ZapUPI key — env var takes priority over admin-set value."""
    return ZAPUPI_KEY or get_settings().get("zapupi_key", "")

# ─────────────── Used Amounts (anti-fraud) ───────────────

def get_used_amounts() -> dict:
    """Dict {amount_str: {used_at, user_id, order_id}}"""
    d = load_json(USED_AMOUNTS_FILE, {})
    return d if isinstance(d, dict) else {}

def save_used_amounts(d: dict) -> None:
    _save(USED_AMOUNTS_FILE, d)

def _amount_key(amount: float) -> str:
    return f"{amount:.2f}"

def _is_amount_used(amount: float) -> bool:
    return _amount_key(amount) in get_used_amounts()

def _mark_amount_used(amount: float, user_id: int, order_id: str) -> None:
    used = get_used_amounts()
    used[_amount_key(amount)] = {
        "used_at": now_ts(),
        "user_id": user_id,
        "order_id": order_id,
    }
    save_used_amounts(used)

# ─────────────── Unique amount generator ───────────────

def _generate_unique_amount(base_amount: int) -> float:
    """Add a unique paise suffix (0-99) to distinguish concurrent payments.
    Tries up to 100 values. Returns base_amount.00 if all taken (very unlikely)."""
    import random
    used = get_used_amounts()
    candidates = list(range(100))
    random.shuffle(candidates)
    for paise in candidates:
        candidate = round(base_amount + paise / 100, 2)
        if _amount_key(candidate) not in used:
            return candidate
    return float(base_amount)

# ─────────────── Deposits log ───────────────

def get_deposits_log() -> list:
    d = load_json(DEPOSITS_LOG_FILE, [])
    return d if isinstance(d, list) else []

def save_deposits_log(d: list) -> None:
    _save(DEPOSITS_LOG_FILE, d[-50:])

def log_deposit(entry: dict) -> None:
    log = get_deposits_log()
    log.append(entry)
    save_deposits_log(log)

# ─────────────── ALOO API: verify payment ───────────────

def _aloo_verify(amount: float) -> dict:
    """Call ALOO API to check if a payment of exactly amount was received.
    Returns the API response dict on success, None on error."""
    if not ALOO_API_KEY or not ALOO_MERCHANT_ID:
        logger.warning("[ALOO] API key or merchant ID not configured!")
        return None
    try:
        params = urllib.parse.urlencode({
            "api_key":     ALOO_API_KEY,
            "merchant_id": ALOO_MERCHANT_ID,
            "amount":      f"{amount:.2f}",
        })
        url = f"{ALOO_BASE_URL}/verify?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "CouponBot/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except Exception as e:
        logger.error(f"[ALOO] API call failed: {e}")
        return None

# ─────────────── Core: approve order & deliver coupon ───────────────

async def _execute_aloo_approve(context, order_id: str, amount: float, utr: str = "") -> bool:
    """Mark payment verified -> deliver coupon -> notify admin."""
    order, status = await _execute_approve(context, order_id)
    if not isinstance(status, list):
        logger.error(f"[ALOO] _execute_approve failed: {status}")
        return False

    user_id = (order or {}).get("user_id", 0)

    # Mark amount as used (anti-fraud)
    _mark_amount_used(amount, user_id, order_id)

    # Log deposit
    log_deposit({
        "ts":       now_ts(),
        "user_id":  user_id,
        "username": (order or {}).get("username", ""),
        "product":  (order or {}).get("product", ""),
        "expected": amount,
        "paid":     amount,
        "utr":      utr,
        "status":   "approved",
        "auto":     True,
    })

    # Notify admin
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"\u2705 <b>Auto Payment Approved (ALOO/BharatPe)</b>\n"
                f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f464 User: <code>{user_id}</code>\n"
                f"\U0001f4e6 Product: {html.escape(str((order or {}).get('product', '')))}\n"
                f"\U0001f4b0 Amount: \u20b9{amount:.2f}\n"
                f"\U0001f511 UTR: <code>{utr or 'N/A'}</code>\n"
                f"\U0001f39f Coupon delivered."
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"[ALOO] Admin notify failed: {e}")
    return True

# ─────────────── Polling job: runs every ALOO_POLL_INTERVAL seconds ───────────────

async def aloo_poll_job(context) -> None:
    """Background job: poll ALOO API for each user with a pending payment."""
    pending_payments = context.bot_data.get("aloo_pending", {})
    if not pending_payments:
        return

    to_remove = []
    for user_id_str, info in list(pending_payments.items()):
        user_id    = int(user_id_str)
        amount     = info["amount"]
        order_id   = info["order_id"]
        polls_done = info.get("polls_done", 0)
        chat_id    = info.get("chat_id", user_id)

        # Timeout check
        if polls_done >= ALOO_MAX_POLLS:
            to_remove.append(user_id_str)
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"\u26a0\ufe0f *Payment Verify Nahi Ho Saka*\n\n"
                        f"5 minute mein payment confirm nahi hua.\n"
                        f"Agar aapne payment ki hai toh support se contact karo: {SUPPORT_HANDLE}\n\n"
                        f"Amount: \u20b9{amount:.2f}"
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass
            # Cancel order
            orders = get_orders()
            if order_id in orders and orders[order_id].get("status") == "pending":
                orders[order_id]["status"] = "timeout"
                save_orders(orders)
            pending_orders = get_pending()
            pending_orders.pop(user_id_str, None)
            save_pending(pending_orders)
            log_deposit({
                "ts": now_ts(), "user_id": user_id,
                "expected": amount, "status": "timeout", "auto": True,
            })
            continue

        # Poll ALOO API
        result = _aloo_verify(amount)
        info["polls_done"] = polls_done + 1
        pending_payments[user_id_str] = info

        if result and result.get("success"):
            utr = result.get("utr", "")
            to_remove.append(user_id_str)
            # Notify user
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="\u2705 *Payment verified!* Coupon delivery in progress...",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass
            await _execute_aloo_approve(context, order_id, amount, utr)

    # Remove completed entries
    for uid_str in to_remove:
        pending_payments.pop(uid_str, None)
    context.bot_data["aloo_pending"] = pending_payments

# ─────────────── "Maine Pay Kar Diya" Handler ───────────────

_MAX_PAY_RETRIES = 10  # max times user can click retry

async def i_paid_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User clicked 'Maine Pay Kar Diya' — call ALOO API once to verify."""
    query = update.callback_query
    await query.answer("🔍 Payment check ho raha hai...")
    user_id = query.from_user.id

    amount   = context.user_data.get("pending_amount")
    order_id = get_pending().get(str(user_id))

    # Fallback: parse amount from callback_data (survives bot restart)
    if not amount:
        cb = query.data  # e.g. "i_paid_150.37"
        if cb.startswith("i_paid_"):
            try:
                amount = float(cb[len("i_paid_"):])
                context.user_data["pending_amount"] = amount
            except (ValueError, IndexError):
                pass

    if not amount or not order_id:
        await query.edit_message_caption(
            caption="❌ *Session expire ho gaya.* /start dabao naya order karo.",
            parse_mode=ParseMode.MARKDOWN,
        ) if query.message and query.message.photo else await query.edit_message_text(
            "❌ *Session expire ho gaya.* /start dabao naya order karo.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Track retry count
    retries = context.user_data.get("paid_check_count", 0)
    if retries >= _MAX_PAY_RETRIES:
        # Cancel order
        orders = get_orders()
        if order_id in orders:
            orders[order_id]["status"] = "timeout"
            save_orders(orders)
        pending = get_pending()
        pending.pop(str(user_id), None)
        save_pending(pending)
        context.user_data.pop("pending_product",  None)
        context.user_data.pop("pending_amount",   None)
        context.user_data.pop("paid_check_count", None)
        msg = "❌ *Bahut zyada retries ho gayi.* Order cancel kar diya gaya.\n\nDobara order karo ya support se contact karo."
        try:
            await query.edit_message_caption(caption=msg, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    context.user_data["paid_check_count"] = retries + 1

    # ── Detect payment method via order record ──
    orders_rec = get_orders()
    order_via = (orders_rec.get(order_id) or {}).get("via", "aloo")

    retry_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Dobara Check Karo", callback_data="i_paid_retry")],
        [InlineKeyboardButton("❌ Cancel Order",      callback_data="cancel_order")],
    ])

    async def _edit_msg(text: str, kb=None):
        try:
            if query.message and query.message.photo:
                await query.edit_message_caption(caption=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            else:
                await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass

    if order_via == "zapupi":
        # ── ZapUPI check ──
        await _edit_msg(f"🔍 *Payment verify ho rahi hai...* (Attempt {retries+1}/{_MAX_PAY_RETRIES})\n\nEk second ruko...")
        zap_status = _zapupi_check_status(order_id)
        data = (zap_status or {}).get("data", {})
        status_val = data.get("status", "")
        if status_val == "Success":
            utr = data.get("utr", "")
            context.user_data.pop("paid_check_count", None)
            await _edit_msg("✅ *Payment verify ho gaya!* Coupon deliver ho raha hai...")
            await _execute_zapupi_approve(context, order_id, float(amount), utr)
        else:
            baki = _MAX_PAY_RETRIES - retries - 1
            msg = (
                f"⚠️ *Payment abhi detect nahi hui.*\n\n"
                f"Agar aapne payment kar di hai toh thodi der baad dobara check karo.\n"
                f"_(Attempts remaining: {baki})_"
            )
            await _edit_msg(msg, retry_kb)
    else:
        # ── ALOO check ──
        await _edit_msg(f"🔍 *Payment verify ho rahi hai...* (Attempt {retries+1}/{_MAX_PAY_RETRIES})\n\nEk second ruko...")
        result = _aloo_verify(amount)
        if result and result.get("success"):
            utr = result.get("utr", "")
            context.user_data.pop("paid_check_count", None)
            await _edit_msg("✅ *Payment verify ho gaya!* Coupon deliver ho raha hai...")
            await _execute_aloo_approve(context, order_id, amount, utr)
        else:
            baki = _MAX_PAY_RETRIES - retries - 1
            msg = (
                f"⚠️ *Payment abhi detect nahi hui.*\n\n"
                f"Agar aapne payment kar di hai toh thodi der baad dobara check karo.\n"
                f"_(Attempts remaining: {baki})_"
            )
            await _edit_msg(msg, retry_kb)


async def i_paid_retry_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Same as i_paid_handler — retry button."""
    await i_paid_handler(update, context)


# ─────────────── Admin Panel: Payment settings ───────────────

def _back_to_admin_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("\u25c0\ufe0f Back to Admin", callback_data="admin_back")]])

async def admin_set_upi(update, context) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    context.user_data["awaiting_upi"] = True
    cur = get_active_upi()
    await query.edit_message_text(
        f"\U0001f4b3 *Change UPI ID*\n\nCurrent: `{cur}`\n\nNaya UPI ID type karke bhejo (cancel ke liye /admin):",
        reply_markup=_back_to_admin_kb(),
        parse_mode=ParseMode.MARKDOWN,
    )

async def admin_set_qr(update, context) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    context.user_data["awaiting_qr_upload"] = True
    await query.edit_message_text(
        f"\U0001f4f7 *Change QR Code*\n\nNaya QR image (photo) bhejo abhi (cancel ke liye /admin):",
        reply_markup=_back_to_admin_kb(),
        parse_mode=ParseMode.MARKDOWN,
    )

async def admin_recent_deposits(update, context) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    log = list(reversed(get_deposits_log()))[:30]
    if not log:
        text = "\U0001f4ca *Recent Deposits*\n\n_(Koi deposit nahi hua abhi tak)_"
    else:
        lines_out = ["\U0001f4ca *Recent Deposits (latest 30)*", "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"]
        for e in log:
            ts = datetime.fromtimestamp(float(e.get("ts", 0))).strftime("%d %b %H:%M")
            status_emoji = {"approved": "\u2705", "timeout": "\u23f1"}.get(e.get("status", ""), "\u2753")
            uid = e.get("user_id", "")
            utr = e.get("utr", "N/A")
            amt = e.get("paid") or e.get("expected", "")
            lines_out.append(f"{status_emoji} \u20b9{amt}  UTR:`{utr}`  User:`{uid}`  {ts}")
        text = "\n".join(lines_out)
    await query.edit_message_text(text, reply_markup=_back_to_admin_kb(), parse_mode=ParseMode.MARKDOWN)

async def admin_set_timeout(update, context) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    context.user_data["awaiting_timeout"] = True
    cur = get_settings().get("timeout_minutes", 5)
    await query.edit_message_text(
        f"\u23f1 *Set Payment Timeout*\n\nCurrent: *{cur} minutes*\n\nNumber bhejo (1-60):",
        reply_markup=_back_to_admin_kb(),
        parse_mode=ParseMode.MARKDOWN,
    )

# ─────────────── Min Quantity admin panel ───────────────

async def admin_min_qty_panel(update, context) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    context.user_data.pop("awaiting_min_qty_edit", None)
    lines_out = [
        "\U0001f4d0 *Minimum Quantity Settings*",
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
        "Default: *1* (sab products ke liye)",
        "Kisi product ka minimum badhana ho toh niche button dabao.",
        "",
    ]
    rows = []
    keys = [k for k in STORE_PRODUCT_ORDER if k in PRODUCTS]
    for k in keys:
        p = PRODUCTS[k]
        if p.get("hidden"):
            continue
        m = get_min_qty(k)
        marker = "" if m == 1 else "  \u2699\ufe0f"
        lines_out.append(f"{p.get('emoji','\U0001f4e6')} *{p.get('name', k)}* \u2014 min: *{m}*{marker}")
        lbl = p.get("name", k)
        if len(lbl) > 22:
            lbl = lbl[:21] + "\u2026"
        rows.append([InlineKeyboardButton(
            f"\u270f\ufe0f {lbl} (now: {m})",
            callback_data=f"admin_min_qty_edit_{k}",
        )])
    rows.append([InlineKeyboardButton("\u25c0\ufe0f Back to Admin", callback_data="admin_back")])
    await query.edit_message_text(
        "\n".join(lines_out),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.MARKDOWN,
    )

async def admin_min_qty_edit(update, context) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    key = query.data.replace("admin_min_qty_edit_", "", 1)
    if key not in PRODUCTS:
        await query.edit_message_text("\u274c Product nahi mila.", reply_markup=_back_to_admin_kb())
        return
    cur = get_min_qty(key)
    p = PRODUCTS[key]
    context.user_data["awaiting_min_qty_edit"] = key
    await query.edit_message_text(
        f"\U0001f4d0 *Set Min Quantity*\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"{p.get('emoji','\U0001f4e6')} *{p.get('name', key)}*\n"
        f"Current min: *{cur}*\n\n"
        f"\U0001f447 New min number bhejo (1 = no limit, max 1000).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("\u25c0\ufe0f Back", callback_data="admin_min_qty")]]),
        parse_mode=ParseMode.MARKDOWN,
    )

# ═══════════════════════════════════════════════════════════════════════════
# END ALOO BHARATPE PAYMENT SYSTEM
# ═══════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════
# ZAPUPI PAYMENT SYSTEM
# ═══════════════════════════════════════════════════════════════════════════

def _zapupi_create_order(order_id: str, amount: float) -> dict:
    """Call ZapUPI API to create a new payment order.
    Returns the API response dict or None on error."""
    key = get_zapupi_key()
    if not key:
        logger.warning("[ZapUPI] API key not configured!")
        return {"status": "error", "message": "ZapUPI key not set. Admin se contact karo."}
    try:
        payload = json.dumps({
            "zap_key":  key,
            "order_id": order_id,
            "amount":   str(amount),
            "remark":   "TelegramBotOrder",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{ZAPUPI_BASE_URL}/api/create-order",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "CouponBot/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"[ZapUPI] create_order failed: {e}")
        return None


def _zapupi_check_status(order_id: str) -> dict:
    """Poll ZapUPI order-status API.
    Returns API response dict or None on error."""
    key = get_zapupi_key()
    if not key:
        return None
    try:
        payload = json.dumps({"zap_key": key, "order_id": order_id}).encode("utf-8")
        req = urllib.request.Request(
            f"{ZAPUPI_BASE_URL}/api/order-status",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "CouponBot/1.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        logger.error(f"[ZapUPI] check_status failed: {e}")
        return None


async def _execute_zapupi_approve(context, order_id: str, amount: float, utr: str = "") -> bool:
    """Mark ZapUPI payment verified → deliver coupon → notify admin."""
    order, status = await _execute_approve(context, order_id)
    if not isinstance(status, list):
        logger.error(f"[ZapUPI] _execute_approve failed: {status}")
        return False

    user_id = (order or {}).get("user_id", 0)

    log_deposit({
        "ts":       now_ts(),
        "user_id":  user_id,
        "username": (order or {}).get("username", ""),
        "product":  (order or {}).get("product", ""),
        "expected": amount,
        "paid":     amount,
        "utr":      utr,
        "status":   "approved",
        "auto":     True,
        "via":      "zapupi",
    })

    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"✅ <b>Auto Payment Approved (ZapUPI)</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 User: <code>{user_id}</code>\n"
                f"📦 Product: {html.escape(str((order or {}).get('product', '')))}\n"
                f"💰 Amount: ₹{amount:.2f}\n"
                f"🔑 UTR: <code>{utr or 'N/A'}</code>\n"
                f"🎟 Coupon delivered."
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.error(f"[ZapUPI] Admin notify failed: {e}")
    return True


async def zapupi_poll_job(context) -> None:
    """Background job: poll ZapUPI API for each user with a pending payment."""
    pending_payments = context.bot_data.get("zapupi_pending", {})
    if not pending_payments:
        return

    to_remove = []
    for user_id_str, info in list(pending_payments.items()):
        user_id    = int(user_id_str)
        amount     = info["amount"]
        order_id   = info["order_id"]
        polls_done = info.get("polls_done", 0)
        chat_id    = info.get("chat_id", user_id)

        if polls_done >= ZAPUPI_MAX_POLLS:
            to_remove.append(user_id_str)
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⚠️ *Payment Verify Nahi Ho Saka*\n\n"
                        f"5 minute mein payment confirm nahi hua.\n"
                        f"Agar aapne payment ki hai toh support se contact karo: {SUPPORT_HANDLE}\n\n"
                        f"Amount: ₹{amount:.2f}"
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception:
                pass
            orders = get_orders()
            if order_id in orders and orders[order_id].get("status") == "pending":
                orders[order_id]["status"] = "timeout"
                save_orders(orders)
            pending_orders = get_pending()
            pending_orders.pop(user_id_str, None)
            save_pending(pending_orders)
            log_deposit({
                "ts": now_ts(), "user_id": user_id,
                "expected": amount, "status": "timeout", "auto": True, "via": "zapupi",
            })
            continue

        result = _zapupi_check_status(order_id)
        info["polls_done"] = polls_done + 1
        pending_payments[user_id_str] = info

        if result and result.get("status") == "success":
            data   = result.get("data", {})
            status = data.get("status", "")
            if status == "Success":
                utr = data.get("utr", "")
                to_remove.append(user_id_str)
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="✅ *Payment verified!* Coupon delivery in progress...",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass
                await _execute_zapupi_approve(context, order_id, amount, utr)
            elif status == "Failed":
                to_remove.append(user_id_str)
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"❌ *Payment Failed*\n\n"
                            f"ZapUPI ne payment fail report ki.\n"
                            f"Support: {SUPPORT_HANDLE}"
                        ),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    pass

    for uid_str in to_remove:
        pending_payments.pop(uid_str, None)
    context.bot_data["zapupi_pending"] = pending_payments


# ─────────────── Admin: Payment Methods Panel ───────────────

async def admin_payment_methods(update, context) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    s = get_settings()
    aloo_on   = s.get("aloo_enabled", True)
    zap_on    = s.get("zapupi_enabled", False)
    zap_key   = get_zapupi_key()
    active    = get_active_payment_method()

    aloo_icon = "🟢" if aloo_on else "🔴"
    zap_icon  = "🟢" if zap_on  else "🔴"
    active_lbl = {"aloo": "ALOO/BharatPe", "zapupi": "ZapUPI", "none": "⚠️ Koi nahi"}.get(active, active)

    text = (
        f"💳 *Payment Methods*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Active Method: *{active_lbl}*\n\n"
        f"{aloo_icon} *ALOO/BharatPe* — {'ON' if aloo_on else 'OFF'}\n"
        f"{zap_icon} *ZapUPI* — {'ON' if zap_on else 'OFF'}\n\n"
        f"ZapUPI Key: `{'Set ✅' if zap_key else 'Not set ❌'}`\n\n"
        f"ℹ️ _Agar dono ON hain toh ZapUPI priority lega._"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{aloo_icon} ALOO Toggle ({'ON→OFF' if aloo_on else 'OFF→ON'})",
                              callback_data="admin_toggle_aloo")],
        [InlineKeyboardButton(f"{zap_icon} ZapUPI Toggle ({'ON→OFF' if zap_on else 'OFF→ON'})",
                              callback_data="admin_toggle_zapupi")],
        [InlineKeyboardButton("🔑 Set ZapUPI Key", callback_data="admin_set_zapupi_key")],
        [InlineKeyboardButton("◀️ Back to Admin",  callback_data="admin_back")],
    ])
    await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


async def admin_toggle_aloo(update, context) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    s = get_settings()
    s["aloo_enabled"] = not s.get("aloo_enabled", True)
    save_settings(s)
    await admin_payment_methods(update, context)


async def admin_toggle_zapupi(update, context) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    s = get_settings()
    s["zapupi_enabled"] = not s.get("zapupi_enabled", False)
    save_settings(s)
    await admin_payment_methods(update, context)


async def admin_set_zapupi_key(update, context) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in ADMIN_IDS:
        return
    context.user_data["awaiting_zapupi_key"] = True
    cur = get_zapupi_key()
    masked = (cur[:6] + "..." + cur[-4:]) if len(cur) > 10 else ("Set ✅" if cur else "Not set ❌")
    await query.edit_message_text(
        f"🔑 *Set ZapUPI API Key*\n\nCurrent: `{masked}`\n\nApna ZapUPI API key bhejo (cancel ke liye /admin):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_payment_methods")]]),
        parse_mode=ParseMode.MARKDOWN,
    )

# ═══════════════════════════════════════════════════════════════════════════
# END ZAPUPI PAYMENT SYSTEM
# ═══════════════════════════════════════════════════════════════════════════


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set!")
    if not ADMIN_ID:
        raise ValueError("TELEGRAM_ADMIN_ID environment variable not set!")
    if not _MONGO_URI:
        raise ValueError("MONGODB_URI environment variable not set! It is required for database persistence on Render.")

    # Load products configuration on startup (lazy connection/loading inside main, after Flask thread is spun up)
    load_startup_data()

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
    app.add_handler(CommandHandler("ban",         cmd_ban))
    app.add_handler(CommandHandler("unban",       cmd_unban))
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
    app.add_handler(CommandHandler("debug_ref",      debug_ref_command))
    app.add_handler(CommandHandler("force_reward",   force_reward_command))
    app.add_handler(CommandHandler("give_points",    give_points_command))
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
    app.add_handler(CallbackQueryHandler(join_waitlist,       pattern="^waitlist_"))
    app.add_handler(CallbackQueryHandler(buy_product,         pattern="^buy_"))
    app.add_handler(CallbackQueryHandler(custom_qty_prompt, pattern="^qty_custom_"))
    app.add_handler(CallbackQueryHandler(select_quantity,   pattern=r"^qty_(?!custom_).+_\d+$"))
    app.add_handler(CallbackQueryHandler(cancel_order,      pattern="^cancel_order$"))
    app.add_handler(CallbackQueryHandler(i_paid_handler,       pattern="^i_paid(_[0-9.]+)?$"))
    app.add_handler(CallbackQueryHandler(i_paid_retry_handler, pattern="^i_paid_retry$"))

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
    app.add_handler(CallbackQueryHandler(admin_remove_coupon,     pattern="^admin_remove_coupon$"))
    app.add_handler(CallbackQueryHandler(admin_rmcoupon_sel,      pattern="^admin_rmcoupon_sel_"))
    app.add_handler(CallbackQueryHandler(admin_rmcoupon_do,       pattern="^admin_rmcoupon_do_"))
    app.add_handler(CallbackQueryHandler(admin_products_panel,   pattern="^admin_products$"))
    app.add_handler(CallbackQueryHandler(admin_create_service,   pattern="^admin_create_service$"))
    app.add_handler(CallbackQueryHandler(admin_edit_svc_list,    pattern="^admin_edit_svc_list$"))
    app.add_handler(CallbackQueryHandler(admin_edit_svc_sel,     pattern="^admin_edit_svc_sel_"))
    app.add_handler(CallbackQueryHandler(admin_edit_svc_field,   pattern="^admin_edit_svc_field_"))
    app.add_handler(CallbackQueryHandler(admin_del_service_list, pattern="^admin_del_service_list$"))
    app.add_handler(CallbackQueryHandler(admin_del_svc_confirm,  pattern="^admin_del_svc_confirm_"))
    app.add_handler(CallbackQueryHandler(admin_del_svc_do,       pattern="^admin_del_svc_do_"))
    app.add_handler(CallbackQueryHandler(admin_combo_create,     pattern="^admin_combo_create$"))
    app.add_handler(CallbackQueryHandler(admin_combo_sel,        pattern="^admin_combo_sel_"))
    app.add_handler(CallbackQueryHandler(admin_combo_done,       pattern="^admin_combo_done$"))
    app.add_handler(CallbackQueryHandler(admin_broadcast_prompt, pattern="^admin_broadcast$"))
    app.add_handler(CallbackQueryHandler(admin_back,             pattern="^admin_back"))
    app.add_handler(CallbackQueryHandler(admin_rewards_panel,    pattern="^admin_rewards$"))
    app.add_handler(CallbackQueryHandler(admin_reward_delete,    pattern="^admin_rwd_del_"))
    app.add_handler(CallbackQueryHandler(admin_reward_detail,    pattern="^admin_rwd_"))

    # ALOO Payment admin panel handlers
    app.add_handler(CallbackQueryHandler(admin_set_upi,          pattern="^admin_set_upi$"))
    app.add_handler(CallbackQueryHandler(admin_set_qr,           pattern="^admin_set_qr$"))
    app.add_handler(CallbackQueryHandler(admin_recent_deposits,  pattern="^admin_recent_deposits$"))
    app.add_handler(CallbackQueryHandler(admin_set_timeout,      pattern="^admin_set_timeout$"))
    app.add_handler(CallbackQueryHandler(admin_min_qty_panel,    pattern="^admin_min_qty$"))
    app.add_handler(CallbackQueryHandler(admin_min_qty_edit,     pattern="^admin_min_qty_edit_"))

    # Payment Methods panel handlers (ALOO toggle + ZapUPI)
    app.add_handler(CallbackQueryHandler(admin_payment_methods,  pattern="^admin_payment_methods$"))
    app.add_handler(CallbackQueryHandler(admin_toggle_aloo,      pattern="^admin_toggle_aloo$"))
    app.add_handler(CallbackQueryHandler(admin_toggle_zapupi,    pattern="^admin_toggle_zapupi$"))
    app.add_handler(CallbackQueryHandler(admin_set_zapupi_key,   pattern="^admin_set_zapupi_key$"))

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

    # ALOO API is used for payment verification (no userbot needed)
    if not ALOO_API_KEY or not ALOO_MERCHANT_ID:
        logger.warning("⚠️ ALOO_API_KEY or ALOO_MERCHANT_ID not set! Payments will NOT verify automatically.")
    else:
        logger.info(f"✅ ALOO API configured (merchant: {ALOO_MERCHANT_ID})")

    # ZapUPI startup check
    _zap_key = get_zapupi_key()
    if _zap_key:
        logger.info("✅ ZapUPI API key configured.")
    else:
        logger.warning("⚠️ ZapUPI key not set — ZapUPI payments will fail if enabled.")

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

    import time as _restart_time
    import asyncio as _asyncio
    while True:
        try:
            loop = _asyncio.new_event_loop()
            _asyncio.set_event_loop(loop)
            main()
        except Exception as e:
            logger.error(f"Bot crashed: {e}. Restarting in 5s...")
            try:
                loop.close()
            except Exception:
                pass
            _restart_time.sleep(5)
