# Patch requests.Session with 60s timeout BEFORE any other imports
# This is required for HF Spaces to connect to api.telegram.org:443
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Store original request method
_original_request = requests.Session.request

def _patched_request(self, method, url, **kwargs):
    # Force minimum 60 second timeout (HF Spaces needs longer for api.telegram.org)
    timeout = kwargs.get('timeout', 60)
    if isinstance(timeout, (int, float)) and timeout < 60:
        kwargs['timeout'] = 60
    elif isinstance(timeout, tuple) and len(timeout) == 2:
        # (connect_timeout, read_timeout) - bump read_timeout if too low
        connect_t, read_t = timeout
        if read_t < 60:
            kwargs['timeout'] = (connect_t, 60)
    elif 'timeout' not in kwargs:
        kwargs['timeout'] = 60
    return _original_request(self, method, url, **kwargs)

requests.Session.request = _patched_request

# Now import everything else
import os
import hashlib
import threading
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, abort
import logging
import traceback

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.environ.get("BOT_TOKEN")
SPACE_URL = os.environ.get("SPACE_URL")  # e.g., https://username-space.hf.space
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
search_cache = {}  # user_id -> list of search results
download_sessions = {}  # user_id -> {progress_msg_id, cancel_flag, temp_path}


# ======================================================================
# PoW SOLVER (from download_noplayer.py)
# ======================================================================
def solve_proof_of_work(challenge: str, difficulty: int) -> str:
    """Finds a nonce such that SHA-256(challenge + nonce) has N leading zero bits."""
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


# ======================================================================
# TELEGRAM API HELPERS
# ======================================================================
def send_message(chat_id: int, text: str, reply_markup=None, parse_mode="HTML"):
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"send_message error: {e}")
        return None


def edit_message(chat_id: int, message_id: int, text: str, reply_markup=None, parse_mode="HTML"):
    url = f"{TELEGRAM_API}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"edit_message error: {e}")
        return None


def delete_message(chat_id: int, message_id: int):
    url = f"{TELEGRAM_API}/deleteMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "message_id": message_id})
    except:
        pass


def answer_callback_query(callback_query_id: str, text: str = None):
    url = f"{TELEGRAM_API}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        requests.post(url, json=payload)
    except:
        pass


def send_video(chat_id: int, video_path: str, caption: str = None, reply_markup=None):
    """Send video file to user"""
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
            resp = requests.post(url, data=data, files=files, timeout=180)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"send_video error: {e}")
        return None


def send_document(chat_id: int, file_path: str, caption: str = None):
    """Send as document if video fails"""
    url = f"{TELEGRAM_API}/sendDocument"
    try:
        with open(file_path, "rb") as f:
            files = {"document": f}
            data = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
                data["parse_mode"] = "HTML"
            resp = requests.post(url, data=data, files=files, timeout=180)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"send_document error: {e}")
        return None


def send_photo(chat_id: int, photo_url: str, caption: str = None, reply_markup=None):
    url = f"{TELEGRAM_API}/sendPhoto"
    payload = {"chat_id": chat_id, "photo": photo_url}
    if caption:
        payload["caption"] = caption
        payload["parse_mode"] = "HTML"
    if reply_markup:
        import json
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload)
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
    """Detail view with Download button above New Search"""
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
        resp = requests.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        return [r for r in data.get("results", []) if r.get("media_type") in ("movie", "tv")][:10]
    except:
        return []


def tmdb_get_details(media_type: str, item_id: int):
    url = f"{TMDB_BASE_URL}/{media_type}/{item_id}"
    params = {"api_key": TMDB_API_KEY, "append_to_response": "credits,videos,images", "language": "en-US"}
    try:
        resp = requests.get(url, params=params)
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
# MAPPLE.RIP HANDSHAKE & DOWNLOAD (from download_noplayer.py)
# ======================================================================
def get_mapple_session():
    """Create session with proper headers"""
    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=3)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": MAPPLE_REFERER,
        "Origin": MAPPLE_ORIGIN
    })
    return session


