#!/bin/bash
set -e

echo "=== Production Startup ==="

# ── Persistent data directory (outside repo — never overwritten by deploys) ──
DATA_DIR="/home/runner/bot_data"
mkdir -p "$DATA_DIR"
echo "[0/3] Persistent data directory: $DATA_DIR"

# Migrate any existing data files from the old location (one-time migration)
for FILE in coupons.json users.json orders.json pending_orders.json; do
    SRC="/home/runner/workspace/telegram-bot/$FILE"
    DST="$DATA_DIR/$FILE"
    if [ ! -f "$DST" ] && [ -f "$SRC" ]; then
        cp "$SRC" "$DST"
        echo "      Migrated $FILE → $DATA_DIR"
    fi
done

# Install Python dependencies
echo "[1/3] Installing Python dependencies..."
pip install -q -r /home/runner/workspace/telegram-bot/requirements.txt 2>/dev/null || true

# Start Telegram bot in background
echo "[2/3] Starting Telegram bot..."
cd /home/runner/workspace/telegram-bot
export BOT_DATA_DIR="$DATA_DIR"
python bot.py &
BOT_PID=$!
echo "      Bot started (PID $BOT_PID)"

# Start Node.js API server in foreground (handles health checks)
echo "[3/3] Starting web server..."
cd /home/runner/workspace
exec node --enable-source-maps artifacts/api-server/dist/index.mjs
