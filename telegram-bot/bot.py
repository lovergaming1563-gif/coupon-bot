import os
import json
import logging
import asyncio
from datetime import datetime, timezone
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log"),
    ],
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.environ.get("TELEGRAM_ADMIN_ID", "0"))

COUPONS_FILE = "coupons.json"
USERS_FILE = "users.json"
ORDERS_FILE = "orders.json"

ORDER_TIMEOUT_SECONDS = 300      # 5 minutes — auto-cancel if no screenshot
EXIT_TRAP_SECONDS = 180          # 3 minutes — send urgency nudge
FAST_PAYMENT_THRESHOLD = 120     # < 2 minutes = 🔥 FAST USER

LOW_STOCK_THRESHOLD = 5

UPI_ID = "yourname@upi"

PRODUCTS = {
    "coupon_100": {"name": "₹100 Myntra Coupon", "price": 35, "emoji": "🟢"},
    "coupon_150": {"name": "₹150 Myntra Coupon", "price": 30, "emoji": "🔵"},
}


# ─────────────── JSON helpers ───────────────

def load_json(filepath: str, default):
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(filepath: str, data) -> None:
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2)


def get_coupons() -> dict:
    return load_json(COUPONS_FILE, {"coupon_100": [], "coupon_150": []})


def save_coupons(c: dict) -> None:
    save_json(COUPONS_FILE, c)


def get_users() -> dict:
    return load_json(USERS_FILE, {})


def save_users(u: dict) -> None:
    save_json(USERS_FILE, u)


def get_orders() -> dict:
    return load_json(ORDERS_FILE, {})


def save_orders(o: dict) -> None:
    save_json(ORDERS_FILE, o)


# ─────────────── Utilities ───────────────

def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def register_user(user) -> None:
    users = get_users()
    uid = str(user.id)
    if uid not in users:
        users[uid] = {
            "id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "joined": datetime.now().isoformat(),
        }
        save_users(users)


def get_stock(product_key: str) -> int:
    return len(get_coupons().get(product_key, []))


def get_stats() -> dict:
    orders = get_orders()
    completed = [o for o in orders.values() if o.get("status") == "approved"]
    pending   = [o for o in orders.values() if o.get("status") == "pending"]
    revenue   = sum(PRODUCTS[o["product"]]["price"] for o in completed if o.get("product") in PRODUCTS)
    return {
        "total_sold":    len(completed),
        "total_revenue": revenue,
        "total_users":   len(get_users()),
        "pending_count": len(pending),
    }


def get_low_stock_alert() -> str:
    alerts = []
    for key, p in PRODUCTS.items():
        s = get_stock(key)
        if s < LOW_STOCK_THRESHOLD:
            alerts.append(f"⚠️ *{p['name']}*: only {s} left!")
    return "\n".join(alerts)


