import os
import json
import logging
import asyncio
from datetime import datetime
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

PRODUCTS = {
    "coupon_100": {
        "name": "₹100 Myntra Coupon",
        "price": 35,
        "emoji": "🟢",
        "key": "coupon_100",
    },
    "coupon_150": {
        "name": "₹150 Myntra Coupon",
        "price": 30,
        "emoji": "🔵",
        "key": "coupon_150",
    },
}

LOW_STOCK_THRESHOLD = 5


def load_json(filepath: str, default) -> dict | list:
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


def save_coupons(coupons: dict) -> None:
    save_json(COUPONS_FILE, coupons)


def get_users() -> dict:
    return load_json(USERS_FILE, {})


def save_users(users: dict) -> None:
    save_json(USERS_FILE, users)


def get_orders() -> dict:
    return load_json(ORDERS_FILE, {})


def save_orders(orders: dict) -> None:
    save_json(ORDERS_FILE, orders)


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
    coupons = get_coupons()
    return len(coupons.get(product_key, []))


def get_stats() -> dict:
    orders = get_orders()
    completed = [o for o in orders.values() if o.get("status") == "approved"]
    pending = [o for o in orders.values() if o.get("status") == "pending"]
    total_revenue = sum(
        PRODUCTS[o["product"]]["price"] for o in completed if o.get("product") in PRODUCTS
    )
    return {
        "total_sold": len(completed),
        "total_revenue": total_revenue,
        "total_users": len(get_users()),
        "pending_count": len(pending),
    }


def get_low_stock_alert() -> str:
    alerts = []
    for key, product in PRODUCTS.items():
        stock = get_stock(key)
        if stock < LOW_STOCK_THRESHOLD:
            alerts.append(f"⚠️ *{product['name']}*: only {stock} left!")
    return "\n".join(alerts) if alerts else ""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    register_user(user)

    stock_100 = get_stock("coupon_100")
    stock_150 = get_stock("coupon_150")

    text = (
        "🎉 *Welcome to Coupon Store!*\n"
        "🔥 *Best Deals – Instant Delivery*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💸 ₹100 Myntra Coupon — *₹35 only*\n"
        "💸 ₹150 Myntra Coupon — *₹30 only*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ Fast Delivery  |  ✅ Trusted  |  🤖 Automatic System"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                f"🟢 Buy ₹100 Coupon  [{stock_100} left]",
                callback_data="buy_coupon_100",
            )
        ],
        [
            InlineKeyboardButton(
                f"🔵 Buy ₹150 Coupon  [{stock_150} left]",
                callback_data="buy_coupon_150",
            )
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)


