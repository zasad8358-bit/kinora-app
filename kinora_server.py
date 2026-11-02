#!/usr/bin/env python3
"""
KINORA server (Termux).

  python kinora_server.py build   -> yangi guruhdagi kinolardan katalog.json yig'adi
                                      (TMDB: poster, fon, logo, reyting, tavsif)
  python kinora_server.py         -> mini app + videoni Telegramdan oqim qilib beradi

Eslatma: kino7.py bilan BIR VAQTDA ishlatmang (ikkalasi bitta sessiyani ishlatadi).
"""
import ast
import asyncio
import json
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from telethon import TelegramClient

_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(_dir)

# ====== SOZLAMALAR ======
PORT = 8080
WEB = "web"                 # mini app fayllari (index.html) shu papkada
CATALOG = "catalog.json"
RAW = "raw_cache.json"       # Telegram papkalari ro'yxati (10 daqiqalik ish bir marta)
CACHE = "tmdb_cache.json"   # TMDB natijalari: qayta ishga tushirsa tez ketadi
OVERRIDES = {}
VER = "3"
WORKERS = 10                # TMDB so'rovlari bir vaqtda nechta
PROGRESS = "progress.json"
SESSION = "kino_session"
CHUNK = 512 * 1024          # Telegram yuklash bo'lagi (1 MB ga bo'linishi shart)
# ========================

# API_ID, API_HASH, NEW_TITLE eski.py dan olinadi (kino7.py dagidek)
with open("eski.py", encoding="utf-8") as f:
    _src = f.read()


def _get(name):
    m = re.search(rf"^{name} = (.+?)\s*(#.*)?$", _src, re.M)
    return ast.literal_eval(m.group(1))


API_ID = _get("API_ID")
API_HASH = _get("API_HASH")
NEW_TITLE = _get("NEW_TITLE")


def _tmdb_key():
    k = os.environ.get("TMDB_KEY")
    if not k and os.path.exists("tmdb.txt"):
        k = open("tmdb.txt", encoding="utf-8").read().strip()
    return k


TMDB_KEY = _tmdb_key()
client = TelegramClient(SESSION, API_ID, API_HASH)

GENRE_UZ = {
    "action": "Jangari", "adventure": "Sarguzasht", "animation": "Multfilm",
    "comedy": "Komediya", "crime": "Jinoyat", "documentary": "Hujjatli",
    "drama": "Drama", "family": "Oilaviy", "fantasy": "Fantastika",
    "history": "Tarixiy", "horror": "Qo'rqinchli", "music": "Musiqiy",
    "mystery": "Sirli", "romance": "Romantik", "science fiction": "Ilmiy fantastika",
    "thriller": "Triller", "war": "Urush", "western": "Vestern", "kids": "Bolalar",
    "action & adventure": "Jangari", "sci-fi & fantasy": "Fantastika",
    "war & politics": "Urush", "tv movie": "Telefilm",
}


# ---------------- TMDB ----------------

TMDB_HOST = "api.themoviedb.org"
TMDB_HOSTS = ["api.themoviedb.org", "api.tmdb.org"]   # ikkinchisi zaxira manzil


def pick_host():
    """Qaysi TMDB manzili ochilishini tekshiradi (VPN kerak bo'lishi mumkin)."""
    global TMDB_HOST
    for h in TMDB_HOSTS:
        TMDB_HOST = h
        t = time.time()
        try:
            urllib.request.urlopen(f"https://{h}/3/configuration?api_key={TMDB_KEY}", timeout=6)
            print(f"TMDB ochildi: {h} ({time.time() - t:.1f}s)")
            return True
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):    # server javob berdi, demak ochiq
                print(f"TMDB ochildi: {h}, lekin kalit xato bo'lishi mumkin (kod {e.code})")
                return True
        except Exception as e:
            print(f"TMDB ochilmadi: {h} ({type(e).__name__})")
    return False


class NetError(Exception):
    pass


def tmdb(path, **params):
    headers = {}
    if TMDB_KEY and TMDB_KEY.startswith("eyJ"):      # v4 token
        headers["Authorization"] = "Bearer " + TMDB_KEY
    else:
        params["api_key"] = TMDB_KEY
    url = f"https://{TMDB_HOST}/3" + path + "?" + urllib.parse.urlencode(params)
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                return {}
            time.sleep(2)
        except Exception:
            if attempt == 2:
                raise NetError("tarmoq")
            time.sleep(1)
    return {}