def cancel_user_timers(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    """Cancel any running order/exit-trap jobs for this user."""
    for key in (f"order_timer_{user_id}", f"exit_trap_{user_id}"):
        job = context.user_data.pop(key, None)
        if job:
            job.schedule_removal()


# ─────────────── Timer callbacks (JobQueue) ───────────────

async def order_expired(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Called after ORDER_TIMEOUT_SECONDS if no screenshot submitted."""
    job = context.job
    user_id    = job.data["user_id"]
    product_key = job.data["product_key"]

    ud = context.application.user_data.get(user_id, {})
    # If user already submitted screenshot, do nothing
    if ud.get("pending_product") != product_key:
        return

    ud.pop("pending_product", None)
    ud.pop("order_start_ts", None)

    try:
        await context.bot.send_message(
            chat_id=user_id,
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
        logger.error(f"order_expired notify error: {e}")


async def exit_trap_nudge(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Called after EXIT_TRAP_SECONDS of inactivity — urgency nudge."""
    job = context.job
    user_id     = job.data["user_id"]
    product_key = job.data["product_key"]

    ud = context.application.user_data.get(user_id, {})
    if ud.get("pending_product") != product_key:
        return

    remaining = int(ORDER_TIMEOUT_SECONDS - (now_ts() - ud.get("order_start_ts", now_ts())))
    mins, secs = divmod(max(remaining, 0), 60)

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "⚡ *Hurry! Your order is about to expire!*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                f"😱 Only *{mins}m {secs}s* left to complete payment!\n\n"
                "💳 Send your payment screenshot *right now*\n"
                "or lose this deal forever! 🔥"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"exit_trap_nudge error: {e}")


# ─────────────── /start ───────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    register_user(user)

    cancel_user_timers(context, user.id)
    context.user_data.pop("pending_product", None)
    context.user_data.pop("order_start_ts", None)

    s100 = get_stock("coupon_100")
    s150 = get_stock("coupon_150")

    text = (
        "🎉 *Welcome to Coupon Store!*\n"
        "🔥 *Limited Deals Available*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💸 ₹100 Myntra Coupon — *₹35 only*\n"
        "💸 ₹150 Myntra Coupon — *₹30 only*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ Instant Delivery  |  ✅ Trusted  |  🤖 Auto-System"
    )
    keyboard = [
        [InlineKeyboardButton(f"🟢 Buy ₹100 Coupon  [{s100} left]", callback_data="buy_coupon_100")],
        [InlineKeyboardButton(f"🔵 Buy ₹150 Coupon  [{s150} left]", callback_data="buy_coupon_150")],
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)


# ─────────────── Buy flow ───────────────

async def buy_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    product_key = query.data.replace("buy_", "")
    product = PRODUCTS.get(product_key)
    if not product:
        await query.edit_message_text("❌ Product not found.")
        return

    if get_stock(product_key) == 0:
        await query.edit_message_text(
            "😔 *Out of Stock!*\n\nThis product is currently unavailable.\nCheck back later!",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Cancel any old timers before starting new order
    cancel_user_timers(context, query.from_user.id)

    ts = now_ts()
    context.user_data["pending_product"] = product_key
    context.user_data["order_start_ts"]  = ts

    # Schedule 5-min auto-cancel
    timer_job = context.job_queue.run_once(
        order_expired,
        when=ORDER_TIMEOUT_SECONDS,
        data={"user_id": query.from_user.id, "product_key": product_key},
        name=f"order_{query.from_user.id}",
    )
    context.user_data[f"order_timer_{query.from_user.id}"] = timer_job

    # Schedule 3-min exit-trap nudge
    trap_job = context.job_queue.run_once(
        exit_trap_nudge,
        when=EXIT_TRAP_SECONDS,
        data={"user_id": query.from_user.id, "product_key": product_key},
        name=f"trap_{query.from_user.id}",
    )
    context.user_data[f"exit_trap_{query.from_user.id}"] = trap_job

    text = (
        f"⏳ *Order Created!*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🧾 Product: *{product['name']}*\n"
        f"💰 Price: *₹{product['price']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📲 *Payment Details:*\n"
        f"🏦 UPI ID: `{UPI_ID}`\n"
        f"💵 Amount: *₹{product['price']}*\n\n"
        f"⚠️ You have *ONLY 5 minutes* to complete payment.\n"
        f"After that your order will be *cancelled automatically*.\n\n"
        f"📸 *Send your payment screenshot to proceed.*"
    )
    keyboard = [[InlineKeyboardButton("❌ Cancel Order", callback_data="cancel_order")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    cancel_user_timers(context, query.from_user.id)
    context.user_data.pop("pending_product", None)
    context.user_data.pop("order_start_ts", None)
    await query.edit_message_text(
        "❌ *Order Cancelled.*\n\nUse /start to browse again.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Screenshot handler ───────────────

async def handle_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    if user.id == ADMIN_ID:
        return

    product_key = context.user_data.get("pending_product")
    if not product_key:
        return

    product = PRODUCTS.get(product_key)
    if not product:
        return

    if get_stock(product_key) == 0:
        await update.message.reply_text(
            "😔 *Sorry!* This product just went out of stock.\nUse /start to choose another.",
            parse_mode=ParseMode.MARKDOWN,
        )
        cancel_user_timers(context, user.id)
        context.user_data.pop("pending_product", None)
        return

    # Calculate elapsed time → determine priority
    start_ts  = context.user_data.get("order_start_ts", now_ts())
    elapsed   = now_ts() - start_ts
    is_fast   = elapsed < FAST_PAYMENT_THRESHOLD
    priority  = "fast" if is_fast else "normal"
    priority_label = "🔥 FAST PAYMENT" if is_fast else "🐢 NORMAL PAYMENT"

    # Cancel timers — screenshot received in time
    cancel_user_timers(context, user.id)
    context.user_data.pop("pending_product", None)
    context.user_data.pop("order_start_ts", None)

    order_id = f"{user.id}_{int(now_ts())}"
    orders = get_orders()
    orders[order_id] = {
        "order_id":    order_id,
        "user_id":     user.id,
        "username":    user.username,
        "first_name":  user.first_name,
        "product":     product_key,
        "status":      "pending",
        "priority":    priority,
        "elapsed_sec": round(elapsed),
        "timestamp":   datetime.now().isoformat(),
        "file_id":     update.message.photo[-1].file_id if update.message.photo else None,
    }
    save_orders(orders)

    mins_e, secs_e = divmod(int(elapsed), 60)
    await update.message.reply_text(
        "⏳ *Payment Under Review...*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Screenshot received!\n"
        "👨‍💼 Admin is verifying your payment.\n\n"
        "🕐 You will be notified once approved.\n"
        "━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Build admin caption
    admin_text = (
        f"{priority_label}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 User: [{user.first_name}](tg://user?id={user.id})\n"
        f"🆔 ID: `{user.id}`\n"
        f"📦 Product: *{product['name']}*\n"
        f"💰 Amount: ₹{product['price']}\n"
        f"⏱ Paid in: *{mins_e}m {secs_e}s*\n"
        f"🕐 Time: {datetime.now().strftime('%d %b %Y, %I:%M %p')}\n"
        f"🔑 Order: `{order_id}`\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = [[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_{order_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject_{order_id}"),
    ]]

    try:
        if update.message.photo:
            await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=update.message.photo[-1].file_id,
                caption=admin_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN,
            )
        elif update.message.document:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=update.message.document.file_id,
                caption=admin_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN,
            )
    except Exception as e:
        logger.error(f"Failed to forward to admin: {e}")


# ─────────────── Approve ───────────────

async def approve_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Not authorized.", show_alert=True)
        return

    order_id = query.data.replace("approve_", "")
    orders   = get_orders()
    order    = orders.get(order_id)

    if not order:
        await query.edit_message_caption("❌ Order not found.", parse_mode=ParseMode.MARKDOWN)
        return
    if order["status"] != "pending":
        await query.answer("Already processed.", show_alert=True)
        return

    product_key     = order["product"]
    coupons         = get_coupons()
    product_coupons = coupons.get(product_key, [])

    if not product_coupons:
        await query.answer("⚠️ Out of stock! Cannot approve.", show_alert=True)
        return

    coupon_code                = product_coupons.pop(0)
    coupons[product_key]       = product_coupons
    save_coupons(coupons)

    order["status"]      = "approved"
    order["coupon_code"] = coupon_code
    order["approved_at"] = datetime.now().isoformat()
    orders[order_id]     = order
    save_orders(orders)

    product = PRODUCTS[product_key]
    try:
        await context.bot.send_message(
            chat_id=order["user_id"],
            text=(
                f"✅ *Payment Confirmed!*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎉 Your order has been *approved*!\n\n"
                f"🎟 Product: *{product['name']}*\n"
                f"🎁 *Your Coupon Code:*\n\n"
                f"`{coupon_code}`\n\n"
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
        logger.error(f"Failed to send coupon to user {order['user_id']}: {e}")

    priority_label = "🔥 FAST" if order.get("priority") == "fast" else "🐢 NORMAL"
    await query.edit_message_caption(
        f"✅ *Order Approved!* [{priority_label}]\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Order: `{order_id}`\n"
        f"👤 User: {order.get('first_name', 'N/A')}\n"
        f"🎟 Coupon: `{coupon_code}`\n"
        f"📦 Product: {product['name']}",
        parse_mode=ParseMode.MARKDOWN,
    )

    low_stock = get_low_stock_alert()
    if low_stock:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ *Low Stock Alert!*\n━━━━━━━━━━━━━━\n{low_stock}",
            parse_mode=ParseMode.MARKDOWN,
        )


# ─────────────── Reject ───────────────

async def _do_reject(context, order_id: str, from_caption: bool = True):
    """Shared reject logic."""
    orders = get_orders()
    order  = orders.get(order_id)
    if not order or order["status"] != "pending":
        return None, "not_found"

    order["status"]      = "rejected"
    order["rejected_at"] = datetime.now().isoformat()
    orders[order_id]     = order
    save_orders(orders)

    try:
        await context.bot.send_message(
            chat_id=order["user_id"],
            text=(
                "❌ *Invalid Payment!*\n"
                "━━━━━━━━━━━━━━━━━━━━\n\n"
                "😔 We could not verify your payment.\n\n"
                "📌 *Possible reasons:*\n"
                "• Screenshot unclear or invalid\n"
                "• Wrong amount paid\n"
                "• Payment not completed\n\n"
                "💡 Send the *correct screenshot* or /start to retry."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"Failed to notify user: {e}")

    return order, "ok"


async def reject_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Not authorized.", show_alert=True)
        return

    order_id = query.data.replace("reject_", "")
    order, status = await _do_reject(context, order_id)

    if status == "not_found":
        await query.edit_message_caption("❌ Order not found or already processed.", parse_mode=ParseMode.MARKDOWN)
        return

    product = PRODUCTS.get(order["product"], {})
    await query.edit_message_caption(
        f"❌ *Order Rejected*\n"
        f"🆔 Order: `{order_id}`\n"
        f"👤 User: {order.get('first_name', 'N/A')}\n"
        f"📦 Product: {product.get('name', 'N/A')}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def reject_text_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reject from admin pending-orders list (no photo caption)."""
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    order_id = query.data.replace("reject_text_", "")
    _, status = await _do_reject(context, order_id)

    if status == "not_found":
        await query.answer("Order not found or already processed.", show_alert=True)
        return

    await query.answer(f"✅ Rejected.", show_alert=True)


# ─────────────── Admin panel ───────────────

def _admin_dashboard_text() -> str:
    stats     = get_stats()
    low_stock = get_low_stock_alert()
    text = (
        f"📊 *ADMIN DASHBOARD*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 Total Sold: *{stats['total_sold']}*\n"
        f"💰 Earnings: *₹{stats['total_revenue']}*\n"
        f"👥 Users: *{stats['total_users']}*\n"
        f"⏳ Pending: *{stats['pending_count']}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    if low_stock:
        text += f"\n\n{low_stock}"
    return text


def _admin_dashboard_keyboard():
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
        await update.message.reply_text("⛔ *Access Denied.* Admin only.", parse_mode=ParseMode.MARKDOWN)
        return
    await update.message.reply_text(
        _admin_dashboard_text(),
        reply_markup=_admin_dashboard_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    context.user_data.pop("broadcast_mode", None)
    await query.edit_message_text(
        _admin_dashboard_text(),
        reply_markup=_admin_dashboard_keyboard(),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Admin: Stock ───────────────

async def admin_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return

    coupons = get_coupons()
    lines = ["📦 *Current Stock*\n━━━━━━━━━━━━━━━━━━━━\n"]
    for key, p in PRODUCTS.items():
        s      = len(coupons.get(key, []))
        status = "🔴 Out of Stock" if s == 0 else ("⚠️ Low" if s < LOW_STOCK_THRESHOLD else "✅ In Stock")
        lines.append(f"{p['emoji']} *{p['name']}*\n   Count: *{s}* — {status}")

    await query.edit_message_text(
        "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]]),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Admin: Stats ───────────────

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return

    stats  = get_stats()
    orders = get_orders()
    completed = [o for o in orders.values() if o.get("status") == "approved"]
    rejected  = [o for o in orders.values() if o.get("status") == "rejected"]
    pending   = [o for o in orders.values() if o.get("status") == "pending"]
    fast      = [o for o in completed if o.get("priority") == "fast"]

    sold_rev = {}
    for o in completed:
        pk = o.get("product", "?")
        sold_rev.setdefault(pk, [0, 0])
        sold_rev[pk][0] += 1
        sold_rev[pk][1] += PRODUCTS.get(pk, {}).get("price", 0)

    lines = [
        "📊 *Detailed Statistics*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"👥 Total Users:    *{stats['total_users']}*",
        f"📦 Total Orders:   *{len(orders)}*",
        f"✅ Completed:      *{len(completed)}*",
        f"🔥 Fast Payers:    *{len(fast)}*",
        f"❌ Rejected:       *{len(rejected)}*",
        f"⏳ Pending:        *{len(pending)}*",
        f"💰 Total Revenue:  *₹{stats['total_revenue']}*",
        "",
        "📈 *By Product:*",
    ]
    for key, p in PRODUCTS.items():
        d = sold_rev.get(key, [0, 0])
        lines.append(f"  {p['emoji']} {p['name']}: *{d[0]} sold* | ₹{d[1]}")

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]]),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Admin: Pending orders (sorted by priority) ───────────────

async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return

    orders  = get_orders()
    pending = {oid: o for oid, o in orders.items() if o.get("status") == "pending"}

    if not pending:
        await query.edit_message_text(
            "✅ *No pending orders!*\n\nAll orders processed.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]]),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Sort: fast first, then normal
    sorted_orders = sorted(
        pending.items(),
        key=lambda x: (0 if x[1].get("priority") == "fast" else 1, x[1].get("timestamp", "")),
    )

    lines = [f"⏳ *Pending Orders ({len(pending)})*", "━━━━━━━━━━━━━━━━━━━━"]
    keyboard_rows = []

    for oid, o in sorted_orders[:10]:
        p      = PRODUCTS.get(o["product"], {})
        name   = o.get("first_name", "Unknown")
        label  = "🔥" if o.get("priority") == "fast" else "🐢"
        elapsed = o.get("elapsed_sec", 0)
        mins_e, secs_e = divmod(elapsed, 60)
        lines.append(f"{label} *{name}* — {p.get('name', 'N/A')} ⏱ {mins_e}m{secs_e}s")
        keyboard_rows.append([
            InlineKeyboardButton(f"✅ {label}{name}", callback_data=f"approve_{oid}"),
            InlineKeyboardButton("❌ Reject",         callback_data=f"reject_text_{oid}"),
        ])

    keyboard_rows.append([InlineKeyboardButton("◀️ Back", callback_data="admin_back")])
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard_rows),
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Admin: Add coupon ───────────────

async def admin_add_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    await query.edit_message_text(
        "➕ *Add Coupons*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Use these commands:\n\n"
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
            "❌ *Usage:*\n`/addcoupon coupon_100 CODE1 CODE2`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    product_key = args[0]
    if product_key not in PRODUCTS:
        await update.message.reply_text(
            "❌ Invalid key. Use `coupon_100` or `coupon_150`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    new_codes = args[1:]
    coupons = get_coupons()
    coupons.setdefault(product_key, []).extend(new_codes)
    save_coupons(coupons)

    p = PRODUCTS[product_key]
    await update.message.reply_text(
        f"✅ *{len(new_codes)} coupon(s) added!*\n\n"
        f"📦 Product: *{p['name']}*\n"
        f"📊 Total Stock: *{len(coupons[product_key])}*",
        parse_mode=ParseMode.MARKDOWN,
    )


# ─────────────── Admin: Broadcast ───────────────

async def admin_broadcast_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return
    context.user_data["broadcast_mode"] = True
    await query.edit_message_text(
        "📢 *Broadcast Message*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📝 *Type your message now.*\n"
        "It will be sent to all registered users.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Cancel", callback_data="admin_back")]]),
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.user_data.get("broadcast_mode"):
        return

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


# ─────────────── Fallback text handler ───────────────

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    if user.id == ADMIN_ID and context.user_data.get("broadcast_mode"):
        await handle_broadcast_message(update, context)
        return

    if context.user_data.get("pending_product"):
        await update.message.reply_text(
            "📸 *Please send a payment screenshot* (photo) to proceed.\n\n"
            "Or tap ❌ Cancel on the order message, or /start to go back.",
            parse_mode=ParseMode.MARKDOWN,
        )


# ─────────────── Main ───────────────

def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set!")
    if not ADMIN_ID:
        raise ValueError("TELEGRAM_ADMIN_ID not set!")

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",      start))
    app.add_handler(CommandHandler("admin",      admin_panel))
    app.add_handler(CommandHandler("addcoupon",  add_coupon_command))

    # Buy & cancel
    app.add_handler(CallbackQueryHandler(buy_product,   pattern="^buy_"))
    app.add_handler(CallbackQueryHandler(cancel_order,  pattern="^cancel_order$"))

    # Approve / reject (from photo caption or pending list)
    app.add_handler(CallbackQueryHandler(approve_order,    pattern="^approve_"))
    app.add_handler(CallbackQueryHandler(reject_order,     pattern="^reject_(?!text_)"))
    app.add_handler(CallbackQueryHandler(reject_text_order, pattern="^reject_text_"))

    # Admin panel navigation
    app.add_handler(CallbackQueryHandler(admin_stock,           pattern="^admin_stock$"))
    app.add_handler(CallbackQueryHandler(admin_stats,           pattern="^admin_stats$"))
    app.add_handler(CallbackQueryHandler(admin_pending,         pattern="^admin_pending$"))
    app.add_handler(CallbackQueryHandler(admin_add_coupon,      pattern="^admin_add_coupon$"))
    app.add_handler(CallbackQueryHandler(admin_broadcast_prompt, pattern="^admin_broadcast$"))
    app.add_handler(CallbackQueryHandler(admin_back,            pattern="^admin_back"))

    # Media & text
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_screenshot))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    logger.info("🤖 Bot started with timer + priority + exit-trap systems!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
