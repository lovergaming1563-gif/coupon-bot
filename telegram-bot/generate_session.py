"""
Run this ONCE on your local machine to generate a Pyrogram SESSION_STRING.
Then add the output as the SESSION_STRING environment variable on Railway.

Usage:  python generate_session.py

You will be asked for:
    - API ID    (from https://my.telegram.org/apps)
    - API Hash  (same place)
    - Phone number (in international format, e.g. +91XXXXXXXXXX)
    - Login code that Telegram sends to your account
    - 2FA password (only if you have one)

The same SESSION_STRING used by the OTP bot can be re-used here — just copy
it into the coupon bot's Railway env vars.
"""
import asyncio


async def main():
    try:
        from pyrogram import Client
    except ImportError:
        print("Install pyrogram first: pip install pyrogram tgcrypto")
        return

    api_id = int(input("API ID: ").strip())
    api_hash = input("API Hash: ").strip()

    async with Client(
        "gen_session",
        api_id=api_id,
        api_hash=api_hash,
        in_memory=True,
    ) as app:
        session_string = await app.export_session_string()

    print("\n" + "=" * 60)
    print("SESSION_STRING (copy this entire value):")
    print(session_string)
    print("=" * 60)
    print("\nAdd this as SESSION_STRING in your Railway environment variables.")


if __name__ == "__main__":
    asyncio.run(main())
