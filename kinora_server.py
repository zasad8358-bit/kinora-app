import asyncio, hashlib, hmac, json, mimetypes, os, re, sys, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetChannelsRequest
from telethon import Button
from telethon.tl.types import InputChannel

os.chdir(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("PORT", 8080))
WEB = "web"
CATALOG = "catalog.json"
CHUNK = 512 * 1024
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = int(os.environ["CHAT_ID"])
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")

client = TelegramClient(StringSession(), API_ID, API_HASH)
_lock = threading.Lock()
_peer = None
_msgs = {}


async def get_peer():
    global _peer
    if _peer:
        return _peer
    try:
        _peer = await client.get_input_entity(CHAT_ID)
        return _peer
    except ValueError:
        pass
    s = str(CHAT_ID)
    cid = int(s[4:]) if s.startswith("-100") else abs(CHAT_ID)
    res = await client(GetChannelsRequest([InputChannel(cid, 0)]))
    if not res.chats or not hasattr(res.chats[0], "access_hash"):
        raise RuntimeError("Guruh topilmadi: bot guruhda adminmi va CHAT_ID to'g'rimi?")
    _peer = utils.get_input_peer(res.chats[0])
    return _peer


async def get_msg(mid):
    if mid not in _msgs:
        if len(_msgs) > 500:
            _msgs.clear()
        _msgs[mid] = await client.get_messages(await get_peer(), ids=mid)
    return _msgs[mid]


def run(coro, timeout=90):
    return asyncio.run_coroutine_threadsafe(coro, client.loop).result(timeout)


@client.on(events.NewMessage(pattern=r"^/start", func=lambda e: e.is_private))
async def on_start(event):
    if WEBAPP_URL:
        await event.respond("KINORA ga xush kelibsiz!", buttons=[Button.url("🎬 Kinolarni ochish", WEBAPP_URL)])
    else:
        await event.respond("KINORA ishlayapti. (WEBAPP_URL sozlanmagan)")


@client.on(events.NewMessage(pattern=r"^/reload", func=lambda e: e.is_private))
async def on_reload(event):
    if event.sender_id != ADMIN_ID:
        return
    _msgs.clear()
    await event.respond("Kesh tozalandi ✅")


def valid_admin(init_data):
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        got = pairs.pop("hash", "")
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got):
            return False
        return json.loads(pairs.get("user", "{}")).get("id") == ADMIN_ID
    except Exception:
        return False


def save_edit(movie_id, fields):
    with _lock:
        with open(CATALOG, encoding="utf-8") as f:
            data = json.load(f)
        for m in data:
            if m.get("id") == movie_id:
                for k in ("title", "description", "poster"):
                    if k in fields:
                        v = str(fields[k]).strip()[:4000]
                        if k == "poster" and v and not v.startswith("https://"):
                            continue
                        m[k] = v
                break
        else:
            return False
        tmp = CATALOG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, CATALOG)
    return True


def parse_range(header, size):
    if not header:
        return 0, size - 1, False
    m = re.match(r"bytes=(\d*)-(\d*)", header)
    if not m or (not m.group(1) and not m.group(2)):
        return 0, size - 1, False
    if m.group(1):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
    else:
        start = max(0, size - int(m.group(2)))
        end = size - 1
    return start, min(end, size - 1), True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/healthz":
                return self._send(200, b"ok", "text/plain")
            if path == "/api/movies":
                data = open(CATALOG, "rb").read() if os.path.exists(CATALOG) else b"[]"
                return self._send(200, data, "application/json; charset=utf-8")
            if path.startswith("/stream/"):
                return self.stream(int(path.split("/")[2]))
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            full = os.path.normpath(os.path.join(WEB, rel))
            if not full.startswith(WEB + os.sep) or not os.path.isfile(full):
                return self._send(404, b"yo'q", "text/plain; charset=utf-8")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            return self._send(200, open(full, "rb").read(), ctype)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            print("Xato:", e)
            try:
                self._send(500, b"server xatosi", "text/plain; charset=utf-8")
            except Exception:
                pass

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path != "/api/edit":
                return self._send(404, b"yo'q", "text/plain; charset=utf-8")
            n = int(self.headers.get("Content-Length", 0))
            if n > 100000:
                return self._send(413, b"katta", "text/plain; charset=utf-8")
            body = json.loads(self.rfile.read(n) or b"{}")
            if not ADMIN_ID or not valid_admin(body.get("initData", "")):
                return self._send(403, b"ruxsat yo'q", "text/plain; charset=utf-8")
            ok = save_edit(body.get("id"), body)
            return self._send(200 if ok else 404,
                              b'{"ok":true}' if ok else b'{"ok":false}', "application/json")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            print("Xato:", e)
            try:
                self._send(400, b"xato so'rov", "text/plain; charset=utf-8")
            except Exception:
                pass

    def stream(self, mid):
        m = run(get_msg(mid))
        if not m or not m.document:
            return self._send(404, b"video yo'q", "text/plain; charset=utf-8")
        size = m.document.size
        mime = m.document.mime_type or "video/mp4"
        start, end, partial = parse_range(self.headers.get("Range"), size)
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        aligned = start - start % CHUNK
        skip = start - aligned
        gen = client.iter_download(m.media, offset=aligned, request_size=CHUNK, file_size=size)

        async def nxt():
            return await gen.__anext__()

        sent = 0
        try:
            while sent < length:
                try:
                    chunk = run(nxt())
                except StopAsyncIteration:
                    break
                if skip:
                    chunk, skip = chunk[skip:], 0
                chunk = chunk[:length - sent]
                self.wfile.write(chunk)
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                run(gen.close(), 10)
            except Exception:
                pass


def start_http():
    os.makedirs(WEB, exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    print(f"HTTP server: 0.0.0.0:{PORT}", flush=True)
    srv.serve_forever()


def main():
    threading.Thread(target=start_http, daemon=True).start()
    client.start(bot_token=BOT_TOKEN)
    print("Bot ishga tushdi", flush=True)
    try:
        client.loop.run_until_complete(get_peer())
        print("Guruh topildi", flush=True)
    except Exception as e:
        print("OGOHLANTIRISH:", e, flush=True)
    client.run_until_disconnected()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
