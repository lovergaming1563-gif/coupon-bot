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

# ── Merge initial_users.json into users.json (adds missing users, never deletes) ──
INITIAL_USERS="/home/runner/workspace/telegram-bot/initial_users.json"
USERS_FILE="$DATA_DIR/users.json"
if [ -f "$INITIAL_USERS" ]; then
    python3 - << 'PYEOF'
import json, os, sys

initial_path = "/home/runner/workspace/telegram-bot/initial_users.json"
users_path   = os.environ.get("BOT_DATA_DIR", "/home/runner/bot_data") + "/users.json"

with open(initial_path) as f:
    initial = json.load(f)

if os.path.exists(users_path):
    with open(users_path) as f:
        existing = json.load(f)
else:
    existing = {}

before = len(existing)
for uid, data in initial.items():
    if uid not in existing:
        existing[uid] = data

after = len(existing)
with open(users_path, "w") as f:
    json.dump(existing, f, ensure_ascii=False, indent=2)

print(f"      Users merged: {before} → {after} (+{after - before} restored)")
PYEOF
fi

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
