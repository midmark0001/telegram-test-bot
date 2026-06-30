# ======================================================================
# IMPORTS
# ======================================================================
import os
import hashlib
import threading
import tempfile
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, abort
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ======================================================================
# CONFIGURATION
# ======================================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
SPACE_URL = os.environ.get("SPACE_URL")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Mapple.rip endpoints
MAPPLE_WATCH_URL = "https://mapple.rip/watch/{media_type}/{item_id}"
MAPPLE_PLAYBACK_INIT = "https://mapple.rip/api/playback-init"
MAPPLE_STREAM_API = "https://mapple.rip/api/stream"
MAPPLE_API_KEY = "mptv_sk_a8f29c4e7b3d1f"
MAPPLE_REFERER = "https://mapple.rip/"
MAPPLE_ORIGIN = "https://mapple.rip/"

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")
if not SPACE_URL:
    raise ValueError("SPACE_URL environment variable is required")
if not TMDB_API_KEY:
    raise ValueError("TMDB_API_KEY environment variable is required")

logger.info(f"Starting bot with token: {BOT_TOKEN[:10]}...")

app = Flask(__name__)

WEBHOOK_SECRET = BOT_TOKEN.split(":")[0]
WEBHOOK_PATH = f"/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{SPACE_URL}{WEBHOOK_PATH}"

logger.info(f"Webhook path: {WEBHOOK_PATH}")
logger.info(f"Webhook URL: {WEBHOOK_URL}")

# In-memory caches
search_cache = {}
download_sessions = {}

# Webhook setup flag (lazy init on first request)
_webhook_initialized = False
_webhook_lock = threading.Lock()

# ======================================================================
# TELEGRAM API HELPERS WITH RETRY
# ======================================================================
# Create a session with retry strategy for Telegram API
_telegram_session = requests.Session()
_retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["HEAD", "GET", "POST", "PUT", "DELETE", "OPTIONS", "TRACE"],
    raise_on_status=False,
)
_adapter = HTTPAdapter(max_retries=_retry_strategy, pool_connections=20, pool_maxsize=20)
_telegram_session.mount("https://", _adapter)
_telegram_session.mount("http://", _adapter)

def _telegram_request(method: str, endpoint: str, **kwargs):
    """Make Telegram API request with retry and 90s timeout"""
    timeout = kwargs.pop('timeout', 90)
    url = f"{TELEGRAM_API}/{endpoint}"
    try:
        return _telegram_session.request(method, url, timeout=timeout, **kwargs)
    except Exception as e:
        logger.error(f"Telegram API {method} {endpoint} failed: {e}")
        raise


def send_message(chat_id: int, text: str, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = _telegram_request("POST", "sendMessage", json=payload)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"send_message error: {e}")
        return None


def edit_message(chat_id: int, message_id: int, text: str, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = _telegram_request("POST", "editMessageText", json=payload)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"edit_message error: {e}")
        return None


def delete_message(chat_id: int, message_id: int):
    try:
        _telegram_request("POST", "deleteMessage", json={"chat_id": chat_id, "message_id": message_id})
    except:
        pass


def answer_callback_query(callback_query_id: str, text: str = None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        _telegram_request("POST", "answerCallbackQuery", json=payload)
    except:
        pass


def send_video(chat_id: int, video_path: str, caption: str = None, reply_markup=None):
    url = f"{TELEGRAM_API}/sendVideo"
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
            resp = _telegram_session.post(url, data=data, files=files, timeout=180)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"send_video error: {e}")
        return None


def send_document(chat_id: int, file_path: str, caption: str = None):
    url = f"{TELEGRAM_API}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            files = {"document": f}
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
                data["parse_mode"] = "HTML"
            resp = _telegram_session.post(url, data=data, files=files, timeout=180)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"send_document error: {e}")
        return None


