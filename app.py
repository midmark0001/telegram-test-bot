import os
import requests
from flask import Flask, request, abort
import logging
import traceback

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.environ.get("BOT_TOKEN")
RENDER_URL = os.environ.get("RENDER_URL")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")
if not RENDER_URL:
    raise ValueError("RENDER_URL environment variable is required")
if not TMDB_API_KEY:
    raise ValueError("TMDB_API_KEY environment variable is required")

logger.info(f"Starting bot with token: {BOT_TOKEN[:10]}...")

app = Flask(__name__)

WEBHOOK_SECRET = BOT_TOKEN.split(":")[0]
WEBHOOK_PATH = f"/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{RENDER_URL}{WEBHOOK_PATH}"

logger.info(f"Webhook path: {WEBHOOK_PATH}")
logger.info(f"Webhook URL: {WEBHOOK_URL}")

# In-memory cache for search results (user_id -> list of results)
search_cache = {}


def tmdb_search(query: str, page: int = 1):
    """Search TMDB for movies and TV shows"""
    url = f"{TMDB_BASE_URL}/search/multi"
    params = {
        "api_key": TMDB_API_KEY,
        "query": query,
        "page": page,
        "include_adult": "false",
        "language": "en-US"
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        results = [r for r in data.get("results", []) if r.get("media_type") in ("movie", "tv")]
        return results[:10]
    except Exception as e:
        logger.error(f"TMDB search error: {e}\n{traceback.format_exc()}")
        return []


def tmdb_get_details(media_type: str, item_id: int):
    """Get detailed info for a movie or TV show"""
    url = f"{TMDB_BASE_URL}/{media_type}/{item_id}"
    params = {
        "api_key": TMDB_API_KEY,
        "append_to_response": "credits,videos,images",
        "language": "en-US"
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"TMDB details error: {e}\n{traceback.format_exc()}")
        return None


def format_movie_result(movie: dict) -> str:
    title = movie.get("title", "Unknown")
    release = movie.get("release_date", "N/A")[:4] if movie.get("release_date") else "N/A"
    rating = movie.get("vote_average", 0)
    overview = movie.get("overview", "No description")[:100]
    return f"🎬 {title} ({release}) ⭐ {rating}/10\n{overview}..."


def format_tv_result(tv: dict) -> str:
    name = tv.get("name", "Unknown")
    air_date = tv.get("first_air_date", "N/A")[:4] if tv.get("first_air_date") else "N/A"
    rating = tv.get("vote_average", 0)
    overview = tv.get("overview", "No description")[:100]
    return f"📺 {name} ({air_date}) ⭐ {rating}/10\n{overview}..."


def format_details(data: dict, media_type: str) -> tuple[str, str | None]:
    title = data.get("title") or data.get("name") or "Unknown"
    media_label = "🎬 Movie" if media_type == "movie" else "📺 TV Series"
    status = data.get("status", "Unknown")
    
    if media_type == "movie":
        date_str = data.get("release_date", "N/A")
    else:
        date_str = data.get("first_air_date", "N/A")
    
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


def send_message(chat_id: int, text: str, reply_markup=None, parse_mode="HTML"):
    """Send message via direct Telegram API"""
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"send_message error: {e}\n{traceback.format_exc()}")
        return None


def edit_message(chat_id: int, message_id: int, text: str, reply_markup=None, parse_mode="HTML"):
    """Edit message via direct Telegram API"""
    url = f"{TELEGRAM_API}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"edit_message error: {e}\n{traceback.format_exc()}")
        return None


def delete_message(chat_id: int, message_id: int):
    """Delete message via direct Telegram API"""
    url = f"{TELEGRAM_API}/deleteMessage"
    payload = {"chat_id": chat_id, "message_id": message_id}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"delete_message error: {e}\n{traceback.format_exc()}")
        return None


def answer_callback_query(callback_query_id: str, text: str = None):
    """Answer callback query via direct Telegram API"""
    url = f"{TELEGRAM_API}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"answer_callback_query error: {e}\n{traceback.format_exc()}")
        return None


