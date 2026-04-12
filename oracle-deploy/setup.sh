#!/bin/bash
# ═══════════════════════════════════════════════════
#  Coupon Bot — Oracle Cloud Auto Setup Script
#  Run this on your Oracle Ubuntu 22.04 VM
#  Command: bash setup.sh
# ═══════════════════════════════════════════════════
set -e

echo ""
echo "╔══════════════════════════════════════╗"
echo "║    Coupon Bot — Oracle Cloud Setup   ║"
echo "╚══════════════════════════════════════╝"
echo ""

BOT_DIR="/home/ubuntu/coupon-bot"
DATA_DIR="/home/ubuntu/bot_data"
USER="ubuntu"

# ─── Step 1: System packages ───
echo "[1/7] System update aur packages install ho rahe hain..."
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip python3-venv git curl unzip 2>/dev/null
echo "      ✅ Done"

# ─── Step 2: Node.js 20 ───
echo "[2/7] Node.js 20 install ho raha hai..."
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - 2>/dev/null
sudo apt-get install -y -qq nodejs 2>/dev/null
echo "      ✅ Node $(node --version) installed"

# ─── Step 3: pnpm ───
echo "[3/7] pnpm install ho raha hai..."
sudo npm install -g pnpm --silent
echo "      ✅ pnpm $(pnpm --version) installed"

# ─── Step 4: Data directory ───
echo "[4/7] Data directory bana raha hai..."
mkdir -p "$DATA_DIR"
echo "      ✅ $DATA_DIR ready"

# ─── Step 5: Python dependencies ───
echo "[5/7] Python libraries install ho rahi hain..."
pip3 install -q -r "$BOT_DIR/telegram-bot/requirements.txt" 2>/dev/null || echo "      ⚠️ Bot files abhi nahi mili, baad mein install hogi"
echo "      ✅ Done"

# ─── Step 6: API Server build ───
echo "[6/7] API Server build ho raha hai..."
if [ -d "$BOT_DIR/artifacts/api-server" ]; then
    cd "$BOT_DIR"
    pnpm install --silent 2>/dev/null || true
    pnpm --filter @workspace/api-server run build 2>/dev/null || true
    echo "      ✅ API Server built"
else
    echo "      ⚠️ API server files abhi nahi mili"
fi

# ─── Step 7: Systemd services ───
echo "[7/7] Bot services setup ho rahe hain..."

# Bot service
sudo tee /etc/systemd/system/coupon-bot.service > /dev/null << EOF
[Unit]
Description=Coupon Telegram Bot
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$BOT_DIR/telegram-bot
Environment=BOT_DATA_DIR=$DATA_DIR
EnvironmentFile=$BOT_DIR/.env
ExecStart=/usr/bin/python3 bot.py
Restart=always
RestartSec=10
StandardOutput=append:$DATA_DIR/bot.log
StandardError=append:$DATA_DIR/bot.log

[Install]
WantedBy=multi-user.target
EOF

# API server service
sudo tee /etc/systemd/system/coupon-api.service > /dev/null << EOF
[Unit]
Description=Coupon Bot API Server
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$BOT_DIR
Environment=PORT=8080
Environment=NODE_ENV=production
Environment=BOT_DATA_DIR=$DATA_DIR
EnvironmentFile=$BOT_DIR/.env
ExecStart=/usr/bin/node --enable-source-maps artifacts/api-server/dist/index.mjs
Restart=always
RestartSec=10
StandardOutput=append:$DATA_DIR/api.log
StandardError=append:$DATA_DIR/api.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable coupon-bot coupon-api
echo "      ✅ Services registered"

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║  Setup complete! Ab yeh karo:               ║"
echo "║                                              ║"
echo "║  1. .env file mein tokens daalo:            ║"
echo "║     nano /home/ubuntu/coupon-bot/.env       ║"
echo "║                                              ║"
echo "║  2. Bot start karo:                         ║"
echo "║     sudo systemctl start coupon-bot         ║"
echo "║     sudo systemctl start coupon-api         ║"
echo "║                                              ║"
echo "║  3. Status check karo:                      ║"
echo "║     sudo systemctl status coupon-bot        ║"
echo "╚══════════════════════════════════════════════╝"