def send_photo(chat_id: int, photo_url: str, caption: str = None, reply_markup=None):
    payload = {"chat_id": chat_id, "photo": photo_url}
    if caption:
        payload["caption"] = caption
        payload["parse_mode"] = "HTML"
    if reply_markup:
        import json
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        _telegram_request("POST", "sendPhoto", json=payload)
    except:
        pass


# ======================================================================
# KEYBOARD BUILDERS
# ======================================================================
def build_results_keyboard(results: list):
    keyboard = {"inline_keyboard": []}
    for i, item in enumerate(results):
        if item["media_type"] == "movie":
            label = f"🎬 {item.get('title', 'Unknown')} ({item.get('release_date', 'N/A')[:4]}) ⭐ {item.get('vote_average', 0)}/10"
            callback_data = f"detail:movie:{item['id']}:{i}"
        else:
            label = f"📺 {item.get('name', 'Unknown')} ({item.get('first_air_date', 'N/A')[:4]}) ⭐ {item.get('vote_average', 0)}/10"
            callback_data = f"detail:tv:{item['id']}:{i}"
        keyboard["inline_keyboard"].append([{"text": label[:60], "callback_data": callback_data}])
    keyboard["inline_keyboard"].append([{"text": "🔍 Search again", "callback_data": "search_again"}])
    return keyboard


def build_detail_keyboard(item_id: int, media_type: str):
    return {
        "inline_keyboard": [
            [{"text": "📥 Download MP4", "callback_data": f"download:{media_type}:{item_id}"}],
            [{"text": "🔍 New Search", "callback_data": "search_again"}]
        ]
    }


def build_download_progress_keyboard():
    return {"inline_keyboard": [[{"text": "❌ Cancel Download", "callback_data": "download_cancel"}]]}


# ======================================================================
# TMDB FUNCTIONS
# ======================================================================
def tmdb_search(query: str, page: int = 1):
    url = f"{TMDB_BASE_URL}/search/multi"
    params = {"api_key": TMDB_API_KEY, "query": query, "page": page, "include_adult": "false", "language": "en-US"}
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return [r for r in data.get("results", []) if r.get("media_type") in ("movie", "tv")][:10]
    except:
        return []


def tmdb_get_details(media_type: str, item_id: int):
    url = f"{TMDB_BASE_URL}/{media_type}/{item_id}"
    params = {"api_key": TMDB_API_KEY, "append_to_response": "credits,videos,images", "language": "en-US"}
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except:
        return None


def format_details(data: dict, media_type: str) -> tuple[str, str | None]:
    title = data.get("title") or data.get("name") or "Unknown"
    media_label = "🎬 Movie" if media_type == "movie" else "📺 TV Series"
    status = data.get("status", "Unknown")
    date_str = data.get("release_date" if media_type == "movie" else "first_air_date", "N/A")
    genres = [g.get("name") for g in data.get("genres", [])]
    genres_str = ", ".join(genres) if genres else "N/A"
    rating = data.get("vote_average", 0)
    votes = data.get("vote_count", 0)
    overview = data.get("overview", "No overview available.")
    cast = [c.get("name") for c in data.get("credits", {}).get("cast", [])[:5]]
    cast_str = ", ".join(cast) if cast else "N/A"
    poster_path = data.get("poster_path")
    poster_url = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None

    msg = (
        f"<b>{title}</b> {media_label}\n"
        f"<b>Status:</b> {status}\n"
        f"<b>Release/Air Date:</b> {date_str}\n"
        f"<b>Genres:</b> {genres_str}\n"
        f"<b>Rating:</b> {rating}/10 ({votes} votes)\n\n"
        f"<b>Cast:</b> {cast_str}\n\n"
        f"<b>Overview:</b>\n{overview}"
    )
    return msg, poster_url


# ======================================================================
# MAPPLE.RIP HANDSHAKE & DOWNLOAD
# ======================================================================
def get_mapple_session():
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=3)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": MAPPLE_REFERER,
        "Origin": MAPPLE_ORIGIN
    })
    return session


