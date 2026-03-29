#!/bin/bash
set -e

echo "=== Production Startup ==="

# Install Python dependencies
echo "[1/3] Installing Python dependencies..."
pip install -q -r /home/runner/workspace/telegram-bot/requirements.txt 2>/dev/null || true

# Start Telegram bot in background
echo "[2/3] Starting Telegram bot..."
cd /home/runner/workspace/telegram-bot
python bot.py &
BOT_PID=$!
echo "      Bot started (PID $BOT_PID)"

# Start Node.js API server in foreground (handles health checks)
echo "[3/3] Starting web server..."
cd /home/runner/workspace
exec node --enable-source-maps artifacts/api-server/dist/index.mjs