def send_photo(chat_id: int, photo_url: str, caption: str = None, reply_markup=None, parse_mode="HTML"):
    """Send photo via direct Telegram API"""
    url = f"{TELEGRAM_API}/sendPhoto"
    payload = {"chat_id": chat_id, "photo": photo_url}
    if caption:
        payload["caption"] = caption
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"send_photo error: {e}\n{traceback.format_exc()}")
        return None


# Build inline keyboard markup
def build_results_keyboard(results: list, user_id: int):
    """Build inline keyboard for search results"""
    keyboard = {"inline_keyboard": []}
    for i, item in enumerate(results):
        if item["media_type"] == "movie":
            label = format_movie_result(item)
            callback_data = f"detail:movie:{item['id']}:{i}"
        else:
            label = format_tv_result(item)
            callback_data = f"detail:tv:{item['id']}:{i}"
        
        btn_text = label.split("\n")[0][:60]
        keyboard["inline_keyboard"].append([{"text": btn_text, "callback_data": callback_data}])
    
    keyboard["inline_keyboard"].append([{"text": "🔍 Search again", "callback_data": "search_again"}])
    return keyboard


def build_detail_keyboard():
    """Build inline keyboard for detail view"""
    return {"inline_keyboard": [[{"text": "🔍 New Search", "callback_data": "search_again"}]]}


# Flask webhook
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    logger.info(f"Webhook request from {request.remote_addr}")
    if request.headers.get("content-type") == "application/json":
        update = request.get_json()
        logger.info(f"Received update: {update.get('update_id')}")
        
        try:
            # Handle message
            if "message" in update:
                handle_message(update["message"])
            # Handle callback query
            elif "callback_query" in update:
                handle_callback_query(update["callback_query"])
            # Handle inline query
            elif "inline_query" in update:
                handle_inline_query(update["inline_query"])
            
            logger.info("Update processed")
        except Exception as e:
            logger.error(f"Error processing update: {e}\n{traceback.format_exc()}")
        return "OK", 200
    abort(403)


def handle_message(message: dict):
    """Handle incoming text message"""
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    text = message.get("text", "").strip()
    message_id = message.get("message_id")
    
    logger.info(f"Message from {chat_id} (user {user_id}): {text}")
    
    if not text:
        return
    
    if text.startswith("/"):
        if text in ["/start", "/help"]:
            welcome_text = (
                "🎬 <b>TMDB Search Bot</b>\n\n"
                "Search for movies and TV shows!\n\n"
                "<b>How to use:</b>\n"
                "• Type any movie/TV name to search\n"
                "• Tap a result to see details\n"
                "• Use inline mode: @Hemaitel_bot <query>\n\n"
                "<b>Commands:</b>\n"
                "/start - Show this message\n"
                "/help - Show this message"
            )
            send_message(chat_id, welcome_text)
        return
    
    # Search query
    searching = send_message(chat_id, "🔍 Searching...")
    if not searching:
        return
    
    searching_msg_id = searching.get("result", {}).get("message_id")
    results = tmdb_search(text)
    logger.info(f"Found {len(results)} results for '{text}'")
    
    if not results:
        edit_message(chat_id, searching_msg_id, "No results found. Try a different query.")
        return
    
    # Cache results
    search_cache[user_id] = results
    
    # Build keyboard
    keyboard = build_results_keyboard(results, user_id)
    
    edit_message(
        chat_id,
        searching_msg_id,
        f"🔍 <b>Results for:</b> {text}\n\nTap a result for details:",
        reply_markup=keyboard
    )


