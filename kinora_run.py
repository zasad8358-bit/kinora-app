import os
import sys
import subprocess
import urllib.request
import json
import time
import re

# Tokenni olish (Render'da Environment Variable orqali, Termux'da fayl orqali)
if os.environ.get("RENDER"):
    TOKEN = os.environ.get("BOT_TOKEN", "")
    IS_RENDER = True
else:
    IS_RENDER = False
    try:
        with open(os.path.expanduser("~/kinora_bot.txt"), "r", encoding="utf-8") as f:
            TOKEN = f.read().strip()
    except:
        TOKEN = ""

if not TOKEN:
    print("XATO: Bot tokeni topilmadi!")
    sys.exit(1)

api_url = f"https://api.telegram.org/bot{TOKEN}/"

def tg(method, **data):
    req = urllib.request.Request(api_url + method, json.dumps(data).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except Exception as e:
        print("Telegram xato:", e)
        return {}

if not IS_RENDER:
    srv = subprocess.Popen([sys.executable, "kinora_server.py"])
    tun = subprocess.Popen(["cloudflared", "tunnel", "--url", "http://localhost:8080"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    
    url = ""
    for line in tun.stdout:
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            url = m.group(0)
            break
else:
    url = os.environ.get("RENDER_EXTERNAL_URL", "")

if url:
    print(f"Mini App havolasi: {url}")
    tg("setChatMenuButton", menu_button={"type": "web_app", "text": "KINORA", "web_app": {"url": url}})

if not IS_RENDER:
    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        srv.terminate()
        tun.terminate()