def execute_handshake(session: requests.Session, item_id: int, media_type: str):
    """Execute the mapple.rip handshake to get stream URL"""
    # Step 1: Get requestToken from watch page
    watch_url = MAPPLE_WATCH_URL.format(media_type=media_type, item_id=item_id)
    page_res = session.get(watch_url)

    import re
    token_match = re.search(r'"requestToken"\s*:\s*"([^"]+)"', page_res.text)
    request_token = token_match.group(1) if token_match else "eyJub2...yy8E"

    # Step 2: Initial playback init
    init_payload = {"mediaId": item_id, "mediaType": media_type, "requestToken": request_token}
    res1 = session.post(MAPPLE_PLAYBACK_INIT, json=init_payload)
    data1 = res1.json()

    # Step 3: PoW if required
    if data1.get("success") and data1.get("requiresPow"):
        pow_meta = data1["pow"]
        resolved_nonce = solve_proof_of_work(pow_meta["challenge"], pow_meta["difficulty"])
        verification_payload = {
            **init_payload,
            "pow": {"challengeId": pow_meta["challengeId"], "nonce": resolved_nonce}
        }
        res2 = session.post(MAPPLE_PLAYBACK_INIT, json=verification_payload)
        data2 = res2.json()
    else:
        data2 = data1

    if not data2.get("success"):
        raise Exception("Handshake token verification signature rejected.")

    final_playback_token = data2["token"]

    # Step 4: Get stream URL
    stream_params = {
        "mediaId": item_id,
        "mediaType": media_type,
        "tv_slug": "",
        "source": "mapple",
        "apikey": MAPPLE_API_KEY,
        "requestToken": request_token,
        "token": final_playback_token
    }
    res3 = session.get(MAPPLE_STREAM_API, params=stream_params)
    data3 = res3.json()

    if data3.get("success") and "data" in data3:
        return data3["data"]["stream_url"]
    raise Exception("Resolver parameters accepted but server returned an empty track object.")