def handle_callback_query(callback_query: dict):
    """Handle inline button callback"""
    callback_query_id = callback_query["id"]
    user_id = callback_query["from"]["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]
    data = callback_query["data"]
    
    logger.info(f"Callback from {user_id}: {data}")
    
    if data == "search_again":
        answer_callback_query(callback_query_id, "Type a new search query!")
        return
    
    if data.startswith("detail:"):
        parts = data.split(":")
        if len(parts) != 4:
            answer_callback_query(callback_query_id, "Invalid data")
            return
        
        _, media_type, item_id_str, index_str = parts
        item_id = int(item_id_str)
        index = int(index_str)
        
        answer_callback_query(callback_query_id, "Loading details...")
        
        cached = search_cache.get(user_id, [])
        if index >= len(cached):
            edit_message(chat_id, message_id, "Result expired. Please search again.")
            return
        
        item = cached[index]
        if item["id"] != item_id or item["media_type"] != media_type:
            edit_message(chat_id, message_id, "Result mismatch. Please search again.")
            return
        
        details = tmdb_get_details(media_type, item_id)
        if not details:
            edit_message(chat_id, message_id, "Failed to fetch details. Try again.")
            return
        
        msg_text, poster_url = format_details(details, media_type)
        keyboard = build_detail_keyboard()
        
        # Delete the results message
        delete_message(chat_id, message_id)
        
        # Send details with photo if available
        if poster_url:
            send_photo(chat_id, poster_url, caption=msg_text, reply_markup=keyboard)
        else:
            send_message(chat_id, msg_text, reply_markup=keyboard)


def handle_inline_query(inline_query: dict):
    """Handle inline query (@bot query)"""
    query_id = inline_query["id"]
    query_text = inline_query["query"]
    user_id = inline_query["from"]["id"]
    
    logger.info(f"Inline query from {user_id}: {query_text}")
    
    if not query_text:
        return
    
    results = tmdb_search(query_text)
    
    inline_results = []
    for i, item in enumerate(results):
        if item["media_type"] == "movie":
            title = item.get("title", "Unknown")
            release = item.get("release_date", "N/A")[:4] if item.get("release_date") else "N/A"
            rating = item.get("vote_average", 0)
            overview = item.get("overview", "No description")[:200]
            poster_path = item.get("poster_path")
            thumb_url = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None
            
            content = f"<b>{title}</b> ({release})\n⭐ {rating}/10\n\n{overview}"
            
            r = {
                "type": "article",
                "id": f"movie_{item['id']}",
                "title": f"🎬 {title} ({release})",
                "description": f"⭐ {rating}/10 - {overview[:100]}",
                "input_message_content": {
                    "message_text": content,
                    "parse_mode": "HTML"
                },
                "thumb_url": thumb_url
            }
        else:
            name = item.get("name", "Unknown")
            air = item.get("first_air_date", "N/A")[:4] if item.get("first_air_date") else "N/A"
            rating = item.get("vote_average", 0)
            overview = item.get("overview", "No description")[:200]
            poster_path = item.get("poster_path")
            thumb_url = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None
            
            content = f"<b>{name}</b> ({air})\n⭐ {rating}/10\n\n{overview}"
            
            r = {
                "type": "article",
                "id": f"tv_{item['id']}",
                "title": f"📺 {name} ({air})",
                "description": f"⭐ {rating}/10 - {overview[:100]}",
                "input_message_content": {
                    "message_text": content,
                    "parse_mode": "HTML"
                },
                "thumb_url": thumb_url
            }
        inline_results.append(r)
    
    # Answer inline query
    url = f"{TELEGRAM_API}/answerInlineQuery"
    payload = {"inline_query_id": query_id, "results": inline_results, "cache_time": 300}
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"answer_inline_query error: {e}\n{traceback.format_exc()}")


@app.route("/")
def root():
    logger.info("Root endpoint accessed")
    return f"Webhook is set to {WEBHOOK_URL}", 200


@app.route("/health")
def health():
    return "OK", 200


# Set webhook at module level (runs when gunicorn imports the app)
try:
    requests.post(f"{TELEGRAM_API}/deleteWebhook", timeout=10)
    resp = requests.post(
        f"{TELEGRAM_API}/setWebhook",
        json={"url": WEBHOOK_URL},
        timeout=10
    )
    logger.info(f"Webhook set at module load: {resp.status_code} - {resp.text}")
except Exception as e:
    logger.error(f"Error setting webhook at module load: {e}\n{traceback.format_exc()}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)