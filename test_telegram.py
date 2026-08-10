import os
import sys
import requests

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

missing = [name for name, val in [
    ("TELEGRAM_BOT_TOKEN", BOT_TOKEN),
    ("TELEGRAM_CHAT_ID", CHAT_ID),
] if not val]

if missing:
    print(f"Missing environment variables: {', '.join(missing)}")
    sys.exit(1)

url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
payload = {
    "chat_id": CHAT_ID,
    "text": "✅ Test alert from Stock Scanner — Telegram delivery is working!",
}

response = requests.post(url, json=payload, timeout=10)

if response.status_code == 200:
    print("Success: Telegram message sent.")
else:
    print(f"Failed to send Telegram message. Status: {response.status_code}")
    print(f"Response: {response.text}")
    sys.exit(1)