def download_stream(chat_id: int, user_id: int, stream_url: str, title: str, progress_msg_id: int):
    """Download HLS or MP4 with live progress updates"""
    session = get_mapple_session()
    safe_title = "".join([c for c in title if c.isalnum() or c in (" ", "_", "-")]).strip()

    # Create temp file
    temp_dir = tempfile.gettempdir()
    temp_path = os.path.join(temp_dir, f"{safe_title}_{user_id}.mp4")

    download_sessions[user_id] = {"progress_msg_id": progress_msg_id, "cancel": False, "temp_path": temp_path}

    try:
        # Check if HLS
        head_check = session.get(stream_url)
        manifest_content = head_check.text.strip()

        if manifest_content.startswith("#EXTM3U"):
            # HLS - find variant playlist
            variant_playlist_url = stream_url
            lines = manifest_content.splitlines()
            for i, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF"):
                    next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
                    if next_line and not next_line.startswith("#"):
                        from urllib.parse import urljoin
                        variant_playlist_url = urljoin(stream_url, next_line)
                        break

            variant_res = session.get(variant_playlist_url)
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
                    r = session.get(chunk_url)
                    if r.status_code == 200:
                        return chunk_index, r.content
                except:
                    pass
                return None

            # Update initial progress
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

            # Write to disk
            edit_message(chat_id, progress_msg_id,
                f"📥 <b>Downloading:</b> {title}\n\n💾 Writing to disk... 100%",
                reply_markup=build_download_progress_keyboard())

            with open(temp_path, "wb") as f:
                for idx in range(total_segments):
                    if idx in memory_buffer:
                        f.write(memory_buffer[idx])

            memory_buffer.clear()

        else:
            # Direct MP4
            with session.get(stream_url, stream=True) as response:
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
    """Handle download button click"""
    callback_query_id = callback_query["id"]
    user_id = callback_query["from"]["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]
    data = callback_query["data"]

    if data == "download_cancel":
        if user_id in download_sessions:
            download_sessions[user_id]["cancel"] = True
        answer_callback_query(callback_query_id, "Cancelling download...")
        return

    # Parse: download:media_type:item_id
    parts = data.split(":")
    if len(parts) != 3:
        answer_callback_query(callback_query_id, "Invalid data")
        return

    _, media_type, item_id_str = parts
    item_id = int(item_id_str)

    answer_callback_query(callback_query_id, "Starting download...")

    # Send initial progress message
    progress_msg = send_message(chat_id, "🔄 Initializing download...", reply_markup=build_download_progress_keyboard())
    if not progress_msg:
        return
    progress_msg_id = progress_msg.get("result", {}).get("message_id")

    # Run download in background
    def run_download():
        try:
            # Execute handshake
            session = get_mapple_session()
            edit_message(chat_id, progress_msg_id, "🔐 Handshaking with server...", reply_markup=build_download_progress_keyboard())

            stream_url = execute_handshake(session, item_id, media_type)

            edit_message(chat_id, progress_msg_id, "📥 Starting download...", reply_markup=build_download_progress_keyboard())

            # Get title from cached result
            cached = search_cache.get(user_id, [])
            title = "Video"
            for item in cached:
                if item.get("id") == item_id and item.get("media_type") == media_type:
                    title = item.get("title") or item.get("name") or "Video"
                    break

            # Download
            temp_path = download_stream(chat_id, user_id, stream_url, title, progress_msg_id)

            if temp_path and os.path.exists(temp_path):
                file_size = os.path.getsize(temp_path)

                # Send video
                edit_message(chat_id, progress_msg_id, f"📤 Sending video ({file_size / 1024 / 1024:.1f} MB)...")

                keyboard = {"inline_keyboard": [[{"text": "🔍 New Search", "callback_data": "search_again"}]]}

                result = send_video(chat_id, temp_path, caption=f"🎬 {title}", reply_markup=keyboard)

                if not result:
                    # Try as document
                    send_document(chat_id, temp_path, caption=f"🎬 {title}")

                # Cleanup
                try:
                    os.remove(temp_path)
                except:
                    pass

                edit_message(chat_id, progress_msg_id, f"✅ <b>Done!</b> {title} sent successfully!", reply_markup=keyboard)
            else:
                edit_message(chat_id, progress_msg_id, "❌ Download failed - file not created", reply_markup={"inline_keyboard": [[{"text": "🔍 New Search", "callback_data": "search_again"}]]})

        except Exception as e:
            logger.error(f"Download pipeline error: {e}\n{traceback.format_exc()}")
            edit_message(chat_id, progress_msg_id, f"❌ <b>Download failed:</b> {str(e)[:200]}", reply_markup={"inline_keyboard": [[{"text": "🔍 New Search", "callback_data": "search_again"}]]})

    threading.Thread(target=run_download, daemon=True).start()


# ======================================================================
# MESSAGE HANDLERS
# ======================================================================
def handle_message(message: dict):
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = message.get("text", "").strip()

    if not text:
        return

    if text.startswith("/"):
        if text in ["/start", "/help"]:
            welcome = (
                "🎬 <b>TMDB Search Bot</b>\n\n"
                "Search movies & TV shows!\n\n"
                "<b>How to use:</b>\n"
                "• Type any name to search\n"
                "• Tap result for details\n"
                "• Tap 📥 Download MP4 to download\n"
                "• Use inline: @Hemaitel_bot <query>\n\n"
                "<b>Commands:</b>\n/start, /help"
            )
            send_message(chat_id, welcome)
        return

    # Search
    searching = send_message(chat_id, "🔍 Searching...")
    if not searching:
        return
    searching_msg_id = searching.get("result", {}).get("message_id")

    results = tmdb_search(text)
    if not results:
        edit_message(chat_id, searching_msg_id, "No results found.")
        return

    search_cache[user_id] = results
    keyboard = build_results_keyboard(results)
    edit_message(chat_id, searching_msg_id, f"🔍 <b>Results for:</b> {text}\n\nTap for details:", reply_markup=keyboard)


def handle_callback_query(callback_query: dict):
    callback_query_id = callback_query["id"]
    user_id = callback_query["from"]["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]
    data = callback_query["data"]

    if data == "search_again":
        answer_callback_query(callback_query_id, "Type a new search!")
        return

    if data.startswith("detail:"):
        # detail:media_type:item_id:index
        parts = data.split(":")
        if len(parts) != 4:
            return
        _, media_type, item_id_str, index_str = parts
        item_id = int(item_id_str)
        index = int(index_str)

        answer_callback_query(callback_query_id, "Loading details...")

        cached = search_cache.get(user_id, [])
        if index >= len(cached):
            edit_message(chat_id, message_id, "Expired. Search again.")
            return

        item = cached[index]
        if item["id"] != item_id or item["media_type"] != media_type:
            edit_message(chat_id, message_id, "Mismatch. Search again.")
            return

        details = tmdb_get_details(media_type, item_id)
        if not details:
            edit_message(chat_id, message_id, "Failed to fetch details.")
            return

        msg_text, poster_url = format_details(details, media_type)
        keyboard = build_detail_keyboard(item_id, media_type)

        delete_message(chat_id, message_id)

        if poster_url:
            send_photo(chat_id, poster_url, caption=msg_text, reply_markup=keyboard)
        else:
            send_message(chat_id, msg_text, reply_markup=keyboard)

    elif data.startswith("download:"):
        handle_download_callback(callback_query)


def handle_inline_query(inline_query: dict):
    query_id = inline_query["id"]
    query_text = inline_query["query"]
    if not query_text:
        return

    results = tmdb_search(query_text)
    inline_results = []
    for item in results:
        if item["media_type"] == "movie":
            title = item.get("title", "Unknown")
            release = item.get("release_date", "N/A")[:4] if item.get("release_date") else "N/A"
            rating = item.get("vote_average", 0)
            overview = item.get("overview", "No description")[:200]
            poster = item.get("poster_path")
            thumb = f"{TMDB_IMAGE_BASE}{poster}" if poster else None
            content = f"<b>{title}</b> ({release})\n⭐ {rating}/10\n\n{overview}"
            r = {"type": "article", "id": f"movie_{item['id']}", "title": f"🎬 {title} ({release})",
                 "description": f"⭐ {rating}/10 - {overview[:100]}",
                 "input_message_content": {"message_text": content, "parse_mode": "HTML"}, "thumb_url": thumb}
        else:
            name = item.get("name", "Unknown")
            air = item.get("first_air_date", "N/A")[:4] if item.get("first_air_date") else "N/A"
            rating = item.get("vote_average", 0)
            overview = item.get("overview", "No description")[:200]
            poster = item.get("poster_path")
            thumb = f"{TMDB_IMAGE_BASE}{poster}" if poster else None
            content = f"<b>{name}</b> ({air})\n⭐ {rating}/10\n\n{overview}"
            r = {"type": "article", "id": f"tv_{item['id']}", "title": f"📺 {name} ({air})",
                 "description": f"⭐ {rating}/10 - {overview[:100]}",
                 "input_message_content": {"message_text": content, "parse_mode": "HTML"}, "thumb_url": thumb}
        inline_results.append(r)

    requests.post(f"{TELEGRAM_API}/answerInlineQuery",
        json={"inline_query_id": query_id, "results": inline_results, "cache_time": 300})


# ======================================================================
# FLASK WEBHOOK
# ======================================================================
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        update = request.get_json()
        try:
            if "message" in update:
                handle_message(update["message"])
            elif "callback_query" in update:
                handle_callback_query(update["callback_query"])
            elif "inline_query" in update:
                handle_inline_query(update["inline_query"])
        except Exception as e:
            logger.error(f"Error: {e}\n{traceback.format_exc()}")
        return "OK", 200
    abort(403)


@app.route("/")
def root():
    return f"Webhook active at {WEBHOOK_URL}", 200


@app.route("/health")
def health():
    return "OK", 200


# Set webhook at module load
try:
    requests.post(f"{TELEGRAM_API}/deleteWebhook")
    resp = requests.post(f"{TELEGRAM_API}/setWebhook", json={"url": WEBHOOK_URL})
    logger.info(f"Webhook set: {resp.status_code} - {resp.text}")
except Exception as e:
    logger.error(f"Webhook setup error: {e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)