def solve_proof_of_work(challenge: str, difficulty: int) -> str:
    full_bytes = difficulty // 8
    remaining_bits = difficulty % 8
    mask = (0xff << (8 - remaining_bits)) & 0xff if remaining_bits > 0 else 0

    nonce = 0
    challenge_bytes = challenge.encode('utf-8')
    sha256 = hashlib.sha256

    logger.info(f"⚡ PoW Solver Started. Difficulty: {difficulty} bits...")

    while True:
        text = challenge_bytes + str(nonce).encode('utf-8')
        digest = sha256(text).digest()

        is_match = True
        for i in range(full_bytes):
            if digest[i] != 0:
                is_match = False
                break

        if is_match and remaining_bits > 0:
            if (digest[full_bytes] & mask) != 0:
                is_match = False

        if is_match:
            logger.info(f"🎯 PoW Puzzle Solved! Nonce found: {nonce}")
            return str(nonce)

        nonce += 1
        if nonce > 50000000:
            raise Exception("PoW execution safety threshold exceeded.")


def execute_handshake(session: requests.Session, item_id: int, media_type: str):
    watch_url = MAPPLE_WATCH_URL.format(media_type=media_type, item_id=item_id)
    page_res = session.get(watch_url, timeout=30)

    import re
    token_match = re.search(r'"requestToken"\s*:\s*"([^"]+)"', page_res.text)
    request_token = token_match.group(1) if token_match else "eyJub2...yy8E"

    init_payload = {"mediaId": item_id, "mediaType": media_type, "requestToken": request_token}
    res1 = session.post(MAPPLE_PLAYBACK_INIT, json=init_payload, timeout=30)
    data1 = res1.json()

    if data1.get("success") and data1.get("requiresPow"):
        pow_meta = data1["pow"]
        resolved_nonce = solve_proof_of_work(pow_meta["challenge"], pow_meta["difficulty"])
        verification_payload = {
            **init_payload,
            "pow": {"challengeId": pow_meta["challengeId"], "nonce": resolved_nonce}
        }
        res2 = session.post(MAPPLE_PLAYBACK_INIT, json=verification_payload, timeout=30)
        data2 = res2.json()
    else:
        data2 = data1

    if not data2.get("success"):
        raise Exception("Handshake token verification signature rejected.")

    final_playback_token = data2["token"]

    stream_params = {
        "mediaId": item_id,
        "mediaType": media_type,
        "tv_slug": "",
        "source": "mapple",
        "apikey": MAPPLE_API_KEY,
        "requestToken": request_token,
        "token": final_playback_token
    }
    res3 = session.get(MAPPLE_STREAM_API, params=stream_params, timeout=30)
    data3 = res3.json()

    if data3.get("success") and "data" in data3:
        return data3["data"]["stream_url"]
    raise Exception("Resolver parameters accepted but server returned an empty track object.")


