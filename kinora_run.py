#!/usr/bin/env python3
"""
KINORA bitta buyruqda: server + tunnel + Telegram bot.

  python kinora_run.py

Avval ~/kinora_bot.txt fayliga BotFather bergan bot tokenini yozing:
  echo 123456:ABC... > ~/kinora_bot.txt

Tunnel manzili har safar yangi bo'ladi: skript uni o'zi topib, botning
menyu tugmasiga qo'yadi va /start ga "KINORA ochish" tugmasi bilan javob beradi.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

os.chdir(os.path.dirname(os.path.abspath(__file__)))
TOKEN = open("kinora_bot.txt", encoding="utf-8").read().strip() if os.path.exists("kinora_bot.txt") else ""
if not TOKEN:
    sys.exit("Bot tokeni yo'q:  echo TOKEN > ~/kinora_bot.txt")
API = f"https://api.telegram.org/bot{TOKEN}/"
URL = {"v": ""}


def tg(method, **data):
    req = urllib.request.Request(API + method, json.dumps(data).encode(),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)
    except Exception as e:
        print("  Telegram xato:", method, type(e).__name__)
        return {}


def button():
    return {"inline_keyboard": [[{"text": "🎬 KINORA ochish", "web_app": {"url": URL["v"]}}]]}


def poll():
    off = 0
    while True:
        r = tg("getUpdates", offset=off, timeout=30)
        for u in r.get("result", []):
            off = u["update_id"] + 1
            m = u.get("message") or {}
            if (m.get("text") or "").startswith("/start") and URL["v"]:
                tg("sendMessage", chat_id=m["chat"]["id"],
                   text="Kinolar, seriallar va animelar bir joyda 🍿", reply_markup=button())
        if not r:
            time.sleep(5)


srv = subprocess.Popen([sys.executable, "kinora_server.py"])
tun = subprocess.Popen(["cloudflared", "tunnel", "--url", "http://localhost:8080"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    for line in tun.stdout:
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            URL["v"] = m.group(0)
            break
    if not URL["v"]:
        sys.exit("Tunnel manzili topilmadi (cloudflared o'rnatilganmi?)")
    tg("setChatMenuButton", menu_button={"type": "web_app", "text": "KINORA", "web_app": {"url": URL["v"]}})
    print("\nTayyor! Mini app manzili:", URL["v"])
    print("Telegramda botga /start yozing yoki menyu tugmasini bosing. To'xtatish: Ctrl+C\n")
    threading.Thread(target=poll, daemon=True).start()
    for _ in tun.stdout:       # tunnel ishlab turishi uchun
        pass
except KeyboardInterrupt:
    pass
finally:
    srv.terminate()
    tun.terminate()