def clean_title(title):
    """'Better Call Saul | S01 ...' -> ('Better Call Saul', None)"""
    m = re.search(r"\b(19|20)\d{2}\b", title)
    year = m.group(0) if m else None
    t = re.split(r"[|(\[«]", title)[0]
    t = re.sub(r"[^\w\s'’ʻ:&.\-]", " ", t)
    t = re.sub(
        r"\b(uzbek|o['’ʻ]?zbek|tilida|serial|kino|film|multfilm|anime|hd|"
        r"1080p|720p|full|to['’ʻ]?liq|qismi|fasl|sezon|season)\b",
        " ", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" -.:")
    return t, year


def name_from_file(fn):
    """'Breaking.Bad.S01E01.1080p.mkv' -> 'Breaking Bad'"""
    if not fn:
        return ""
    b = os.path.splitext(fn)[0]
    b = re.split(r"[.\s_-]*(?:S\d{1,2}[.\s_-]?E\d{1,3}|\b(?:19|20)\d{2}\b|\d{3,4}p|\bE\d{2,3}\b)",
                 b, maxsplit=1, flags=re.I)[0]
    return re.sub(r"[._]+", " ", b).strip()


def name_from_caption(c):
    """'🔍 Josus (The Spy) — ...' -> ['The Spy', 'Josus']  (qavs ichidagi asl nom birinchi)"""
    line = (c or "").strip().split("\n")[0]
    head = re.split(r"\s[—–-]\s|\|", line)[0]
    raw = re.findall(r"\(([^)]+)\)", head) + [re.sub(r"\([^)]*\)", " ", head)]
    out = []
    for p in raw:
        p = re.sub(r"[^\w\s'’ʻ:&.\-]", " ", p)
        p = re.sub(r"\s+", " ", p).strip(" -.:")
        if 2 <= len(p) <= 40 and p not in out:
            out.append(p)
    return out


def find_tmdb(cands, cat):
    kinds = ["tv", "movie"] if cat in ("Serial", "Anime") else ["movie", "tv"]
    seen = set()
    for cand in cands:
        q, year = clean_title(cand)
        if not q or q.lower() in seen:
            continue
        seen.add(q.lower())
        for use_year in ((True, False) if year else (False,)):
            for kind in kinds:
                p = {"query": q, "language": "en-US"}
                if use_year:
                    p["year" if kind == "movie" else "first_air_date_year"] = year
                res = tmdb("/search/" + kind, **p).get("results") or []
                if res:
                    return kind, res[0]
    return None, None


def img(path, size):
    return f"https://image.tmdb.org/t/p/{size}{path}" if path else ""


def enrich(kind, item):
    d = tmdb(f"/{kind}/{item['id']}", language="en-US",
             append_to_response="images,translations",
             include_image_language="en,null")
    overview = ""
    trs = (d.get("translations") or {}).get("translations", [])
    for iso in ("uz", "ru"):
        for t in trs:
            if t.get("iso_639_1") == iso and (t.get("data") or {}).get("overview"):
                overview = t["data"]["overview"]
                break
        if overview:
            break
    overview = overview or d.get("overview") or ""
    logos = (d.get("images") or {}).get("logos") or []
    date = d.get("release_date") or d.get("first_air_date") or ""
    names = [g["name"] for g in d.get("genres", [])]
    return {
        "tmdb_id": item["id"],
        "year": date[:4],
        "rating": round(d.get("vote_average") or 0, 1),
        "genres": [GENRE_UZ.get(n.lower(), n) for n in names][:3],
        "lang": d.get("original_language") or "",
        "genres_en": [n.lower() for n in names],
        "overview": overview,
        "tagline": d.get("tagline") or "",
        "poster": img(d.get("poster_path"), "w500"),
        "backdrop": img(d.get("backdrop_path"), "w1280"),
        "logo": img(logos[0]["file_path"], "w500") if logos else "",
    }


# ---------------- Telegram ----------------

def is_video(m):
    if m.video:
        return True
    doc = m.document
    return bool(doc and (doc.mime_type or "").startswith("video/"))


async def find_chat(title):
    async for d in client.iter_dialogs():
        if (d.name or "").lower() == title.lower():
            return d.entity
    async for d in client.iter_dialogs():
        if title.lower() in (d.name or "").lower():
            return d.entity
    raise RuntimeError(f"Guruh topilmadi: {title}")


def video_info(m):
    doc = m.document
    name, dur = "", 0
    for a in doc.attributes:
        if hasattr(a, "file_name") and a.file_name:
            name = a.file_name
        if hasattr(a, "duration") and a.duration:
            dur = int(a.duration)
    label = (m.message or name or "").strip()
    s = e = 0
    for src in (m.message or "", name):
        mm = re.search(r"S(\d+)\D{0,6}E(\d+)", src, re.I)
        if mm:
            s, e = int(mm.group(1)), int(mm.group(2))
            break
    if not e:
        mm = re.search(r"(\d+)\s*-?\s*qism", m.message or "", re.I)
        if mm:
            e = int(mm.group(1))
    nm = re.search(r"[«\"“]([^»\"”]+)[»\"”]", label)
    return {
        "id": m.id, "size": doc.size, "mime": doc.mime_type or "video/mp4",
        "duration": dur, "season": s, "episode": e,
        "name": nm.group(1).strip() if nm else "",
        "file": name,
    }