async def buy_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    product_key = query.data.replace("buy_", "")
    product = PRODUCTS.get(product_key)

    if not product:
        await query.edit_message_text("❌ Product not found.")
        return

    stock = get_stock(product_key)
    if stock == 0:
        await query.edit_message_text(
            "😔 *Sorry!*\n\nThis product is currently *out of stock*.\nPlease check back later!",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    context.user_data["pending_product"] = product_key

    upi_id = "yourname@upi"
    text = (
        f"🧾 *Order Details*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎟 Product: *{product['name']}*\n"
        f"💰 Price: *₹{product['price']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📲 *Payment Details:*\n"
        f"🏦 UPI ID: `{upi_id}`\n"
        f"💵 Amount: *₹{product['price']}*\n\n"
        f"📸 *Please complete the payment and send your payment screenshot here.*\n\n"
        f"⚠️ After verification, your coupon will be delivered instantly!"
    )

    keyboard = [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_order")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("pending_product", None)
    await query.edit_message_text(
        "❌ *Order cancelled.*\n\nUse /start to browse again.",
        parse_mode=ParseMode.MARKDOWN,
    )


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

    stock = get_stock(product_key)
    if stock == 0:
        await update.message.reply_text(
            "😔 Sorry, this product just went out of stock. Please /start and choose another.",
            parse_mode=ParseMode.MARKDOWN,
        )
        context.user_data.pop("pending_product", None)
        return

    order_id = f"{user.id}_{int(datetime.now().timestamp())}"
    orders = get_orders()
    orders[order_id] = {
        "order_id": order_id,
        "user_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "product": product_key,
        "status": "pending",
        "timestamp": datetime.now().isoformat(),
        "file_id": update.message.photo[-1].file_id if update.message.photo else None,
    }
    save_orders(orders)

    context.user_data.pop("pending_product", None)

    await update.message.reply_text(
        "⏳ *Payment Under Review...*\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Your screenshot has been received!\n"
        "👨‍💼 Admin is verifying your payment.\n\n"
        "🕐 You will be notified once approved.\n"
        "━━━━━━━━━━━━━━━━━━━━",
        parse_mode=ParseMode.MARKDOWN,
    )

    admin_text = (
        f"🔔 *New Payment Screenshot!*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 User: [{user.first_name}](tg://user?id={user.id})\n"
        f"🆔 User ID: `{user.id}`\n"
        f"📦 Product: *{product['name']}*\n"
        f"💰 Amount: ₹{product['price']}\n"
        f"🕐 Time: {datetime.now().strftime('%d %b %Y, %I:%M %p')}\n"
        f"🔑 Order ID: `{order_id}`\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

    keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{order_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{order_id}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        if update.message.photo:
            await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=update.message.photo[-1].file_id,
                caption=admin_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN,
            )
        elif update.message.document:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=update.message.document.file_id,
                caption=admin_text,
                reply_markup=reply_markup,
                parse_mode=ParseMode.MARKDOWN,
            )
    except Exception as e:
        logger.error(f"Failed to forward to admin: {e}")


async def approve_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ You are not authorized.", show_alert=True)
        return

    order_id = query.data.replace("approve_", "")
    orders = get_orders()
    order = orders.get(order_id)

    if not order:
        await query.edit_message_caption("❌ Order not found.", parse_mode=ParseMode.MARKDOWN)
        return

    if order["status"] != "pending":
        await query.answer("This order has already been processed.", show_alert=True)
        return

    product_key = order["product"]
    coupons = get_coupons()
    product_coupons = coupons.get(product_key, [])

    if not product_coupons:
        await query.answer("⚠️ Out of stock! Cannot approve.", show_alert=True)
        return

    coupon_code = product_coupons.pop(0)
    coupons[product_key] = product_coupons
    save_coupons(coupons)

    order["status"] = "approved"
    order["coupon_code"] = coupon_code
    order["approved_at"] = datetime.now().isoformat()
    orders[order_id] = order
    save_orders(orders)

    product = PRODUCTS[product_key]
    try:
        await context.bot.send_message(
            chat_id=order["user_id"],
            text=(
                f"✅ *Payment Confirmed!*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎉 Your order has been approved!\n\n"
                f"🎟 *Product:* {product['name']}\n"
                f"🎁 *Your Coupon Code:*\n\n"
                f"`{coupon_code}`\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📋 *How to use:*\n"
                f"1. Open Myntra App\n"
                f"2. Add items to cart\n"
                f"3. Enter code at checkout\n\n"
                f"🙏 *Thank you for your purchase!*\n"
                f"⭐ Come back for more deals!"
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"Failed to send coupon to user {order['user_id']}: {e}")

    await query.edit_message_caption(
        f"✅ *Order Approved!*\n"
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


async def reject_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ You are not authorized.", show_alert=True)
        return

    order_id = query.data.replace("reject_", "")
    orders = get_orders()
    order = orders.get(order_id)

    if not order:
        await query.edit_message_caption("❌ Order not found.", parse_mode=ParseMode.MARKDOWN)
        return

    if order["status"] != "pending":
        await query.answer("This order has already been processed.", show_alert=True)
        return

    order["status"] = "rejected"
    order["rejected_at"] = datetime.now().isoformat()
    orders[order_id] = order
    save_orders(orders)

    product = PRODUCTS.get(order["product"], {})
    try:
        await context.bot.send_message(
            chat_id=order["user_id"],
            text=(
                f"❌ *Payment Not Verified*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"😔 We could not verify your payment screenshot.\n\n"
                f"📌 *Possible reasons:*\n"
                f"• Screenshot is unclear or invalid\n"
                f"• Wrong payment amount sent\n"
                f"• Payment not completed\n\n"
                f"💡 Please send the *correct payment screenshot* or\n"
                f"use /start to try again."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"Failed to notify user {order['user_id']}: {e}")

    await query.edit_message_caption(
        f"❌ *Order Rejected*\n"
        f"🆔 Order: `{order_id}`\n"
        f"👤 User: {order.get('first_name', 'N/A')}\n"
        f"📦 Product: {product.get('name', 'N/A')}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.* Admin only.", parse_mode=ParseMode.MARKDOWN)
        return

    stats = get_stats()
    low_stock = get_low_stock_alert()

    text = (
        f"📊 *ADMIN DASHBOARD*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 Total Coupons Sold: *{stats['total_sold']}*\n"
        f"💰 Total Earnings: *₹{stats['total_revenue']}*\n"
        f"👥 Total Users: *{stats['total_users']}*\n"
        f"⏳ Pending Orders: *{stats['pending_count']}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

    if low_stock:
        text += f"\n\n{low_stock}"

    keyboard = [
        [
            InlineKeyboardButton("📦 Stock", callback_data="admin_stock"),
            InlineKeyboardButton("⏳ Pending Orders", callback_data="admin_pending"),
        ],
        [
            InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
            InlineKeyboardButton("➕ Add Coupon", callback_data="admin_add_coupon"),
        ],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)


async def admin_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    coupons = get_coupons()
    lines = ["📦 *Current Stock*\n━━━━━━━━━━━━━━━━━━━━\n"]
    for key, product in PRODUCTS.items():
        stock = len(coupons.get(key, []))
        status = "🔴 Out of Stock" if stock == 0 else ("⚠️ Low Stock" if stock < LOW_STOCK_THRESHOLD else "✅ In Stock")
        lines.append(f"{product['emoji']} *{product['name']}*\n   Count: *{stock}* | {status}")

    keyboard = [[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]]
    await query.edit_message_text(
        "\n\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    stats = get_stats()
    orders = get_orders()

    completed = [o for o in orders.values() if o.get("status") == "approved"]
    rejected = [o for o in orders.values() if o.get("status") == "rejected"]
    pending = [o for o in orders.values() if o.get("status") == "pending"]

    sold_by_product = {}
    revenue_by_product = {}
    for o in completed:
        pk = o.get("product", "unknown")
        sold_by_product[pk] = sold_by_product.get(pk, 0) + 1
        revenue_by_product[pk] = revenue_by_product.get(pk, 0) + PRODUCTS.get(pk, {}).get("price", 0)

    lines = [
        "📊 *Detailed Statistics*",
        "━━━━━━━━━━━━━━━━━━━━",
        f"👥 Total Users: *{stats['total_users']}*",
        f"📦 Total Orders: *{len(orders)}*",
        f"✅ Completed: *{len(completed)}*",
        f"❌ Rejected: *{len(rejected)}*",
        f"⏳ Pending: *{len(pending)}*",
        f"💰 Total Revenue: *₹{stats['total_revenue']}*",
        "",
        "📈 *By Product:*",
    ]
    for key, product in PRODUCTS.items():
        lines.append(
            f"  {product['emoji']} {product['name']}: "
            f"*{sold_by_product.get(key, 0)} sold* | ₹{revenue_by_product.get(key, 0)}"
        )

    keyboard = [[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]]
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    orders = get_orders()
    pending = {oid: o for oid, o in orders.items() if o.get("status") == "pending"}

    if not pending:
        keyboard = [[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]]
        await query.edit_message_text(
            "✅ *No pending orders right now!*\n\nAll orders have been processed.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = [f"⏳ *Pending Orders ({len(pending)})*", "━━━━━━━━━━━━━━━━━━━━"]
    keyboard_rows = []
    for oid, o in list(pending.items())[:10]:
        product = PRODUCTS.get(o["product"], {})
        name = o.get("first_name", "Unknown")
        lines.append(f"• {name} → {product.get('name', 'N/A')}")
        keyboard_rows.append([
            InlineKeyboardButton(f"✅ {name}", callback_data=f"approve_{oid}"),
            InlineKeyboardButton(f"❌ Reject", callback_data=f"reject_text_{oid}"),
        ])

    keyboard_rows.append([InlineKeyboardButton("◀️ Back", callback_data="admin_back")])
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard_rows),
        parse_mode=ParseMode.MARKDOWN,
    )


async def reject_text_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    order_id = query.data.replace("reject_text_", "")
    orders = get_orders()
    order = orders.get(order_id)

    if not order or order["status"] != "pending":
        await query.answer("Order not found or already processed.", show_alert=True)
        return

    order["status"] = "rejected"
    order["rejected_at"] = datetime.now().isoformat()
    orders[order_id] = order
    save_orders(orders)

    product = PRODUCTS.get(order["product"], {})
    try:
        await context.bot.send_message(
            chat_id=order["user_id"],
            text=(
                f"❌ *Payment Not Verified*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"😔 We could not verify your payment screenshot.\n\n"
                f"💡 Please send the *correct payment screenshot* or\n"
                f"use /start to try again."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"Failed to notify user: {e}")

    await query.answer(f"✅ Order {order_id} rejected.", show_alert=True)


async def admin_add_coupon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    text = (
        "➕ *Add Coupons*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Use these commands to add coupons:\n\n"
        "📌 `/addcoupon coupon_100 CODE1 CODE2 CODE3`\n"
        "📌 `/addcoupon coupon_150 CODE1 CODE2`\n\n"
        "_Separate multiple codes with spaces._"
    )

    keyboard = [[InlineKeyboardButton("◀️ Back", callback_data="admin_back")]]
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )


async def add_coupon_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ *Access Denied.*", parse_mode=ParseMode.MARKDOWN)
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ *Usage:*\n`/addcoupon coupon_100 CODE1 CODE2`\nor\n`/addcoupon coupon_150 CODE1 CODE2`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    product_key = args[0]
    if product_key not in PRODUCTS:
        await update.message.reply_text(
            f"❌ Invalid product key. Use: `coupon_100` or `coupon_150`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    new_codes = args[1:]
    coupons = get_coupons()
    coupons.setdefault(product_key, [])
    coupons[product_key].extend(new_codes)
    save_coupons(coupons)

    product = PRODUCTS[product_key]
    await update.message.reply_text(
        f"✅ *{len(new_codes)} coupon(s) added!*\n\n"
        f"📦 Product: *{product['name']}*\n"
        f"📊 Total Stock: *{len(coupons[product_key])}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_broadcast_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    context.user_data["broadcast_mode"] = True

    keyboard = [[InlineKeyboardButton("◀️ Cancel", callback_data="admin_back_cancel_broadcast")]]
    await query.edit_message_text(
        "📢 *Broadcast Message*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "📝 *Send your broadcast message now.*\n\n"
        "It will be sent to all registered users.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != ADMIN_ID:
        return

    if not context.user_data.get("broadcast_mode"):
        return

    context.user_data.pop("broadcast_mode", None)
    message_text = update.message.text

    users = get_users()
    success = 0
    failed = 0

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
        f"📢 *Broadcast Complete!*\n\n"
        f"✅ Sent: *{success}*\n"
        f"❌ Failed: *{failed}*",
        parse_mode=ParseMode.MARKDOWN,
    )


async def admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.from_user.id != ADMIN_ID:
        return

    context.user_data.pop("broadcast_mode", None)

    stats = get_stats()
    low_stock = get_low_stock_alert()

    text = (
        f"📊 *ADMIN DASHBOARD*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📦 Total Coupons Sold: *{stats['total_sold']}*\n"
        f"💰 Total Earnings: *₹{stats['total_revenue']}*\n"
        f"👥 Total Users: *{stats['total_users']}*\n"
        f"⏳ Pending Orders: *{stats['pending_count']}*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    if low_stock:
        text += f"\n\n{low_stock}"

    keyboard = [
        [
            InlineKeyboardButton("📦 Stock", callback_data="admin_stock"),
            InlineKeyboardButton("⏳ Pending Orders", callback_data="admin_pending"),
        ],
        [
            InlineKeyboardButton("📊 Stats", callback_data="admin_stats"),
            InlineKeyboardButton("➕ Add Coupon", callback_data="admin_add_coupon"),
        ],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast")],
    ]
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user

    if user.id == ADMIN_ID and context.user_data.get("broadcast_mode"):
        await handle_broadcast_message(update, context)
        return

    if context.user_data.get("pending_product"):
        await update.message.reply_text(
            "📸 *Please send a payment screenshot* (photo) to proceed.\n\n"
            "Or use /start to go back to the main menu.",
            parse_mode=ParseMode.MARKDOWN,
        )


def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set!")
    if not ADMIN_ID:
        raise ValueError("TELEGRAM_ADMIN_ID environment variable not set!")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("addcoupon", add_coupon_command))

    application.add_handler(CallbackQueryHandler(buy_product, pattern="^buy_"))
    application.add_handler(CallbackQueryHandler(cancel_order, pattern="^cancel_order$"))
    application.add_handler(CallbackQueryHandler(approve_order, pattern="^approve_"))
    application.add_handler(CallbackQueryHandler(reject_order, pattern="^reject_[^t]"))
    application.add_handler(CallbackQueryHandler(reject_text_order, pattern="^reject_text_"))
    application.add_handler(CallbackQueryHandler(admin_stock, pattern="^admin_stock$"))
    application.add_handler(CallbackQueryHandler(admin_stats, pattern="^admin_stats$"))
    application.add_handler(CallbackQueryHandler(admin_pending, pattern="^admin_pending$"))
    application.add_handler(CallbackQueryHandler(admin_add_coupon, pattern="^admin_add_coupon$"))
    application.add_handler(CallbackQueryHandler(admin_broadcast_prompt, pattern="^admin_broadcast$"))
    application.add_handler(CallbackQueryHandler(admin_back, pattern="^admin_back"))

    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.ALL, handle_screenshot)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    logger.info("🤖 Bot started!")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
