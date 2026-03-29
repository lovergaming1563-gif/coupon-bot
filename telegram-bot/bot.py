import os
import json
import logging
import threading
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
ADMIN_ID   = int(os.environ.get("TELEGRAM_ADMIN_ID", "0"))

UPI_ID         = "k36672632@okicici"         # UPI ID
SUPPORT_HANDLE = "@MyntraCouponsupport_bot"  # Support Telegram handle
QR_IMAGE_PATH  = "qr_code.jpg"              # QR code image (already loaded)

COUPONS_FILE       = "coupons.json"
USERS_FILE         = "users.json"
ORDERS_FILE        = "orders.json"
PENDING_FILE       = "pending_orders.json"

ORDER_TIMEOUT_SECONDS  = 300   # 5 min auto-cancel
EXIT_TRAP_SECONDS      = 180   # 3 min urgency nudge
FAST_PAYMENT_THRESHOLD = 120   # < 2 min = 🔥 fast
LOW_STOCK_THRESHOLD    = 5

PRODUCTS = {
    "coupon_100": {"name": "₹100 Myntra Coupon", "price": 35, "emoji": "🟢"},
    "coupon_150": {"name": "₹150 Myntra Coupon", "price": 30, "emoji": "🔵"},
}

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
    flask_app.run(host="0.0.0.0", port=3000)


# ─────────────── JSON helpers ───────────────