def load_overrides():
    """nomlar.json: {"Josus": "The Spy", "Qahramonlar": "tv:1234"} - qo'lda to'g'rilash"""
    try:
        return json.load(open("nomlar.json", encoding="utf-8"))
    except Exception:
        return {}


def cache_ver():
    try:
        return "3-%d" % int(os.path.getmtime("nomlar.json"))
    except OSError:
        return "3"


def lookup(r):
    """Bir kino uchun TMDB ma'lumoti (alohida oqimda ishlaydi)."""
    ov = OVERRIDES.get(r["title"]) or []
    ov = [ov] if isinstance(ov, str) else list(ov)
    for o in ov:
        m = re.match(r"^(tv|movie):(\d+)$", o.strip())
        if m:
            return {"v": VER, "kind": m.group(1),
                    "info": enrich(m.group(1), {"id": int(m.group(2))})}
    cands = ov + [name_from_file(r["vids"][0]["file"])] \
        + name_from_caption(r["caption"]) + [r["title"]]
    kind, item = find_tmdb([c for c in cands if c], r["cat"])
    return {"v": VER, "kind": kind, "info": enrich(kind, item) if item else {}}


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def run_tmdb(todo, cache):
    """Internet uzilsa to'xtaydi; xato natijalar keshga YOZILMAYDI."""
    done = errs = 0
    ex = ThreadPoolExecutor(WORKERS)
    futs = {ex.submit(lookup, r): r for r in todo}
    try:
        for fu in as_completed(futs):
            r = futs[fu]
            try:
                cache[f"{r['tid']}|{r['title']}"] = fu.result()
            except NetError:
                errs += 1
                if errs >= 8:
                    print("Internet uzildi, TMDB bosqichi to'xtatildi.")
                    break
                continue
            except Exception as e:
                print("  xato:", r["title"], e)
            done += 1
            if done % 20 == 0:
                print(f"  TMDB: {done}/{len(todo)}")
                save_json(CACHE, cache)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
        save_json(CACHE, cache)
    return errs >= 8


async def scan_telegram(new, prog):
    raw = []
    for n, tid in enumerate(prog.get("order", []), 1):
        st = prog.get("topics", {}).get(str(tid))
        if tid in prog.get("skip", []) or not st or not st.get("new"):
            continue
        photo, caption, vids = None, "", []
        async for m in client.iter_messages(new, reply_to=st["new"], reverse=True):
            if m.photo and photo is None:
                photo, caption = m, m.message or ""
            elif is_video(m):
                vids.append(video_info(m))
        if not vids:
            continue
        vids.sort(key=lambda v: (v["season"], v["episode"], v["id"]))
        fixed = bool(prog.get("cat", {}).get(str(tid)))
        cat = prog.get("cat", {}).get(str(tid)) or ("Kino" if len(vids) <= 1 else "Serial")
        raw.append({"tid": tid, "title": st.get("title") or "?",
                    "photo_id": photo.id if photo else None, "caption": caption,
                    "vids": vids, "cat": cat, "fixed": fixed, "new": st["new"]})
        if len(raw) % 25 == 0:
            print(f"  Telegram: {len(raw)} ta papka o'qildi")
    return raw


