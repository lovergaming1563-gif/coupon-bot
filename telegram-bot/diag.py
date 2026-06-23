import os
import sys
import json
from datetime import datetime, timezone
from pymongo import MongoClient
import requests

# Enable UTF-8 encoding for console
sys.stdout.reconfigure(encoding="utf-8")

print("=== STARTING DIAGNOSTIC TRACE FOR ORDER HZ-FBQHM1 ===")

# 1. Connect to MongoDB
_MONGO_URI = os.environ.get("MONGODB_URI", "")
if not _MONGO_URI:
    print("ERROR: MONGODB_URI is not set in environment.")
    sys.exit(1)

client = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=5000)
db = client["hypes_zone_bot"]
print("Connected to MongoDB database successfully.")

# Target info
order_id = "HZ-FBQHM1"
utr = "987037523249"
tx_id = "T2606231557111451065330"

report = []
def log_report(msg):
    print(msg)
    report.append(msg)

log_report(f"<b>Diagnostic Trace Report for Order:</b> <code>{order_id}</code>")
log_report(f"<b>Time of analysis:</b> {datetime.now(timezone.utc).isoformat()}")

# ----------------- Check 1: Search payment_orders -----------------
log_report("\n--- 1. SEARCHING PAYMENT ORDERS ---")
order_doc = db["payment_orders"].find_one({"order_id": order_id})
if order_doc:
    log_report("<b>Document found in payment_orders!</b>")
    # Pretty print full document
    log_report(f"Full Document:\n<pre>{json.dumps(order_doc, indent=2, default=str)}</pre>")
    log_report(f"Current Status: <code>{order_doc.get('status')}</code>")
    log_report(f"Created At: <code>{order_doc.get('created_at')}</code>")
    log_report(f"Expires At: <code>{order_doc.get('expires_at')}</code>")
else:
    log_report("<b>Document NOT found in payment_orders.</b>")

# ----------------- Check 2: Search payment_transactions -----------------
log_report("\n--- 2. SEARCHING PAYMENT TRANSACTIONS ---")
tx_matches_order = list(db["payment_transactions"].find({"order_id": order_id}))
tx_matches_utr = list(db["payment_transactions"].find({"utr": utr}))
tx_matches_id = list(db["payment_transactions"].find({"transaction_id": tx_id}))

log_report(f"Transactions matching Order ID '{order_id}': {len(tx_matches_order)}")
for tx in tx_matches_order:
    log_report(f"<pre>{json.dumps(tx, indent=2, default=str)}</pre>")

log_report(f"Transactions matching UTR '{utr}': {len(tx_matches_utr)}")
for tx in tx_matches_utr:
    log_report(f"<pre>{json.dumps(tx, indent=2, default=str)}</pre>")

log_report(f"Transactions matching Tx ID '{tx_id}': {len(tx_matches_id)}")
for tx in tx_matches_id:
    log_report(f"<pre>{json.dumps(tx, indent=2, default=str)}</pre>")

# ----------------- Check 3: Search payment_audit_logs -----------------
log_report("\n--- 3. SEARCHING PAYMENT AUDIT LOGS ---")
audit_logs = list(db["payment_audit_logs"].find({"order_id": order_id}))
log_report(f"Audit logs matching Order ID '{order_id}': {len(audit_logs)}")
for al in sorted(audit_logs, key=lambda x: x.get("timestamp", "")):
    log_report(f"- [{al.get('timestamp')}] Status: <code>{al.get('status')}</code> | Reason: <code>{al.get('reason')}</code> | UTR: <code>{al.get('utr')}</code> | Tx ID: <code>{al.get('transaction_id')}</code>")

# Search by UTR and Transaction ID too in audit logs
audit_logs_utr = list(db["payment_audit_logs"].find({"utr": utr}))
log_report(f"\nAudit logs matching UTR '{utr}': {len(audit_logs_utr)}")
for al in audit_logs_utr:
    log_report(f"- [{al.get('timestamp')}] Order ID: <code>{al.get('order_id')}</code> | Status: <code>{al.get('status')}</code> | Reason: <code>{al.get('reason')}</code>")

audit_logs_tx = list(db["payment_audit_logs"].find({"transaction_id": tx_id}))
log_report(f"\nAudit logs matching Transaction ID '{tx_id}': {len(audit_logs_tx)}")
for al in audit_logs_tx:
    log_report(f"- [{al.get('timestamp')}] Order ID: <code>{al.get('order_id')}</code> | Status: <code>{al.get('status')}</code> | Reason: <code>{al.get('reason')}</code>")

