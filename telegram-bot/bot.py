import os
import json
import sqlite3
import logging
import threading
import html
from datetime import datetime, timezone
from flask import Flask
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

UPI_ID         = "k36672632@okicici"         # UPI ID
SUPPORT_HANDLE = "@MyntraCouponsupport_bot"  # Support Telegram handle
QR_IMAGE_PATH  = "qr_code.jpg"              # QR code image (already loaded)

# ─────────────── Referral & Channel Config ───────────────
CHANNEL_USERNAME    = "@withoutanyinvestmentwork"
CHANNEL_LINK        = "https://t.me/withoutanyinvestmentwork"
REFERRAL_GOAL       = 5                          # referrals needed for free coupon
REFERRAL_REWARD_KEY = "coupon_bigbasket_150"     # free coupon product key

# ─────────────── SQLite Referral DB ───────────────
REFERRAL_DB = os.path.join(
    os.environ.get("BOT_DATA_DIR", "/home/runner/bot_data"), "referrals.db"
)

def init_referral_db():
    con = sqlite3.connect(REFERRAL_DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            user_id      TEXT PRIMARY KEY,
            referred_by  TEXT NOT NULL,
            reward_given INTEGER NOT NULL DEFAULT 0,
            joined_at    TEXT
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

def db_insert_referral(user_id: str, referred_by: str) -> bool:
    """Insert referral. Returns True if new row inserted."""
    try:
        con = sqlite3.connect(REFERRAL_DB)
        con.execute(
            "INSERT INTO referrals (user_id, referred_by, reward_given, joined_at) VALUES (?,?,0,?)",
            (user_id, referred_by, datetime.now().isoformat())
        )
        con.commit()
        con.close()
        return True
    except sqlite3.IntegrityError:
        return False

def db_mark_reward_given(user_id: str):
    con = sqlite3.connect(REFERRAL_DB)
    con.execute("UPDATE referrals SET reward_given = 1 WHERE user_id = ?", (user_id,))
    con.commit()
    con.close()

def db_successful_referral_count(referrer_id: str) -> int:
    """Count of verified referrals (reward_given=1) for this referrer."""
    con = sqlite3.connect(REFERRAL_DB)
    count = con.execute(
        "SELECT COUNT(*) FROM referrals WHERE referred_by = ? AND reward_given = 1",
        (referrer_id,)
    ).fetchone()[0]
    con.close()
    return count

def db_total_referral_count(referrer_id: str) -> int:
    """Count of all referrals (pending + verified) for this referrer."""
    con = sqlite3.connect(REFERRAL_DB)
    count = con.execute(
        "SELECT COUNT(*) FROM referrals WHERE referred_by = ?",
        (referrer_id,)
    ).fetchone()[0]
    con.close()
    return count

# Data files stored in a persistent directory that survives deployments.
# Always stored outside the repo so code changes never wipe user data.
_DATA_DIR    = os.environ.get("BOT_DATA_DIR", "/home/runner/bot_data")
os.makedirs(_DATA_DIR, exist_ok=True)

# One-time migration: if data exists in old telegram-bot/ location, move it here
_OLD_DIR = os.path.dirname(os.path.abspath(__file__))
if _OLD_DIR != _DATA_DIR:
    for _f in ["coupons.json", "users.json", "orders.json", "pending_orders.json"]:
        _src = os.path.join(_OLD_DIR, _f)
        _dst = os.path.join(_DATA_DIR, _f)
        if os.path.exists(_src) and not os.path.exists(_dst):
            import shutil; shutil.copy2(_src, _dst)

COUPONS_FILE = os.path.join(_DATA_DIR, "coupons.json")
USERS_FILE   = os.path.join(_DATA_DIR, "users.json")
ORDERS_FILE  = os.path.join(_DATA_DIR, "orders.json")
PENDING_FILE = os.path.join(_DATA_DIR, "pending_orders.json")

ORDER_TIMEOUT_SECONDS  = 300   # 5 min auto-cancel
EXIT_TRAP_SECONDS      = 180   # 3 min urgency nudge
FAST_PAYMENT_THRESHOLD = 120   # < 2 min = 🔥 fast
LOW_STOCK_THRESHOLD    = 5

PRODUCTS = {
    "coupon_100": {"name": "₹100 Myntra Coupon", "price": 35, "emoji": "🟢"},
    "coupon_150": {"name": "₹150 Myntra Coupon", "price": 30, "emoji": "🔵"},
    "coupon_bigbasket_150": {"name": "₹150 BigBasket Cashback", "price": 30, "emoji": "🛒"},
}

def get_unit_price(product_key: str, quantity: int) -> int:
    """Returns unit price with tiered discounts for coupon_100."""
    if product_key == "coupon_100":
        if quantity >= 20:
            return 30
        elif quantity >= 10:
            return 32
        else:
            return 35
    return PRODUCTS[product_key]["price"]


QTY_EMOJIS = {
    1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣",
    6: "6️⃣", 7: "7️⃣", 8: "8️⃣", 9: "9️⃣", 10: "🔟",
}


# ─────────────── Flask keep-alive ───────────────

flask_app = Flask(__name__)

@flask_app.route("/")
def index():
    return "Bot is running ✅"

def keep_alive():
    port = int(os.environ.get("PORT", 3000))
    flask_app.run(host="0.0.0.0", port=port)


# ─────────────── JSON helpers ───────────────

def load_json(fp: str, default):
    try:
        with open(fp, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(fp: str, data) -> None:
    """Atomic write: write to temp file first, then rename to prevent corruption."""
    tmp = fp + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, fp)


def get_coupons()  -> dict: return load_json(COUPONS_FILE, {"coupon_100": [], "coupon_150": [], "coupon_bigbasket_150": []})
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
            "id":         user.id,
            "username":   user.username,
            "first_name": user.first_name,
            "joined":     datetime.now().isoformat(),
        }
        save_users(users)
        return True
    return False


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
    """Send a free coupon to the referrer. Called after every REFERRAL_GOAL verifications."""
    coupons = get_coupons()
    stock   = coupons.get(REFERRAL_REWARD_KEY, [])
    product = PRODUCTS[REFERRAL_REWARD_KEY]

    if stock:
        code = stock.pop(0)
        save_coupons(coupons)
        try:
            await context.bot.send_message(
                chat_id=int(referrer_id),
                text=(
                    f"🎁 *Congratulations\\! Free Coupon Earned\\!*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"You completed *{total_verified} verified referrals*\\! 🔥\n\n"
                    f"🎟 *{product['name']}*\n"
                    f"🔑 Your free coupon code:\n\n"
                    f"`{code}`\n\n"
                    f"Keep referring to earn more\\! 🚀"
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as e:
            logger.error(f"Referral reward send failed: {e}")
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"🎁 Referral reward sent!\nUser: {referrer_id} ({referrer_name})\nCoupon: {code}\nVerified referrals: {total_verified}",
            )
        except Exception:
            pass
    else:
        try:
            await context.bot.send_message(
                chat_id=int(referrer_id),
                text=(
                    f"🎁 *Congratulations! Free Coupon Earned!*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"You completed *{total_verified} verified referrals*! 🔥\n\n"
                    f"⏳ Your free *{product['name']}* is being prepared.\n"
                    f"You'll receive it shortly from admin!"
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"⚠️ Referral reward PENDING (stock empty)!\nUser: {referrer_id} ({referrer_name}) earned free {product['name']} after {total_verified} referrals.\nPlease add coupon stock & send manually!",
            )
        except Exception:
            pass


def get_stock(pk: str) -> int:
    return len(get_coupons().get(pk, []))


def get_stats() -> dict:
    orders    = get_orders()
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
    for k, p in PRODUCTS.items():
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


# ─────────────── /start ───────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    # ── Handle referral arg: /start ref_12345 ──
    args        = context.args or []
    referrer_id = None
    if args and args[0].startswith("ref_"):
        referrer_id = args[0][4:]  # strip "ref_" prefix

    is_new = register_user(user)

    # ── Record referral in SQLite (only for genuinely new users, no self-referral) ──
    show_referral_prompt = False
    if is_new and referrer_id and referrer_id != str(user.id):
        inserted = db_insert_referral(str(user.id), referrer_id)
        if inserted:
            show_referral_prompt = True  # show "join channel to activate" prompt

    cancel_user_timers(context, user.id)
    clear_user_order_state(context)

    s100 = get_stock("coupon_100")
    s150 = get_stock("coupon_150")
    s_bb = get_stock("coupon_bigbasket_150")

    text = (
        "🎉 *Welcome to Coupon Store*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔥 *Best Deals Available*\n\n"
        "💸 ₹100 Myntra Coupon — *₹35 only*\n"
        "💸 ₹150 Myntra Coupon — *₹30 only*\n"
        "🛒 ₹150 BigBasket Cashback — *₹30 only*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ Instant Delivery  |  ✅ Trusted  |  💬 24/7 Support"
    )
    keyboard = [
        [InlineKeyboardButton(
            f"🟢 ₹100 Myntra – ₹35  [{s100} left]" if s100 > 0 else "🟢 ₹100 Myntra – Out of Stock",
            callback_data="buy_coupon_100",
        )],
        [InlineKeyboardButton(
            f"🔵 ₹150 Myntra – ₹30  [{s150} left]" if s150 > 0 else "🔵 ₹150 Myntra – Out of Stock",
            callback_data="buy_coupon_150",
        )],
        [InlineKeyboardButton(
            f"🛒 BigBasket ₹150 Cashback – ₹30  [{s_bb} left]" if s_bb > 0 else "🛒 BigBasket ₹150 Cashback – Out of Stock",
            callback_data="buy_coupon_bigbasket_150",
        )],
        [InlineKeyboardButton("🔗 Refer & Earn Free Coupon", callback_data="referral_menu")],
        [InlineKeyboardButton("📞 Contact Support", url="tg://openmessage?user_id=6724474397")],
    ]
    await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN,
    )

    # ── Referral prompt: show AFTER main menu (no reward yet) ──
    if show_referral_prompt:
        await update.message.reply_text(
            "🎁 *You were referred by a friend!*\n\n"
            "To activate the referral reward for your friend,\n"
            f"please join our channel:\n\n"
            f"👉 {CHANNEL_LINK}\n\n"
            "After joining, tap *✅ Verify* below.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Join Channel", url=CHANNEL_LINK)],
                [InlineKeyboardButton("✅ Verify & Activate Referral", callback_data="verify_referral")],
            ]),
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

    # Check actual channel membership via Telegram API
    is_member = await check_channel_membership(context.bot, user.id)

    # None = API error (bot not admin in channel or other Telegram error)
    if is_member is None:
        await query.edit_message_text(
            "⚠️ *Verification temporarily unavailable*\n\n"
            "Bot cannot verify channel membership right now.\n\n"
            "Please contact admin to manually verify:\n"
            f"👉 @MyntraCouponsupport\\_bot\n\n"
            "_Reason: Bot needs to be admin of the channel._",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔁 Try Again", callback_data="verify_referral")],
            ]),
            parse_mode=ParseMode.MARKDOWN,
        )
        # Alert admin about the issue
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"⚠️ Referral verify failed for user {user.id} ({user.first_name})\nBot is NOT admin of {CHANNEL_USERNAME} — please add bot as admin!",
            )
        except Exception:
            pass
        return

    if is_member is False:
        await query.edit_message_text(
            "❌ *You haven't joined the channel yet!*\n\n"
            f"Please join first:\n👉 {CHANNEL_LINK}\n\n"
            "After joining, tap ✅ Verify again.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📢 Join Channel", url=CHANNEL_LINK)],
                [InlineKeyboardButton("✅ Verify Again", callback_data="verify_referral")],
            ]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ✅ Channel joined + reward not given → process reward
    db_mark_reward_given(uid)

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
    bot_username   = (await context.bot.get_me()).username
    ref_link       = f"https://t.me/{bot_username}?start=ref_{uid}"
    progress_bar   = "✅" * progress + "⬜" * (REFERRAL_GOAL - progress)

    text = (
        "🔗 *Refer & Earn Free Coupon*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎁 Reward: *Free ₹150 BigBasket Cashback Coupon*\n"
        f"🎯 Goal: *{REFERRAL_GOAL} verified referrals* = 1 free coupon\n\n"
        f"📊 Verified: *{progress}/{REFERRAL_GOAL}*\n"
        f"{progress_bar}\n\n"
        f"📥 Total referred: *{total_pending}*  |  ✅ Verified: *{total_verified}*\n\n"
        f"📤 *Your Referral Link:*\n"
        f"`{ref_link}`\n\n"
        f"_Share this link. Your friend must join the channel & tap Verify to count._"
    )
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
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
    bot_username   = (await context.bot.get_me()).username
    ref_link       = f"https://t.me/{bot_username}?start=ref_{uid}"
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
    s100    = get_stock("coupon_100")
    s150    = get_stock("coupon_150")
    s_bb = get_stock("coupon_bigbasket_150")
    text = (
        "🎉 *Welcome to Coupon Store*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔥 *Best Deals Available*\n\n"
        "💸 ₹100 Myntra Coupon — *₹35 only*\n"
        "💸 ₹150 Myntra Coupon — *₹30 only*\n"
        "🛒 ₹150 BigBasket Cashback — *₹30 only*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ Instant Delivery  |  ✅ Trusted  |  💬 24/7 Support"
    )
    keyboard = [
        [InlineKeyboardButton(
            f"🟢 ₹100 Myntra – ₹35  [{s100} left]" if s100 > 0 else "🟢 ₹100 Myntra – Out of Stock",
            callback_data="buy_coupon_100",
        )],
        [InlineKeyboardButton(
            f"🔵 ₹150 Myntra – ₹30  [{s150} left]" if s150 > 0 else "🔵 ₹150 Myntra – Out of Stock",
            callback_data="buy_coupon_150",
        )],
        [InlineKeyboardButton(
            f"🛒 BigBasket ₹150 Cashback – ₹30  [{s_bb} left]" if s_bb > 0 else "🛒 BigBasket ₹150 Cashback – Out of Stock",
            callback_data="buy_coupon_bigbasket_150",
        )],
        [InlineKeyboardButton("🔗 Refer & Earn Free Coupon", callback_data="referral_menu")],
        [InlineKeyboardButton("📞 Contact Support", url="tg://openmessage?user_id=6724474397")],
    ]
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN,
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

    await query.edit_message_text(
        f"📦 *Select Quantity*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{product['emoji']} *{product['name']}*\n"
        f"💰 Price per unit: *₹{product['price']}*\n"
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

    extra_tnc = ""
    if product_key == "coupon_bigbasket_150":
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
    pool     = coupons.get(pk, [])

    if len(pool) < quantity:
        return order, f"low_stock:{len(pool)}"

    assigned        = pool[:quantity]
    coupons[pk]     = pool[quantity:]
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

    product      = PRODUCTS[pk]
    coupon_lines = "\n".join(f"🎟 `{c}`" for c in assigned)
    try:
        await context.bot.send_message(
            chat_id=order["user_id"],
            text=(
                f"✅ *Payment Confirmed!*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎉 Your order has been *approved!*\n\n"
                f"📦 Product: *{product['name']}*\n"
                f"🔢 Quantity: *{quantity}*\n\n"
                f"🎁 *Your Coupon Code(s):*\n\n"
                f"{coupon_lines}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 *How to redeem:*\n"
                f"1. Open Myntra App\n"
                f"2. Add items to cart\n"
                f"3. Apply code at checkout\n\n"
                f"🙏 *Thank you for your purchase!*\n"
                f"⭐ Come back for more deals!"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"Coupon delivery failed for {order['user_id']}: {e}")

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
            InlineKeyboardButton("👥 Users",          callback_data="admin_users"),
            InlineKeyboardButton("📤 Export Users",   callback_data="admin_export_users"),
        ],
        [InlineKeyboardButton("📢 Broadcast",         callback_data="admin_broadcast")],
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
    coupons = get_coupons()
    lines   = ["📦 *Current Stock*\n━━━━━━━━━━━━━━━━━━━━\n"]
    for k, p in PRODUCTS.items():
        s      = len(coupons.get(k, []))
        status = "🔴 Out of Stock" if s == 0 else ("⚠️ Low" if s < LOW_STOCK_THRESHOLD else "✅ In Stock")
        lines.append(f"{p['emoji']} *{p['name']}*\n   Count: *{s}* — {status}")
    await query.edit_message_text(
        "\n\n".join(lines),
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
    await query.edit_message_text(
        "➕ *Add Coupons*\n━━━━━━━━━━━━━━━━━━━━\n\n"
        "*Myntra ₹100:*\n`/addcoupon coupon_100 CODE1 CODE2`\n\n"
        "*Myntra ₹150:*\n`/addcoupon coupon_150 CODE1 CODE2`\n\n"
        "*BigBasket ₹150:*\n`/addcoupon coupon_bigbasket_150 CODE1 CODE2`\n\n"
        "_Ek saath multiple codes space se alag karke add karo._",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]]),
        parse_mode=ParseMode.MARKDOWN,
    )