async def build():
    if not TMDB_KEY:
        sys.exit("TMDB kalit yo'q. Termux'da:  echo SIZNING_KALIT > ~/tmdb.txt")
    if not pick_host():
        sys.exit("TMDB ochilmayapti. Telefonda VPN yoqing va qayta urinib ko'ring.")
    global OVERRIDES, VER
    OVERRIDES, VER = load_overrides(), cache_ver()
    t0 = time.time()
    fresh = "fresh" in sys.argv          # hamma keshni o'chirib, noldan
    rescan = fresh or "tg" in sys.argv   # faqat Telegramni qayta o'qish
    cache = {}
    if not fresh and os.path.exists(CACHE):
        cache = json.load(open(CACHE, encoding="utf-8"))

    await client.start()
    new = await find_chat(NEW_TITLE)
    prog = json.load(open(PROGRESS, encoding="utf-8"))
    os.makedirs(os.path.join(WEB, "posters"), exist_ok=True)

    # 1) Telegramdan papkalarni o'qish (natija raw_cache.json ga saqlanadi)
    if not rescan and os.path.exists(RAW):
        raw = json.load(open(RAW, encoding="utf-8"))
        print(f"Telegram: keshdan {len(raw)} ta (yangilash uchun: build tg)")
    else:
        raw = await scan_telegram(new, prog)
        save_json(RAW, raw)
        print(f"Telegram tugadi: {len(raw)} ta, {time.time() - t0:.0f}s")

    # 2) TMDB (parallel, keshdan foydalanadi)
    def stale(r):
        c = cache.get(f"{r['tid']}|{r['title']}")
        return c is None or (not c.get("info") and c.get("v") != VER)
    todo = [r for r in raw if stale(r)]
    print(f"TMDB: {len(todo)} ta yangi, {len(raw) - len(todo)} ta keshdan")
    if todo:
        broke = await asyncio.get_running_loop().run_in_executor(None, run_tmdb, todo, cache)
        if broke:
            sys.exit("Internetni tekshirib, qayta ishga tushiring (tayyor qismi saqlandi).")

    # 3) katalogni yig'ish
    catalog = []
    for r in raw:
        c = cache.get(f"{r['tid']}|{r['title']}") or {}
        info, kind = c.get("info") or {}, c.get("kind")
        cat, vids, caption = r["cat"], r["vids"], r["caption"]
        if not r["fixed"] and info:
            if "animation" in info.get("genres_en", []):
                cat = "Anime" if info.get("lang") == "ja" else "Multfilm"
            elif kind == "movie" or len(vids) <= 1:
                cat = "Kino"
        rating = info.get("rating", 0)
        genres = info.get("genres", [])
        if not rating:
            mr = re.search(r"IMDb[^\d]{0,6}(\d(?:\.\d)?)", caption, re.I)
            rating = float(mr.group(1)) if mr else 0
        if not genres:
            mg = re.search(r"Janr:\s*(.+)", caption)
            genres = [g.strip() for g in re.split(r"[,/]", mg.group(1))][:3] if mg else []
        desc = caption if len(caption) > 40 else (info.get("overview") or caption or r["title"])

        tg_poster = ""
        if r["photo_id"] and not info.get("poster"):
            path = os.path.join(WEB, "posters", f"{r['tid']}.jpg")
            if not os.path.exists(path):
                try:
                    pm = await client.get_messages(new, ids=r["photo_id"])
                    await client.download_media(pm, file=path)
                except Exception as e:
                    print("  poster yuklanmadi:", r["title"], type(e).__name__)
            if os.path.exists(path):
                tg_poster = f"posters/{r['tid']}.jpg"

        catalog.append({
            "id": r["tid"], "title": r["title"], "category": cat,
            "year": info.get("year", ""), "rating": rating, "genres": genres,
            "description": desc, "tagline": info.get("tagline", ""),
            "poster": info.get("poster") or tg_poster,
            "backdrop": info.get("backdrop", ""), "logo": info.get("logo", ""),
            "link": f"https://t.me/c/{new.id}/{r['new']}",
            "videos": vids,
        })
    with open(CATALOG, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1)
    hit = sum(1 for x in catalog if "tmdb" in x["poster"])
    miss = [x["title"] for x in catalog if "tmdb" not in x["poster"]]
    try:
        with open("topilmadi.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(miss))
        dl = os.path.expanduser("~/storage/downloads")
        if os.path.isdir(dl):
            with open(os.path.join(dl, "topilmadi.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(miss))
    except OSError:
        pass
    print(f"Tayyor: {len(catalog)} ta (TMDB'da topildi: {hit}) -> {CATALOG}, {time.time() - t0:.0f}s")
    await client.disconnect()


# ---------------- Server ----------------

_msgs = {}
NEW = None


def run(coro):
    return asyncio.run_coroutine_threadsafe(coro, client.loop).result()


async def get_msg(mid):
    if mid not in _msgs:
        _msgs[mid] = await client.get_messages(NEW, ids=mid)
    return _msgs[mid]


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
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        try:
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
                self._send(500, str(e).encode(), "text/plain; charset=utf-8")
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
        gen = client.iter_download(m.media, offset=aligned, request_size=CHUNK,
                                   file_size=size)

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
                run(gen.close())
            except Exception:
                pass


def serve():
    global NEW
    client.start()   # Telethon o'zi sinxron ishlaydi
    NEW = client.loop.run_until_complete(find_chat(NEW_TITLE))
    threading.Thread(target=client.loop.run_forever, daemon=True).start()
    os.makedirs(WEB, exist_ok=True)
    print(f"Server tayyor: http://localhost:{PORT}")
    print("Telegram uchun boshqa oynada:  cloudflared tunnel --url http://localhost:%d" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        client.loop.run_until_complete(build())
    else:
        serve()