def download_stream(chat_id: int, user_id: int, stream_url: str, title: str, progress_msg_id: int):
    session = get_mapple_session()
    safe_title = "".join([c for c in title if c.isalnum() or c in (" ", "_", "-")]).strip()

    temp_dir = tempfile.gettempdir()
    temp_path = os.path.join(temp_dir, f"{safe_title}_{user_id}.mp4")

    download_sessions[user_id] = {"progress_msg_id": progress_msg_id, "cancel": False, "temp_path": temp_path}

    try:
        head_check = session.get(stream_url, timeout=30)
        manifest_content = head_check.text.strip()

        if manifest_content.startswith("#EXTM3U"):
            variant_playlist_url = stream_url
            lines = manifest_content.splitlines()
            for i, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF"):
                    next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
                    if next_line and not next_line.startswith("#"):
                        from urllib.parse import urljoin
                        variant_playlist_url = urljoin(stream_url, next_line)
                        break

            variant_res = session.get(variant_playlist_url, timeout=30)
            segment_urls = []
            for line in variant_res.text.splitlines():
                cleaned = line.strip()
                if cleaned and not cleaned.startswith("#"):
                    from urllib.parse import urljoin
                    segment_urls.append(urljoin(variant_playlist_url, cleaned))

            total_segments = len(segment_urls)
            if total_segments == 0:
                raise Exception("Empty segment list")

            memory_buffer = {}
            completed_count = 0
            counter_lock = threading.Lock()

            def download_chunk(chunk_index, chunk_url):
                if download_sessions.get(user_id, {}).get("cancel"):
                    return None
                try:
                    r = session.get(chunk_url, timeout=30)
                    if r.status_code == 200:
                        return chunk_index, r.content
                except:
                    pass
                return None

            edit_message(chat_id, progress_msg_id, f"📥 <b>Downloading:</b> {title}\n\n🔍 Initializing...\n0%", reply_markup=build_download_progress_keyboard())

            with ThreadPoolExecutor(max_workers=40) as executor:
                futures = {executor.submit(download_chunk, idx, url): idx for idx, url in enumerate(segment_urls)}

                for future in as_completed(futures):
                    if download_sessions.get(user_id, {}).get("cancel"):
                        executor.shutdown(wait=False)
                        return None

                    result = future.result()
                    if result:
                        chunk_idx, binary_data = result
                        memory_buffer[chunk_idx] = binary_data

                    with counter_lock:
                        completed_count += 1

                    percent = (completed_count / total_segments) * 100
                    if completed_count % 5 == 0 or completed_count == total_segments:
                        edit_message(chat_id, progress_msg_id,
                            f"📥 <b>Downloading:</b> {title}\n\n📦 Streaming to RAM: {percent:.1f}% ({completed_count}/{total_segments})",
                            reply_markup=build_download_progress_keyboard())

            if download_sessions.get(user_id, {}).get("cancel"):
                return None

            edit_message(chat_id, progress_msg_id,
                f"📥 <b>Downloading:</b> {title}\n\n💾 Writing to disk... 100%",
                reply_markup=build_download_progress_keyboard())

            with open(temp_path, "wb") as f:
                for idx in range(total_segments):
                    if idx in memory_buffer:
                        f.write(memory_buffer[idx])

            memory_buffer.clear()

        else:
            with session.get(stream_url, stream=True, timeout=30) as response:
                if response.status_code != 200:
                    raise Exception("Server connection dropped")
                total_bytes = int(response.headers.get('content-length', 0))
                bytes_downloaded = 0

                with open(temp_path, "wb") as f:
                    for chunk in response.iter_content(chunk_size=524288):
                        if download_sessions.get(user_id, {}).get("cancel"):
                            return None
                        if not chunk:
                            continue
                        f.write(chunk)
                        bytes_downloaded += len(chunk)
                        if total_bytes > 0 and bytes_downloaded % (1024 * 1024) == 0:
                            percent = (bytes_downloaded / total_bytes) * 100
                            edit_message(chat_id, progress_msg_id,
                                f"📥 <b>Downloading:</b> {title}\n\n📥 Downloading: {percent:.1f}%",
                                reply_markup=build_download_progress_keyboard())

        return temp_path

    except Exception as e:
        logger.error(f"Download error: {e}\n{traceback.format_exc()}")
        raise
    finally:
        if user_id in download_sessions:
            del download_sessions[user_id]


