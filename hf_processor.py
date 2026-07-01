# ======================================================================
# HF SPACES PROCESSOR - Heavy Lifting: Search, Download, PoW, HLS
# ======================================================================
import os
import hashlib
import threading
import tempfile
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Config
BOT_TOKEN = os.environ.get("BOT_TOKEN")
HF_API_URL = os.environ.get("HF_API_URL")  # This space's URL
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

MAPPLE_WATCH_URL = "https://mapple.rip/watch/{media_type}/{item_id}"
MAPPLE_PLAYBACK_INIT = "https://mapple.rip/api/playback-init"
MAPPLE_STREAM_API = "https://mapple.rip/api/stream"
MAPPLE_API_KEY = "mptv_sk_a8f29c4e7b3d1f"
MAPPLE_REFERER = "https://mapple.rip/"
MAPPLE_ORIGIN = "https://mapple.rip/"

if not all([BOT_TOKEN, HF_API_URL, TMDB_API_KEY]):
    raise ValueError("BOT_TOKEN, HF_API_URL, TMDB_API_KEY required")

app = Flask(__name__)

# In-memory caches
search_cache = {}
download_sessions = {}

# Dedicated sessions with aggressive retries for external APIs
# Telegram session - Render will call back to send messages
tg_session = requests.Session()
tg_adapter = HTTPAdapter(max_retries=Retry(total=5, backoff_factor=2, status_forcelist=[429,500,502,503,504]), pool_connections=20, pool_maxsize=20)
tg_session.mount("https://", tg_adapter)

# TMDB session
tmdb_session = requests.Session()
tmdb_adapter = HTTPAdapter(max_retries=Retry(total=3, backoff_factor=1, status_forcelist=[429,500,502,503,504]), pool_connections=10, pool_maxsize=10)
tmdb_session.mount("https://", tmdb_adapter)

# Mapple session
mapple_session = requests.Session()
mapple_adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=3)
mapple_session.mount("https://", mapple_adapter)
mapple_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": MAPPLE_REFERER,
    "Origin": MAPPLE_ORIGIN
})

# ======================================================================
# TELEGRAM API (called by this service, or we can call Render back)
# ======================================================================
def tg_send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    try:
        resp = tg_session.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode, "reply_markup": reply_markup}, timeout=60)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"tg_send_message error: {e}")
        return None