def load_json(fp: str, default):
    try:
        with open(fp, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(fp: str, data) -> None:
    with open(fp, "w") as f:
        json.dump(data, f, indent=2)


def get_coupons()  -> dict: return load_json(COUPONS_FILE, {"coupon_100": [], "coupon_150": []})
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


def register_user(user) -> None:
    users = get_users()
    uid   = str(user.id)
    if uid not in users:
        users[uid] = {
            "id": user.id, "username": user.username,
            "first_name": user.first_name,
            "joined": datetime.now().isoformat(),
        }
        save_users(users)


def get_stock(pk: str) -> int:
    return len(get_coupons().get(pk, []))


def get_stats() -> dict:
    orders    = get_orders()
    completed = [o for o in orders.values() if o.get("status") == "approved"]
    pending   = [o for o in orders.values() if o.get("status") == "pending"]
    revenue   = sum(
        PRODUCTS[o["product"]]["price"] * o.get("quantity", 1)
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
    register_user(user)
    cancel_user_timers(context, user.id)
    clear_user_order_state(context)

    s100 = get_stock("coupon_100")
    s150 = get_stock("coupon_150")

    text = (
        "🎉 *Welcome to Coupon Store*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔥 *Best Deals Available*\n\n"
        "💸 ₹100 Myntra Coupon — *₹35 only*\n"
        "💸 ₹150 Myntra Coupon — *₹30 only*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ Instant Delivery  |  ✅ Trusted  |  💬 24/7 Support"
    )
    keyboard = [
        [InlineKeyboardButton(
            f"🟢 ₹100 Coupon – ₹35  [{s100} left]" if s100 > 0 else "🟢 ₹100 Coupon – Out of Stock",
            callback_data="buy_coupon_100",
        )],
        [InlineKeyboardButton(
            f"🔵 ₹150 Coupon – ₹30  [{s150} left]" if s150 > 0 else "🔵 ₹150 Coupon – Out of Stock",
            callback_data="buy_coupon_150",
        )],
        [InlineKeyboardButton("📞 Contact Support", url="tg://openmessage?user_id=6724474397")],
    ]
    await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN,
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


async def back_to_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    s100 = get_stock("coupon_100")
    s150 = get_stock("coupon_150")
    text = (
        "🎉 *Welcome to Coupon Store*\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔥 *Best Deals Available*\n\n"
        "💸 ₹100 Myntra Coupon — *₹35 only*\n"
        "💸 ₹150 Myntra Coupon — *₹30 only*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "⚡ Instant Delivery  |  ✅ Trusted  |  💬 24/7 Support"
    )
    keyboard = [
        [InlineKeyboardButton(
            f"🟢 ₹100 Coupon – ₹35  [{s100} left]" if s100 > 0 else "🟢 ₹100 Coupon – Out of Stock",
            callback_data="buy_coupon_100",
        )],
        [InlineKeyboardButton(
            f"🔵 ₹150 Coupon – ₹30  [{s150} left]" if s150 > 0 else "🔵 ₹150 Coupon – Out of Stock",
            callback_data="buy_coupon_150",
        )],
        [InlineKeyboardButton("📞 Contact Support", url="tg://openmessage?user_id=6724474397")],
    ]
    await query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Step 1: Product selected → quantity grid ───────────────

async def buy_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query       = update.callback_query
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
        InlineKeyboardButton(QTY_EMOJIS[q], callback_data=f"qty_{product_key}_{q}")
        for q in range(1, 6) if q <= stock
    ]
    row2 = [
        InlineKeyboardButton(QTY_EMOJIS[q], callback_data=f"qty_{product_key}_{q}")
        for q in range(6, 11) if q <= stock
    ]
    keyboard = []
    if row1: keyboard.append(row1)
    if row2: keyboard.append(row2)
    keyboard.append([InlineKeyboardButton("✏️ Custom Quantity", callback_data=f"custom_qty_{product_key}")])
    keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="back_to_start")])

    await query.edit_message_text(
        f"📦 *Select Quantity*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{product['emoji']} *{product['name']}*\n"
        f"💰 Price per unit: *₹{product['price']}*\n"
        f"📊 Stock available: *{stock}*\n\n"
        f"How many do you want?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Custom quantity prompt ───────────────

async def custom_qty_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query       = update.callback_query
    await query.answer()
    product_key = query.data.replace("custom_qty_", "")
    product     = PRODUCTS.get(product_key)
    if not product:
        return

    stock = get_stock(product_key)
    context.user_data["selected_product"]   = product_key
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

    ts    = now_ts()
    total = product["price"] * quantity
    context.user_data["pending_product"]  = product_key
    context.user_data["pending_quantity"] = quantity
    context.user_data["order_start_ts"]   = ts
    context.user_data.pop("awaiting_custom_qty", None)
    context.user_data.pop("selected_product", None)

    uid = update.effective_user.id

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

    qty_disp = QTY_EMOJIS.get(quantity, f"×{quantity}")
    summary_text = (
        f"🧾 *Order Summary*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎟 Product:    *{product['name']}*\n"
        f"📦 Quantity:   *{qty_disp} × {quantity}*\n"
        f"💰 Unit Price: ₹{product['price']}\n"
        f"💵 *Total:     ₹{total}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📲 *Payment Details*\n"
        f"🏦 UPI ID: `{UPI_ID}`\n"
        f"💵 Pay exactly: *₹{total}*\n\n"
        f"📸 *After payment, send your screenshot here.*\n\n"
        f"⏳ You have *5 minutes* to complete payment.\n"
        f"After that your order will be cancelled automatically."
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
    """Handle quantity button 1–10."""
    query = update.callback_query
    await query.answer()

    data        = query.data[4:]          # strip "qty_"
    last_under  = data.rfind("_")
    product_key = data[:last_under]
    quantity    = int(data[last_under + 1:])

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

    order_id = f"{user.id}_{int(now_ts())}"
    total    = product["price"] * quantity

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

    username_display = f"@{user.username}" if user.username else "No username"
    admin_text = (
        f"{plabel}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Name: [{user.first_name}](tg://user?id={user.id})\n"
        f"🔗 Username: {username_display}\n"
        f"🆔 User ID: `{user.id}`\n"
        f"📦 Product: *{product['name']}*\n"
        f"🔢 Quantity: *{quantity}*\n"
        f"💰 Total: *₹{total}*\n"
        f"⏱ Paid in: *{me}m {se}s*\n"
        f"🕐 {datetime.now().strftime('%d %b %Y, %I:%M %p')}\n"
        f"🔑 Order: `{order_id}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Use: `/approve {user.id}` to approve"
    )
    approve_kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{order_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{order_id}"),
    ]])

    try:
        if update.message.photo:
            await context.bot.send_photo(
                chat_id=ADMIN_ID, photo=update.message.photo[-1].file_id,
                caption=admin_text, reply_markup=approve_kb, parse_mode=ParseMode.MARKDOWN,
            )
        elif update.message.document:
            await context.bot.send_document(
                chat_id=ADMIN_ID, document=update.message.document.file_id,
                caption=admin_text, reply_markup=approve_kb, parse_mode=ParseMode.MARKDOWN,
            )
    except Exception as e:
        logger.error(f"Forward to admin failed: {e}")


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
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Not authorized.", show_alert=True)
        return

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
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Not authorized.", show_alert=True)
        return
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

    if not context.args:
        await update.message.reply_text(
            "❌ *Usage:* `/broadcast Your message here`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    message_text = " ".join(context.args)
    users        = get_users()
    success = failed = 0

    for uid in users:
        try:
            await context.bot.send_message(
                chat_id=int(uid),
                text=f"📢 *Announcement*\n━━━━━━━━━━━━━━━━━━━━\n\n{message_text}",
                parse_mode=ParseMode.MARKDOWN,
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
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
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
        "📌 `/addcoupon coupon_100 CODE1 CODE2`\n"
        "📌 `/addcoupon coupon_150 CODE1 CODE2`\n\n"
        "_Separate codes with spaces._",
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
            "❌ *Usage:* `/addcoupon coupon_100 CODE1 CODE2`", parse_mode=ParseMode.MARKDOWN,
        )
        return
    pk = args[0]
    if pk not in PRODUCTS:
        await update.message.reply_text(
            "❌ Invalid key. Use `coupon_100` or `coupon_150`", parse_mode=ParseMode.MARKDOWN,
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
                    text=f"📢 *Announcement*\n━━━━━━━━━━━━━━━━━━━━\n\n{update.message.text}",
                    parse_mode=ParseMode.MARKDOWN,
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

    # Ensure data files exist
    for fp, default in [
        (COUPONS_FILE, {"coupon_100": [], "coupon_150": []}),
        (USERS_FILE,   {}),
        (ORDERS_FILE,  {}),
        (PENDING_FILE, {}),
    ]:
        if not os.path.exists(fp):
            _save(fp, default)

    # Start Flask keep-alive in background thread
    threading.Thread(target=keep_alive, daemon=True).start()
    logger.info("🌐 Keep-alive server started on port 3000")

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("admin",      admin_panel))
    app.add_handler(CommandHandler("addcoupon",  add_coupon_command))
    app.add_handler(CommandHandler("approve",    approve_command))
    app.add_handler(CommandHandler("broadcast",  broadcast_command))

    # Inline buttons — buy & quantity
    app.add_handler(CallbackQueryHandler(support,           pattern="^support$"))
    app.add_handler(CallbackQueryHandler(back_to_start,     pattern="^back_to_start$"))
    app.add_handler(CallbackQueryHandler(buy_product,       pattern="^buy_"))
    app.add_handler(CallbackQueryHandler(custom_qty_prompt, pattern="^custom_qty_"))
    app.add_handler(CallbackQueryHandler(select_quantity,   pattern="^qty_"))
    app.add_handler(CallbackQueryHandler(cancel_order,      pattern="^cancel_order$"))

    # Approve / reject buttons
    app.add_handler(CallbackQueryHandler(approve_order_btn,  pattern="^approve_"))
    app.add_handler(CallbackQueryHandler(reject_order_btn,   pattern="^reject_(?!text_)"))
    app.add_handler(CallbackQueryHandler(reject_text_order,  pattern="^reject_text_"))

    # Admin panel navigation
    app.add_handler(CallbackQueryHandler(admin_stock,            pattern="^admin_stock$"))
    app.add_handler(CallbackQueryHandler(admin_stats,            pattern="^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_pending,          pattern="^admin_pending$"))
    app.add_handler(CallbackQueryHandler(admin_add_coupon,       pattern="^admin_add_coupon$"))
    app.add_handler(CallbackQueryHandler(admin_broadcast_prompt, pattern="^admin_broadcast$"))
    app.add_handler(CallbackQueryHandler(admin_back,             pattern="^admin_back"))

    # Media & text
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_screenshot))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    logger.info("🤖 Bot fully started — all systems active!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