def handle_download_callback(callback_query: dict):
    callback_query_id = callback_query["id"]
    user_id = callback_query["from"]["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]
    data = callback_query["data"]

    if data == "download_cancel":
        if user_id in download_sessions:
            download_sessions[user_id]["cancel"] = True
        answer_callback_query(callback_query_id, "Download cancelled")
        return

    if data.startswith("download:"):
        _, media_type, item_id_str = data.split(":")
        item_id = int(item_id_str)

        answer_callback_query(callback_query_id, "Starting download...")

        # Get details for title
        details = tmdb_get_details(media_type, item_id)
        if not details:
            edit_message(chat_id, message_id, "❌ Failed to get media details")
            return
        title = details.get("title") or details.get("name") or "Unknown"

        progress_text = f"📥 <b>Downloading:</b> {title}\n\n🔍 Starting handshake..."
        edit_message(chat_id, message_id, progress_text, reply_markup=build_download_progress_keyboard())

        def download_task():
            try:
                session = get_mapple_session()
                stream_url = execute_handshake(session, item_id, media_type)

                edit_message(chat_id, message_id,
                    f"📥 <b>Downloading:</b> {title}\n\n✅ Stream URL obtained\n🔍 Initializing download...",
                    reply_markup=build_download_progress_keyboard())

                temp_path = download_stream(chat_id, user_id, stream_url, title, message_id)
                if not temp_path:
                    return

                edit_message(chat_id, message_id,
                    f"📥 <b>Downloading:</b> {title}\n\n✅ Download complete\n📤 Sending video...",
                    reply_markup=None)

                send_video(chat_id, temp_path, caption=f"🎬 {title}")

                # Cleanup
                try:
                    os.remove(temp_path)
                    logger.info(f"Cleaned up temp file: {temp_path}")
                except:
                    pass

                edit_message(chat_id, message_id,
                    f"✅ <b>Done!</b> {title}\n\nVideo sent successfully! 🎬",
                    reply_markup=build_detail_keyboard(item_id, media_type))

            except Exception as e:
                logger.error(f"Download failed: {e}\n{traceback.format_exc()}")
                edit_message(chat_id, message_id,
                    f"❌ <b>Download failed:</b> {title}\n\n{str(e)[:200]}",
                    reply_markup=build_detail_keyboard(item_id, media_type))

        threading.Thread(target=download_task, daemon=True).start()


# ======================================================================
# MESSAGE HANDLERS
# ======================================================================
def handle_message(message: dict):
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = message.get("text", "").strip()

    if text == "/start":
        send_message(chat_id,
            "🎬 <b>Welcome to Movie Bot!</b>\n\n"
            "Send me a movie or TV show name to search.\n\n"
            "Features:\n"
            "• Search TMDB (movies & TV)\n"
            "• View details: poster, cast, genres, rating\n"
            "• 📥 Download MP4 via mapple.rip\n"
            "• Live progress updates\n"
            "• Inline mode: @Hemaitel_bot <query>",
            parse_mode="HTML")
        return

    if text == "/help":
        send_message(chat_id,
            "📖 <b>Help</b>\n\n"
            "• Type any movie/TV name to search\n"
            "• Tap result for details + download\n"
            "• Tap 📥 Download MP4 for video\n"
            "• Inline: @Hemaitel_bot <query>",
            parse_mode="HTML")
        return

    # Search
    if text:
        send_message(chat_id, "🔍 Searching...")
        results = tmdb_search(text)
        if not results:
            send_message(chat_id, "❌ No results found. Try a different query.")
            return

        search_cache[user_id] = results
        send_message(chat_id, "🔍 <b>Search Results:</b>", reply_markup=build_results_keyboard(results), parse_mode="HTML")


def handle_callback_query(callback_query: dict):
    callback_query_id = callback_query["id"]
    user_id = callback_query["from"]["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]
    data = callback_query["data"]

    if data == "search_again":
        answer_callback_query(callback_query_id, "Ready for new search")
        send_message(chat_id, "🔍 Send me a movie or TV show name to search.")
        return

    if data.startswith("detail:"):
        _, media_type, item_id_str, idx_str = data.split(":")
        item_id = int(item_id_str)
        idx = int(idx_str)

        results = search_cache.get(user_id, [])
        if idx < len(results):
            item = results[idx]
            if item["id"] == item_id and item["media_type"] == media_type:
                answer_callback_query(callback_query_id, "Loading details...")
                details = tmdb_get_details(media_type, item_id)
                if details:
                    msg, poster_url = format_details(details, media_type)
                    if poster_url:
                        send_photo(chat_id, poster_url, caption=msg, reply_markup=build_detail_keyboard(item_id, media_type))
                    else:
                        send_message(chat_id, msg, reply_markup=build_detail_keyboard(item_id, media_type), parse_mode="HTML")
                else:
                    send_message(chat_id, "❌ Failed to load details")
        return

    if data.startswith("download:"):
        handle_download_callback(callback_query)
        return

    if data == "download_cancel":
        handle_download_callback(callback_query)
        return


def handle_inline_query(inline_query: dict):
    query_id = inline_query["id"]
    query_text = inline_query["query"].strip()
    if not query_text:
        return

    results = tmdb_search(query_text)
    inline_results = []
    for i, item in enumerate(results):
        name = item.get("title") or item.get("name") or "Unknown"
        air = item.get("release_date" if item["media_type"] == "movie" else "first_air_date", "N/A")[:4]
        rating = item.get("vote_average", 0)
        overview = item.get("overview", "No description")[:200]
        poster = item.get("poster_path")
        thumb = f"{TMDB_IMAGE_BASE}{poster}" if poster else None
        if item["media_type"] == "movie":
            content = f"<b>{name}</b> ({air})\n⭐ {rating}/10\n\n{overview}"
            r = {"type": "article", "id": f"movie_{item['id']}", "title": f"🎬 {name} ({air})",
                 "description": f"⭐ {rating}/10 - {overview[:100]}",
                 "input_message_content": {"message_text": content, "parse_mode": "HTML"}, "thumb_url": thumb}
        else:
            content = f"<b>{name}</b> ({air})\n⭐ {rating}/10\n\n{overview}"
            r = {"type": "article", "id": f"tv_{item['id']}", "title": f"📺 {name} ({air})",
                 "description": f"⭐ {rating}/10 - {overview[:100]}",
                 "input_message_content": {"message_text": content, "parse_mode": "HTML"}, "thumb_url": thumb}
        inline_results.append(r)

    _telegram_request("POST", "answerInlineQuery",
        json={"inline_query_id": query_id, "results": inline_results, "cache_time": 300})


def setup_webhook():
    """Lazy webhook setup - called on first request (non-blocking)"""
    global _webhook_initialized
    with _webhook_lock:
        if _webhook_initialized:
            return
        _webhook_initialized = True  # Mark immediately to prevent duplicate calls
    
    def _do_setup():
        try:
            _telegram_request("POST", "deleteWebhook", timeout=60)
            resp = _telegram_request("POST", "setWebhook", json={"url": WEBHOOK_URL}, timeout=60)
            logger.info(f"Webhook set: {resp.status_code} - {resp.text}")
        except Exception as e:
            logger.error(f"Webhook setup error: {e}")
    
    # Run in background thread - don't block the request
    threading.Thread(target=_do_setup, daemon=True).start()


# ======================================================================
# FLASK WEBHOOK
# ======================================================================
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    # Lazy webhook setup on first request
    setup_webhook()

    if request.headers.get("content-type") == "application/json":
        update = request.get_json()
        # Process in background thread so we return "OK" immediately
        threading.Thread(target=process_update, args=(update,), daemon=True).start()
        return "OK", 200
    abort(403)


def process_update(update: dict):
    """Process update in background thread"""
    try:
        if "message" in update:
            handle_message(update["message"])
        elif "callback_query" in update:
            handle_callback_query(update["callback_query"])
        elif "inline_query" in update:
            handle_inline_query(update["inline_query"])
    except Exception as e:
        logger.error(f"Error: {e}\n{traceback.format_exc()}")


@app.route("/")
def root():
    return f"Webhook active at {WEBHOOK_URL}", 200


@app.route("/health")
def health():
    return "OK", 200


@app.route("/test")
def test():
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)