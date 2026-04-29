"""
Pyrogram userbot for the Coupon Bot.

Why this exists:
    Telegram's Bot API does NOT deliver messages from OTHER BOTS to your bot.
    The SMS forwarder (a 3rd-party bot in the SMS group) is therefore invisible
    to the main coupon bot. A userbot (logged in with a USER account session)
    CAN see those messages, so we use it to read the SMS forwarder's posts and
    feed them into the existing pending_payments.json pipeline.

How it works:
    - Reads API_ID / API_HASH / SESSION_STRING from environment.
    - Connects via Pyrogram with an in-memory session string (no .session file
      written to disk, so Railway redeploys are clean).
    - Listens for messages in the configured `sms_group_id`.
    - Parses each message with the SAME parse_bank_sms() that the main bot
      uses, then appends the parsed UTR/amount into pending_payments.json.
    - The main bot's existing auto_pay_match_job (runs every 3 seconds)
      handles the actual matching against pending UTRs, user notification,
      coupon delivery, etc. We do NOT duplicate that logic here.

Run mode:
    Started by bot.py as a daemon thread with its own asyncio loop. If the
    env vars are missing, the userbot stays disabled and the bot continues
    to work normally (only SMS from non-bot senders will be read).
"""
import asyncio
import logging
import os
import re as _re
import threading

logger = logging.getLogger(__name__)

API_ID = int(os.getenv("API_ID", "0") or "0")
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")


def is_configured() -> bool:
    return bool(API_ID and API_HASH and SESSION_STRING)


async def _userbot_main() -> None:
    if not is_configured():
        logger.warning(
            "[USERBOT] Disabled — API_ID, API_HASH or SESSION_STRING env var not set."
        )
        return

    try:
        from pyrogram import Client, filters
        from pyrogram.types import Message
    except ImportError:
        logger.error(
            "[USERBOT] pyrogram not installed — run `pip install pyrogram tgcrypto`."
        )
        return

    # Lazy-import the main bot's helpers (avoids circular import at module load).
    from bot import (
        get_settings,
        parse_bank_sms,
        get_used_utrs,
        get_pending_payments,
        save_pending_payments,
        now_ts,
    )

    app = Client(
        "coupon_userbot",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        in_memory=True,
    )

    @app.on_message(filters.all)
    async def on_any_message(client, message: Message):  # type: ignore[no-untyped-def]
        try:
            if not message.chat:
                return
            chat_id = message.chat.id

            settings = get_settings()
            sms_grp = settings.get("sms_group_id")
            if sms_grp is None:
                return
            try:
                sms_grp_int = int(sms_grp)
            except (TypeError, ValueError):
                logger.warning(f"[USERBOT] sms_group_id is not an int: {sms_grp!r}")
                return
            if chat_id != sms_grp_int:
                return  # Not the SMS group — ignore.

            text = message.text or message.caption or ""
            if not text:
                return

            # Sender whitelist (if configured) — same logic as sms_group_handler.
            senders = settings.get("allowed_senders", [])
            sender_id = ""
            sender_m = _re.search(r"From:\s*([A-Z0-9-]+)", text)
            if sender_m:
                sender_id = sender_m.group(1)
            if senders and sender_id and sender_id not in senders:
                logger.info(
                    f"[USERBOT] SMS from non-whitelisted sender '{sender_id}' — ignored."
                )
                return

            utr, amount = parse_bank_sms(text)
            if not utr or amount is None:
                return  # Not a credit SMS we care about.

            if utr in get_used_utrs():
                logger.info(f"[USERBOT] UTR {utr} already used — skip.")
                return

            pps = get_pending_payments()
            if any(p.get("utr") == utr for p in pps):
                return  # Already in pending list — main bot may have already added it.

            pps.append({
                "utr": utr,
                "amount": amount,
                "sender": sender_id,
                "received_at": now_ts(),
                "sms_text": text[:500],
            })
            save_pending_payments(pps)
            logger.info(
                f"[USERBOT] 📨 SMS captured → UTR={utr} Amount=₹{amount} Sender={sender_id}"
            )
            # Matching to pending UTRs is done by auto_pay_match_job (every 3s).
        except BaseException as e:
            logger.error(f"[USERBOT] handler error ({type(e).__name__}): {e}", exc_info=True)

    logger.info("[USERBOT] Starting (Pyrogram client)...")
    await app.start()
    logger.info("[USERBOT] ✅ Connected — SMS forwarder bot messages will now be read!")
    # Keep the loop alive forever.
    await asyncio.Event().wait()


def run_userbot_in_thread() -> None:
    """Spawn the userbot in a daemon thread with its own asyncio loop.

    Safe to call when env vars are missing — it logs and returns without
    blocking the main bot from starting up.
    """
    if not is_configured():
        logger.warning(
            "[USERBOT] Skipping thread spawn — env vars missing "
            "(API_ID, API_HASH, SESSION_STRING)."
        )
        return

    def _runner() -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_userbot_main())
        except Exception as e:
            logger.error(f"[USERBOT] thread crashed: {e}", exc_info=True)

    threading.Thread(target=_runner, daemon=True, name="userbot").start()
    logger.info("[USERBOT] 🛰️ Background thread spawned")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    asyncio.run(_userbot_main())