async def add_coupon_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ *Usage:*\n`/addcoupon coupon_100 CODE1 CODE2`\n`/addcoupon coupon_150 CODE1 CODE2`\n`/addcoupon coupon_bigbasket_150 CODE1 CODE2`", parse_mode=ParseMode.MARKDOWN,
        )
        return
    pk = args[0]
    if pk not in PRODUCTS:
        await update.message.reply_text(
            "❌ Invalid key. Use `coupon_100`, `coupon_150` or `coupon_bigbasket_150`", parse_mode=ParseMode.MARKDOWN,
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


# ─────────────── Main ───────────────

def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set!")
    if not ADMIN_ID:
        raise ValueError("TELEGRAM_ADMIN_ID environment variable not set!")

    # Initialise SQLite referral database
    init_referral_db()

    # Ensure data files exist
    for fp, default in [
        (COUPONS_FILE, {"coupon_100": [], "coupon_150": [], "coupon_bigbasket_150": []}),
        (USERS_FILE,   {}),
        (ORDERS_FILE,  {}),
        (PENDING_FILE, {}),
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

    # Inline buttons — buy & quantity
    app.add_handler(CallbackQueryHandler(support,          pattern="^support$"))
    app.add_handler(CallbackQueryHandler(back_to_start,    pattern="^back_to_start$"))
    app.add_handler(CallbackQueryHandler(verify_referral,  pattern="^verify_referral$"))
    app.add_handler(CallbackQueryHandler(referral_menu,    pattern="^referral_menu$"))
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
    app.add_handler(CallbackQueryHandler(admin_export_users,     pattern="^admin_export_users$"))
    app.add_handler(CallbackQueryHandler(admin_pending,          pattern="^admin_pending$"))
    app.add_handler(CallbackQueryHandler(admin_add_coupon,       pattern="^admin_add_coupon$"))
    app.add_handler(CallbackQueryHandler(admin_broadcast_prompt, pattern="^admin_broadcast$"))
    app.add_handler(CallbackQueryHandler(admin_back,             pattern="^admin_back"))

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
    # Kill any duplicate instance first — prevents 409 Conflict
    acquire_pid_lock()

    # Only start Flask keep-alive in non-production environments.
    # In production, Node.js handles health checks on PORT; Flask would conflict.
    is_production = os.environ.get("NODE_ENV") == "production"
    if not is_production:
        flask_port = int(os.environ.get("FLASK_PORT", 3000))
        threading.Thread(target=lambda: flask_app.run(host="0.0.0.0", port=flask_port), daemon=True).start()
        logger.info(f"🌐 Keep-alive server started on port {flask_port}")

    main()
