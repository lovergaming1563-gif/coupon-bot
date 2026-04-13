# Railway Deployment — Step by Step Guide

## STEP 1: GitHub Account Banao (Free)
1. github.com pe jaao → Sign Up
2. Email + password → account ready

## STEP 2: Code GitHub pe Upload Karo
1. github.com pe login karo
2. "New Repository" → Name: `coupon-bot` → Private → Create
3. Replit mein yeh steps karo (Shell/Console mein):
```
git remote add railway https://github.com/TERA_USERNAME/coupon-bot.git
git push railway main
```

## STEP 3: Railway Account Banao (Free Trial)
1. railway.app pe jaao
2. "Start a New Project" → "Login with GitHub"
3. GitHub account se login karo — **koi card nahi chahiye**
4. $5 free trial milega automatically

## STEP 4: Project Deploy Karo
1. Railway dashboard → "New Project"
2. "Deploy from GitHub repo" → apna `coupon-bot` repo select karo
3. Railway automatically detect karega aur deploy karega

## STEP 5: Environment Variables Set Karo
Railway dashboard mein → Variables tab → yeh sab add karo:

```
TELEGRAM_BOT_TOKEN    = (Replit secrets se copy karo)
TELEGRAM_ADMIN_ID     = 6724474397
BOT_USERNAME          = MyntraCouponStores_bot
SESSION_SECRET        = (Replit secrets se copy karo)
REPLIT_DB_URL         = (Replit shell mein: echo $REPLIT_DB_URL)
BOT_DATA_DIR          = /app/telegram-bot/data
REFERRAL_BASE_URL     = https://TERA-RAILWAY-APP-URL.up.railway.app
```

## STEP 6: REPLIT_DB_URL Kaise Milega?
Replit mein Shell/Console open karo aur type karo:
```
echo $REPLIT_DB_URL
```
Jo URL aaye woh copy karke Railway ke REPLIT_DB_URL variable mein daalo.
Yeh important hai — isse tera data (users, coupons) Railway restarts pe save rahega!

## STEP 7: Deploy!
Variables save karo → Railway automatically redeploy karega → Bot chal jayega!

## STEP 8: Replit Deployment Band Karo
Railway pe bot chalne ke baad Replit ka deployment BAND karo — dono ek saath chalenge toh bot conflict karega.

## IMPORTANT: Dono Ek Saath Mat Chalao!
- Replit Deployment: BAND karo
- Railway: ON rakho
- Dono ek saath = bot crash

## STEP 9: REFERRAL_BASE_URL Update Karo
Railway ka URL milne ke baad (format: `xxx.up.railway.app`):
- Railway Variables mein `REFERRAL_BASE_URL` update karo
- Ya API server bhi Railway pe alag service ke roop mein deploy karo