def tg_edit_message(chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
    try:
        resp = tg_session.post(f"{TELEGRAM_API}/editMessageText", json={"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode, "reply_markup": reply_markup}, timeout=60)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"tg_edit_message error: {e}")
        return None

def tg_answer_callback(callback_query_id, text=None):
    try:
        tg_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": callback_query_id, "text": text} if text else {"callback_query_id": callback_query_id}, timeout=30)
    except:
        pass

def tg_send_video(chat_id, video_path, caption=None, reply_markup=None):
    try:
        with open(video_path, "rb") as f:
            files = {"video": f}
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
                data["parse_mode"] = "HTML"
            if reply_markup:
                import json
                data["reply_markup"] = json.dumps(reply_markup)
            resp = tg_session.post(f"{TELEGRAM_API}/sendVideo", data=data, files=files, timeout=180)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"tg_send_video error: {e}")
        return None

# ======================================================================
# KEYBOARDS
# ======================================================================
def build_results_keyboard(results):
    kb = {"inline_keyboard": []}
    for i, item in enumerate(results):
        if item["media_type"] == "movie":
            label = f"🎬 {item.get('title','Unknown')} ({item.get('release_date','N/A')[:4]}) ⭐ {item.get('vote_average',0)}/10"
            cb = f"detail:movie:{item['id']}:{i}"
        else:
            label = f"📺 {item.get('name','Unknown')} ({item.get('first_air_date','N/A')[:4]}) ⭐ {item.get('vote_average',0)}/10"
            cb = f"detail:tv:{item['id']}:{i}"
        kb["inline_keyboard"].append([{"text": label[:60], "callback_data": cb}])
    kb["inline_keyboard"].append([{"text": "🔍 Search again", "callback_data": "search_again"}])
    return kb

def build_detail_keyboard(item_id, media_type):
    return {"inline_keyboard": [[{"text": "📥 Download MP4", "callback_data": f"download:{media_type}:{item_id}"}], [{"text": "🔍 New Search", "callback_data": "search_again"}]]}

def build_progress_keyboard():
    return {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": "download_cancel"}]]}

# ======================================================================
# TMDB
# ======================================================================
def tmdb_search(query):
    try:
        resp = tmdb_session.get(f"{TMDB_BASE_URL}/search/multi", params={"api_key": TMDB_API_KEY, "query": query, "include_adult": "false", "language": "en-US"}, timeout=30)
        resp.raise_for_status()
        return [r for r in resp.json().get("results", []) if r.get("media_type") in ("movie", "tv")][:10]
    except:
        return []

def tmdb_details(media_type, item_id):
    try:
        resp = tmdb_session.get(f"{TMDB_BASE_URL}/{media_type}/{item_id}", params={"api_key": TMDB_API_KEY, "append_to_response": "credits,videos,images", "language": "en-US"}, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except:
        return None

def format_details(data, media_type):
    title = data.get("title") or data.get("name") or "Unknown"
    label = "🎬 Movie" if media_type == "movie" else "📺 TV Series"
    status = data.get("status", "Unknown")
    date = data.get("release_date" if media_type == "movie" else "first_air_date", "N/A")
    genres = ", ".join([g.get("name") for g in data.get("genres", [])]) or "N/A"
    rating = data.get("vote_average", 0)
    votes = data.get("vote_count", 0)
    overview = data.get("overview", "No overview.")
    cast = ", ".join([c.get("name") for c in data.get("credits", {}).get("cast", [])[:5]]) or "N/A"
    poster = data.get("poster_path")
    poster_url = f"{TMDB_IMAGE_BASE}{poster}" if poster else None
    
    msg = (f"<b>{title}</b> {label}\n"
           f"<b>Status:</b> {status}\n"
           f"<b>Date:</b> {date}\n"
           f"<b>Genres:</b> {genres}\n"
           f"<b>Rating:</b> {rating}/10 ({votes} votes)\n\n"
           f"<b>Cast:</b> {cast}\n\n"
           f"<b>Overview:</b>\n{overview}")
    return msg, poster_url

# ======================================================================
# MAPPLE.RIP HANDSHAKE & PoW
# ======================================================================
def solve_pow(challenge, difficulty):
    full = difficulty // 8
    rem = difficulty % 8
    mask = (0xff << (8 - rem)) & 0xff if rem else 0
    nonce = 0
    chal_bytes = challenge.encode()
    sha = hashlib.sha256
    logger.info(f"⚡ PoW: difficulty={difficulty}")
    while True:
        digest = sha(chal_bytes + str(nonce).encode()).digest()
        ok = all(digest[i] == 0 for i in range(full))
        if ok and rem and (digest[full] & mask):
            ok = False
        if ok:
            logger.info(f"🎯 PoW solved: nonce={nonce}")
            return str(nonce)
        nonce += 1
        if nonce > 50_000_000:
            raise Exception("PoW timeout")

def mapple_handshake(item_id, media_type):
    s = mapple_session
    # 1. Get requestToken
    watch = MAPPLE_WATCH_URL.format(media_type=media_type, item_id=item_id)
    page = s.get(watch, timeout=30)
    import re
    m = re.search(r'"requestToken"\s*:\s*"([^"]+)"', page.text)
    request_token = m.group(1) if m else "eyJub2...yy8E"
    
    # 2. Initial playback init
    init = {"mediaId": item_id, "mediaType": media_type, "requestToken": request_token}
    r1 = s.post(MAPPLE_PLAYBACK_INIT, json=init, timeout=30).json()
    
    # 3. PoW if needed
    if r1.get("success") and r1.get("requiresPow"):
        pow_meta = r1["pow"]
        nonce = solve_pow(pow_meta["challenge"], pow_meta["difficulty"])
        verify = {**init, "pow": {"challengeId": pow_meta["challengeId"], "nonce": nonce}}
        r2 = s.post(MAPPLE_PLAYBACK_INIT, json=verify, timeout=30).json()
    else:
        r2 = r1
    
    if not r2.get("success"):
        raise Exception("Handshake failed")
    
    token = r2["token"]
    
    # 4. Get stream URL
    params = {"mediaId": item_id, "mediaType": media_type, "tv_slug": "", "source": "mapple", "apikey": MAPPLE_API_KEY, "requestToken": request_token, "token": token}
    r3 = s.get(MAPPLE_STREAM_API, params=params, timeout=30).json()
    
    if r3.get("success") and "data" in r3:
        return r3["data"]["stream_url"]
    raise Exception("No stream URL")

# ======================================================================
# DOWNLOAD (HLS or MP4) with progress
# ======================================================================
def download_stream(chat_id, user_id, stream_url, title, progress_msg_id):
    s = mapple_session
    safe = "".join(c for c in title if c.isalnum() or c in " _-").strip()
    temp_path = os.path.join(tempfile.gettempdir(), f"{safe}_{user_id}.mp4")
    
    download_sessions[user_id] = {"progress_msg_id": progress_msg_id, "cancel": False, "temp_path": temp_path}
    
    try:
        head = s.get(stream_url, timeout=30)
        content = head.text.strip()
        
        if content.startswith("#EXTM3U"):
            # HLS
            variant_url = stream_url
            for i, line in enumerate(content.splitlines()):
                if line.startswith("#EXT-X-STREAM-INF"):
                    nxt = content.splitlines()[i+1].strip() if i+1 < len(content.splitlines()) else ""
                    if nxt and not nxt.startswith("#"):
                        from urllib.parse import urljoin
                        variant_url = urljoin(stream_url, nxt)
                        break
            
            var_resp = s.get(variant_url, timeout=30)
            segments = []
            for line in var_resp.text.splitlines():
                cl = line.strip()
                if cl and not cl.startswith("#"):
                    from urllib.parse import urljoin
                    segments.append(urljoin(variant_url, cl))
            
            total = len(segments)
            if total == 0:
                raise Exception("Empty segments")
            
            buffer = {}
            done = 0
            lock = threading.Lock()
            
            def dl_chunk(idx, url):
                if download_sessions.get(user_id, {}).get("cancel"):
                    return None
                try:
                    r = s.get(url, timeout=30)
                    if r.status_code == 200:
                        return idx, r.content
                except:
                    pass
                return None
            
            tg_edit_message(chat_id, progress_msg_id, f"📥 <b>Downloading:</b> {title}\n\n🔍 Initializing...\n0%", reply_markup=build_progress_keyboard())
            
            with ThreadPoolExecutor(max_workers=40) as ex:
                futures = {ex.submit(dl_chunk, i, u): i for i, u in enumerate(segments)}
                for fut in as_completed(futures):
                    if download_sessions.get(user_id, {}).get("cancel"):
                        ex.shutdown(wait=False)
                        return None
                    res = fut.result()
                    if res:
                        idx, data = res
                        buffer[idx] = data
                    with lock:
                        done += 1
                    pct = (done / total) * 100
                    if done % 5 == 0 or done == total:
                        tg_edit_message(chat_id, progress_msg_id, f"📥 <b>Downloading:</b> {title}\n\n📦 Streaming to RAM: {pct:.1f}% ({done}/{total})", reply_markup=build_progress_keyboard())
            
            if download_sessions.get(user_id, {}).get("cancel"):
                return None
            
            tg_edit_message(chat_id, progress_msg_id, f"📥 <b>Downloading:</b> {title}\n\n💾 Writing to disk... 100%", reply_markup=build_progress_keyboard())
            
            with open(temp_path, "wb") as f:
                for i in range(total):
                    if i in buffer:
                        f.write(buffer[i])
            buffer.clear()
        
        else:
            # Direct MP4
            with s.get(stream_url, stream=True, timeout=30) as resp:
                if resp.status_code != 200:
                    raise Exception("Download failed")
                total = int(resp.headers.get('content-length', 0))
                done = 0
                with open(temp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=524288):
                        if download_sessions.get(user_id, {}).get("cancel"):
                            return None
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        if total and done % (1024*1024) == 0:
                            pct = (done / total) * 100
                            tg_edit_message(chat_id, progress_msg_id, f"📥 <b>Downloading:</b> {title}\n\n📥 Downloading: {pct:.1f}%", reply_markup=build_progress_keyboard())
        
        return temp_path
    
    except Exception as e:
        logger.error(f"Download error: {e}\n{traceback.format_exc()}")
        raise
    finally:
        if user_id in download_sessions:
            del download_sessions[user_id]

# ======================================================================
# HANDLERS
# ======================================================================
def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "").strip()
    
    if text == "/start":
        tg_send_message(chat_id, "🎬 <b>Welcome!</b>\n\nSend a movie/TV name to search.\nFeatures: TMDB search, details, 📥 MP4 download with live progress.", parse_mode="HTML")
        return
    if text == "/help":
        tg_send_message(chat_id, "📖 Type any title to search. Tap result for details + download.", parse_mode="HTML")
        return
    
    if text:
        tg_send_message(chat_id, "🔍 Searching...")
        results = tmdb_search(text)
        if not results:
            tg_send_message(chat_id, "❌ No results.")
            return
        search_cache[user_id] = results
        tg_send_message(chat_id, "🔍 <b>Results:</b>", reply_markup=build_results_keyboard(results), parse_mode="HTML")

def handle_callback(cb):
    cb_id = cb["id"]
    user_id = cb["from"]["id"]
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    data = cb["data"]
    
    if data == "search_again":
        tg_answer_callback(cb_id, "Ready")
        tg_send_message(chat_id, "🔍 Send a movie/TV name to search.")
        return
    
    if data.startswith("detail:"):
        _, mtype, iid, idx = data.split(":")
        iid, idx = int(iid), int(idx)
        results = search_cache.get(user_id, [])
        if idx < len(results) and results[idx]["id"] == iid and results[idx]["media_type"] == mtype:
            tg_answer_callback(cb_id, "Loading...")
            details = tmdb_details(mtype, iid)
            if details:
                msg, poster = format_details(details, mtype)
                if poster:
                    tg_session.post(f"{TELEGRAM_API}/sendPhoto", json={"chat_id": chat_id, "photo": poster, "caption": msg, "parse_mode": "HTML", "reply_markup": build_detail_keyboard(iid, mtype)}, timeout=60)
                else:
                    tg_send_message(chat_id, msg, reply_markup=build_detail_keyboard(iid, mtype), parse_mode="HTML")
        return
    
    if data.startswith("download:"):
        _, mtype, iid = data.split(":")
        iid = int(iid)
        tg_answer_callback(cb_id, "Starting...")
        
        details = tmdb_details(mtype, iid)
        if not details:
            tg_edit_message(chat_id, msg_id, "❌ Failed to get details")
            return
        title = details.get("title") or details.get("name") or "Unknown"
        
        tg_edit_message(chat_id, msg_id, f"📥 <b>Downloading:</b> {title}\n\n🔗 Handshake...", reply_markup=build_progress_keyboard())
        
        def dl_task():
            try:
                stream_url = mapple_handshake(iid, mtype)
                tg_edit_message(chat_id, msg_id, f"📥 <b>Downloading:</b> {title}\n\n✅ Stream ready\n🔍 Initializing...", reply_markup=build_progress_keyboard())
                
                path = download_stream(chat_id, user_id, stream_url, title, msg_id)
                if not path:
                    return
                
                tg_edit_message(chat_id, msg_id, f"📥 <b>Downloading:</b> {title}\n\n✅ Complete\n📤 Sending...", reply_markup=None)
                
                tg_send_video(chat_id, path, caption=f"🎬 {title}")
                
                try: os.remove(path)
                except: pass
                
                tg_edit_message(chat_id, msg_id, f"✅ <b>Done!</b> {title}\n\nVideo sent! 🎬", reply_markup=build_detail_keyboard(iid, mtype))
            except Exception as e:
                logger.error(f"Download failed: {e}\n{traceback.format_exc()}")
                tg_edit_message(chat_id, msg_id, f"❌ <b>Failed:</b> {title}\n\n{str(e)[:200]}", reply_markup=build_detail_keyboard(iid, mtype))
        
        threading.Thread(target=dl_task, daemon=True).start()
        return
    
    if data == "download_cancel":
        if user_id in download_sessions:
            download_sessions[user_id]["cancel"] = True
        tg_answer_callback(cb_id, "Cancelled")

def handle_inline(inline):
    qid = inline["id"]
    query = inline["query"].strip()
    if not query:
        return
    results = tmdb_search(query)
    articles = []
    for item in results:
        name = item.get("title") or item.get("name") or "Unknown"
        air = (item.get("release_date") if item["media_type"]=="movie" else item.get("first_air_date"))[:4]
        rating = item.get("vote_average", 0)
        overview = item.get("overview", "No description")[:200]
        poster = item.get("poster_path")
        thumb = f"{TMDB_IMAGE_BASE}{poster}" if poster else None
        content = f"<b>{name}</b> ({air})\n⭐ {rating}/10\n\n{overview}"
        if item["media_type"] == "movie":
            articles.append({"type": "article", "id": f"m_{item['id']}", "title": f"🎬 {name} ({air})", "description": f"⭐ {rating}/10 - {overview[:100]}", "input_message_content": {"message_text": content, "parse_mode": "HTML"}, "thumb_url": thumb})
        else:
            articles.append({"type": "article", "id": f"t_{item['id']}", "title": f"📺 {name} ({air})", "description": f"⭐ {rating}/10 - {overview[:100]}", "input_message_content": {"message_text": content, "parse_mode": "HTML"}, "thumb_url": thumb})
    tg_session.post(f"{TELEGRAM_API}/answerInlineQuery", json={"inline_query_id": qid, "results": articles, "cache_time": 300}, timeout=60)

# ======================================================================
# FLASK ROUTES
# ======================================================================
@app.route("/process", methods=["POST"])
def process():
    """Receive update from Render bot, process in background"""
    update = request.get_json()
    threading.Thread(target=process_update, args=(update,), daemon=True).start()
    return jsonify({"status": "accepted"}), 200

def process_update(update):
    try:
        if "message" in update:
            handle_message(update["message"])
        elif "callback_query" in update:
            handle_callback(update["callback_query"])
        elif "inline_query" in update:
            handle_inline(update["inline_query"])
    except Exception as e:
        logger.error(f"Process error: {e}\n{traceback.format_exc()}")

@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)