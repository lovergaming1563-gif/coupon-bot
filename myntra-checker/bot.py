#!/usr/bin/env python3
"""
Myntra Coupon Checker Bot
Checks if a Myntra coupon code is valid (no login required).
"""

import os
import json
import time
import asyncio
import logging
import requests
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]

# ─── Myntra session / API ──────────────────────────────────────────────────────

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": "https://www.myntra.com",
    "Referer": "https://www.myntra.com/",
    "x-meta-app": json.dumps({"deviceType": "WEBSITE"}),
    "x-myntra-abtest": "{}",
}

_INVALID_PHRASES = [
    "invalid", "not valid", "not found", "expired", "does not exist",
    "incorrect", "not applicable", "wrong coupon", "no such coupon",
    "coupon code is not", "invalid coupon", "coupon has expired",
]
_VALID_PHRASES = [
    "applied", "discount", "you save", "savings", "success",
    "congratulations", "off on", "cashback", "coupon applied",
]


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_BASE_HEADERS)
    try:
        s.get("https://www.myntra.com", timeout=10)
    except Exception:
        pass
    return s


def _get_sku(session: requests.Session) -> str | None:
    """Get any valid SKU from Myntra search (for guest cart)."""
    try:
        r = session.get(
            "https://www.myntra.com/gateway/v2/listing/search",
            params={"rawQuery": "tshirt men", "resultsPerPage": "1", "p": "1"},
            timeout=10,
        )
        data = r.json()
        products = data.get("searchData", {}).get("results", {}).get("products", [])
        if products:
            sizes = products[0].get("sizes", [])
            if sizes:
                return str(sizes[0].get("skuId", ""))
    except Exception:
        pass
    return None


def _add_to_cart(session: requests.Session, sku: str) -> bool:
    try:
        r = session.post(
            "https://www.myntra.com/gateway/v2/cart",
            json={"skuId": sku, "qty": 1},
            timeout=10,
        )
        return r.status_code in (200, 201)
    except Exception:
        return False


def _post_coupon(session: requests.Session, code: str) -> dict:
    for endpoint in [
        "https://www.myntra.com/gateway/v2/cart/p/coupon",
        "https://www.myntra.com/gateway/v2/cart/coupon",
    ]:
        try:
            r = session.post(
                endpoint,
                json={"couponCode": code},
                timeout=10,
            )
            try:
                body = r.json()
            except Exception:
                body = {}
            return {"status": r.status_code, "body": body, "endpoint": endpoint}
        except Exception as e:
            logger.warning("Endpoint %s failed: %s", endpoint, e)
    return {"status": 0, "body": {}}


def _parse_result(code: str, resp: dict) -> dict | None:
    """
    Returns dict if decision is clear, None if still uncertain.
    """
    body = resp.get("body", {})
    status = resp.get("status", 0)

    # Flatten all message-like fields
    raw = " ".join(str(v) for v in [
        body.get("errorMessage", ""),
        body.get("message", ""),
        body.get("resultMessage", ""),
        body.get("result", ""),
        body.get("successMessage", ""),
    ]).lower()

    if any(p in raw for p in _INVALID_PHRASES):
        return {
            "valid": False,
            "message": (
                body.get("errorMessage") or body.get("message") or "Invalid or expired coupon."
            ),
        }

    if any(p in raw for p in _VALID_PHRASES) or status == 200:
        disc = body.get("discount") or body.get("savings") or body.get("discountText") or ""
        return {
            "valid": True,
            "message": body.get("message") or body.get("successMessage") or "Coupon applied!",
            "discount": str(disc) if disc else None,
        }

    return None  # uncertain


def check_coupon(code: str) -> dict:
    code = code.strip().upper()
    if len(code) < 3:
        return {"valid": False, "message": "Code too short."}

    session = _new_session()

    # Pass 1: coupon endpoint without cart
    resp1 = _post_coupon(session, code)
    decision = _parse_result(code, resp1)
    if decision:
        return decision

    # Pass 2: add a product to cart, then try again
    sku = _get_sku(session)
    if sku:
        _add_to_cart(session, sku)
        time.sleep(0.8)
        resp2 = _post_coupon(session, code)
        decision = _parse_result(code, resp2)
        if decision:
            return decision

    if resp1.get("status") == 0:
        return {"valid": None, "message": "Connection error — try again."}

    return {
        "valid": None,
        "message": (
            f"Myntra ne clear response nahi diya (status {resp1['status']}). "
            f"Manually myntra.com pe check kar lo."
        ),
    }


# ─── Telegram handlers ─────────────────────────────────────────────────────────

WELCOME = (
    "🛍️ *Myntra Coupon Checker*\n\n"
    "Coupon code bhejo — valid hai ya invalid bata dunga!\n\n"
    "• *Single:* bas code bhejo — `SAVE150`\n"
    "• *Bulk:* multiple codes, ek line mein ek (max 10)\n\n"
    "_Note: Ye unofficially Myntra ka check karta hai, kabhi kabhi ⚠️ uncertain aa sakta hai_"
)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("/")]
    if not lines:
        return

    if len(lines) == 1:
        code = lines[0]
        msg = await update.message.reply_text(
            f"⏳ Checking `{code}`…", parse_mode=ParseMode.MARKDOWN
        )
        result = await asyncio.to_thread(check_coupon, code)

        if result["valid"] is True:
            disc = f"\n💰 *Discount:* `{result['discount']}`" if result.get("discount") else ""
            out = f"✅ *VALID*\n\n🏷️ Code: `{code}`{disc}\nℹ️ {result['message']}"
        elif result["valid"] is False:
            out = f"❌ *INVALID / EXPIRED*\n\n🏷️ Code: `{code}`\nℹ️ {result['message']}"
        else:
            out = (
                f"⚠️ *UNCERTAIN*\n\n🏷️ Code: `{code}`\n"
                f"ℹ️ {result['message']}\n\n"
                f"_Manually check: myntra.com_"
            )
        await msg.edit_text(out, parse_mode=ParseMode.MARKDOWN)

    else:
        codes = lines[:10]
        msg = await update.message.reply_text(f"⏳ Checking {len(codes)} codes…")

        results = await asyncio.gather(*[asyncio.to_thread(check_coupon, c) for c in codes])

        rows = [f"*{len(codes)} codes checked:*\n"]
        for code, res in zip(codes, results):
            icon = "✅" if res["valid"] is True else ("❌" if res["valid"] is False else "⚠️")
            label = "Valid" if res["valid"] is True else ("Invalid" if res["valid"] is False else "Uncertain")
            rows.append(f"{icon} `{code}` — {label}")

        await msg.edit_text("\n".join(rows), parse_mode=ParseMode.MARKDOWN)


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Myntra Coupon Checker Bot started.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
