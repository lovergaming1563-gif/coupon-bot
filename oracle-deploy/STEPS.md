# Oracle Cloud Free VM — Step by Step Guide

## PART 1: Oracle Account Banao (10 min)

1. Jaao: https://cloud.oracle.com
2. "Start for free" click karo
3. Apna naam, email, password daalo
4. Country: India select karo
5. Credit/Debit card add karo (CHARGE NAHI HOGA — sirf verify ke liye)
6. Phone number verify karo
7. Account ready!

---

## PART 2: Free VM Banao (5 min)

Oracle Cloud dashboard mein:

1. Left menu → **Compute** → **Instances**
2. **"Create Instance"** button click karo
3. Settings:
   - Name: `coupon-bot`
   - Image: **Canonical Ubuntu 22.04** (change karo agar alag hai)
   - Shape: **"Change Shape"** click karo
     - Ampere → **VM.Standard.A1.Flex**
     - OCPU: **1**, Memory: **6 GB**
   - SSH Keys: **"Generate a key pair"** → **Download private key** (save karo!)
4. **Create** button dabao
5. 2-3 minute wait karo — status "Running" ho jayega
6. **Public IP address** copy karo (dashboard mein dikhega)

---

## PART 3: Port Open Karo (firewall)

VM list mein apna VM click karo:
1. **"Subnet"** link click karo
2. **"Default Security List"** click karo
3. **"Add Ingress Rules"** click karo
4. Yeh rules add karo:
   - Source: `0.0.0.0/0`, Port: `8080`
5. Save karo

---

## PART 4: VM se Connect Karo

Windows pe (PowerShell):
```
ssh -i C:\Users\TumharaName\Downloads\ssh-key.key ubuntu@VM_KA_IP
```

Mac/Linux pe (Terminal):
```
chmod 400 ~/Downloads/ssh-key.key
ssh -i ~/Downloads/ssh-key.key ubuntu@VM_KA_IP
```

---

## PART 5: Bot Files Upload Karo

Apne computer pe (naya terminal/PowerShell):
```
scp -i ssh-key.key -r /path/to/coupon-bot ubuntu@VM_KA_IP:/home/ubuntu/coupon-bot
```

Ya phir Replit se zip download karo aur upload karo:
```
scp -i ssh-key.key coupon-bot.zip ubuntu@VM_KA_IP:/home/ubuntu/
# VM pe:
cd /home/ubuntu && unzip coupon-bot.zip
```

---

## PART 6: Setup Script Chalao (VM pe)

```bash
cd /home/ubuntu/coupon-bot/oracle-deploy
bash setup.sh
```

---

## PART 7: Environment Variables Set Karo

```bash
cp /home/ubuntu/coupon-bot/oracle-deploy/env.template /home/ubuntu/coupon-bot/.env
nano /home/ubuntu/coupon-bot/.env
```

Yahan apna `TELEGRAM_BOT_TOKEN` aur `VM_KA_IP` daalo. Save karo: Ctrl+X → Y → Enter

---

## PART 8: Bot Start Karo

```bash
sudo systemctl start coupon-bot
sudo systemctl start coupon-api

# Check karo chal raha hai ya nahi:
sudo systemctl status coupon-bot
```

---

## PART 9: Data Restore Karo (old users/coupons)

```bash
# Purana data copy karo:
scp -i ssh-key.key /path/to/bot_data/* ubuntu@VM_KA_IP:/home/ubuntu/bot_data/
```

---

## Useful Commands

```bash
# Bot restart karo
sudo systemctl restart coupon-bot

# Logs dekho
tail -f /home/ubuntu/bot_data/bot.log

# Bot band karo
sudo systemctl stop coupon-bot
```