# ----------------- Check 4: Check if IMAP scan ran -----------------
log_report("\n--- 4. IMAP SCAN RUN ANALYSIS ---")
settings_doc = db["kv_store"].find_one({"_id": "auto_settings"})
settings = settings_doc.get("data", {}) if settings_doc else {}
email_enabled = settings.get("email_enabled")
email_user = settings.get("email_user")
log_report(f"Gmail IMAP scanning enabled (settings): <code>{email_enabled}</code>")
log_report(f"Configured Email User: <code>{email_user}</code>")

# Check recent FamPay verification audits
all_fampay_audits = list(db["payment_audit_logs"].find({}).sort("timestamp", -1).limit(10))
log_report(f"Recent database audits (any order): {len(all_fampay_audits)}")
for al in all_fampay_audits:
    log_report(f"- Order: <code>{al.get('order_id')}</code> | Status: <code>{al.get('status')}</code> | Reason: <code>{al.get('reason')}</code> | Time: {al.get('timestamp')}")

# ----------------- Check 5: Trace Status Transitions & Root Cause -----------------
log_report("\n--- 5. ROOT CAUSE ANALYSIS ---")
transitions = []
for al in sorted(audit_logs, key=lambda x: x.get("timestamp", "")):
    reason = al.get("reason", "")
    status = al.get("status", "")
    transitions.append(f"{status} ({reason})")

log_report("<b>Status Transition Trace:</b> " + " -> ".join(transitions) if transitions else "No transitions logged in database.")

# Determine root cause
root_cause = "Unknown"
if not order_doc:
    root_cause = "Order ID HZ-FBQHM1 does not exist in payment_orders database."
else:
    status = order_doc.get("status")
    if status == "Pending":
        expires_str = order_doc.get("expires_at")
        if expires_str:
            expires_at = datetime.fromisoformat(expires_str.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > expires_at:
                root_cause = "Order expired before payment verification completed or was matched."
            else:
                root_cause = "Order is still Pending. IMAP scanner has not detected the payment email or failed to match it."
        else:
            root_cause = "Order is Pending but has no expiration timestamp."
    elif status == "Processing":
        root_cause = "Order is stuck in Processing. This means a verification job started and locked it, but crashed or timed out before completion, or Gmail IMAP login timed out/failed."
    elif status == "Paid":
        root_cause = "Order was marked Paid but failed during coupon stock reservation/allocation (out of stock) or script crashed during delivery."
    elif status == "Reserved":
        root_cause = "Order coupons were reserved, but failed during Telegram message delivery to the user."
    elif status == "Delivered":
        root_cause = "Order was successfully delivered! If user didn't get it, check chat ID/block status."

log_report(f"\n<b>Determined Root Cause:</b> {root_cause}")

used_amounts_doc = db["kv_store"].find_one({"_id": "used_amounts"})
used_amounts = used_amounts_doc.get("data", {}) if used_amounts_doc else {}
log_report(f"\n<b>Used Amounts Duplicate List:</b>")
for amt_str, val in used_amounts.items():
    if isinstance(val, dict) and (val.get("order_id") == order_id or val.get("utr") == utr):
        log_report(f"- Amount: {amt_str} | Data: <pre>{json.dumps(val)}</pre>")

# 6. Send report to Telegram Admin
token = os.environ.get("TELEGRAM_BOT_TOKEN")
admin_id = os.environ.get("TELEGRAM_ADMIN_ID", "6724474397")
admin_id_2 = os.environ.get("TELEGRAM_ADMIN_ID_2", "")

report_text = "\n".join(report)
chunks = [report_text[i:i+4000] for i in range(0, len(report_text), 4000)]

for chunk in chunks:
    for aid in [admin_id, admin_id_2]:
        if not aid: continue
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": int(aid),
            "text": chunk,
            "parse_mode": "HTML"
        }
        try:
            r = requests.post(url, json=payload, timeout=10)
            print(f"Sent diagnostic chunk to admin {aid}: status {r.status_code}")
        except Exception as e:
            print(f"Failed to send message to admin {aid}: {e}")

print("=== DIAGNOSTIC TRACE COMPLETE ===")
