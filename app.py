import os
import requests
import telebot
from flask import Flask, request, abort
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.environ.get("BOT_TOKEN")
RENDER_URL = os.environ.get("RENDER_URL")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w342"

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")
if not RENDER_URL:
    raise ValueError("RENDER_URL environment variable is required")
if not TMDB_API_KEY:
    raise ValueError("TMDB_API_KEY environment variable is required")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

WEBHOOK_SECRET = BOT_TOKEN.split(":")[0]
WEBHOOK_PATH = f"/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{RENDER_URL}{WEBHOOK_PATH}"

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
        # Filter for movies and TV only
        results = [r for r in data.get("results", []) if r.get("media_type") in ("movie", "tv")]
        return results[:10]  # Limit to 10 results
    except Exception as e:
        logger.error(f"TMDB search error: {e}")
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
        logger.error(f"TMDB details error: {e}")
        return None


def format_movie_result(movie: dict) -> str:
    """Format movie for inline result"""
    title = movie.get("title", "Unknown")
    release = movie.get("release_date", "N/A")[:4] if movie.get("release_date") else "N/A"
    rating = movie.get("vote_average", 0)
    overview = movie.get("overview", "No description")[:100]
    return f"🎬 {title} ({release}) ⭐ {rating}/10\n{overview}..."


def format_tv_result(tv: dict) -> str:
    """Format TV show for inline result"""
    name = tv.get("name", "Unknown")
    air_date = tv.get("first_air_date", "N/A")[:4] if tv.get("first_air_date") else "N/A"
    rating = tv.get("vote_average", 0)
    overview = tv.get("overview", "No description")[:100]
    return f"📺 {name} ({air_date}) ⭐ {rating}/10\n{overview}..."


def format_details(data: dict, media_type: str) -> tuple[str, str | None]:
    """Format detailed info for message + return poster URL"""
    title = data.get("title") or data.get("name") or "Unknown"
    media_label = "🎬 Movie" if media_type == "movie" else "📺 TV Series"
    status = data.get("status", "Unknown")
    
    # Dates
    if media_type == "movie":
        date_str = data.get("release_date", "N/A")
    else:
        date_str = data.get("first_air_date", "N/A")
    
    # Genres
    genres = [g.get("name") for g in data.get("genres", [])]
    genres_str = ", ".join(genres) if genres else "N/A"
    
    # Rating
    rating = data.get("vote_average", 0)
    votes = data.get("vote_count", 0)
    
    # Overview
    overview = data.get("overview", "No overview available.")
    
    # Cast (top 5)
    cast = [c.get("name") for c in data.get("credits", {}).get("cast", [])[:5]]
    cast_str = ", ".join(cast) if cast else "N/A"
    
    # Poster
    poster_path = data.get("poster_path")
    poster_url = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None
    
    # Build message
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


@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    text = (
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
    bot.send_message(message.chat.id, text, parse_mode="HTML")


@bot.message_handler(func=lambda m: True)
def handle_search(message):
    """Handle text messages as search queries"""
    query = message.text.strip()
    if not query:
        return
    
    if query.startswith("/"):
        return
    
    # Send "searching" message
    searching_msg = bot.send_message(message.chat.id, "🔍 Searching...")
    
    results = tmdb_search(query)
    
    if not results:
        bot.edit_message_text("No results found. Try a different query.", message.chat.id, searching_msg.message_id)
        return
    
    # Cache results for this user
    search_cache[message.from_user.id] = results
    
    # Build inline keyboard
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    for i, item in enumerate(results):
        if item["media_type"] == "movie":
            label = format_movie_result(item)
            callback_data = f"detail:movie:{item['id']}:{i}"
        else:
            label = format_tv_result(item)
            callback_data = f"detail:tv:{item['id']}:{i}"
        
        # Truncate label for button
        btn_text = label.split("\n")[0][:60]
        markup.add(telebot.types.InlineKeyboardButton(btn_text, callback_data=callback_data))
    
    markup.add(telebot.types.InlineKeyboardButton("🔍 Search again", callback_data="search_again"))
    
    bot.edit_message_text(
        f"🔍 <b>Results for:</b> {query}\n\nTap a result for details:",
        message.chat.id,
        searching_msg.message_id,
        parse_mode="HTML",
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    """Handle inline button clicks"""
    data = call.data
    
    if data == "search_again":
        bot.answer_callback_query(call.id, "Type a new search query!")
        return
    
    if data.startswith("detail:"):
        # Parse: detail:media_type:id:index
        parts = data.split(":")
        if len(parts) != 4:
            bot.answer_callback_query(call.id, "Invalid data")
            return
        
        _, media_type, item_id_str, index_str = parts
        item_id = int(item_id_str)
        index = int(index_str)
        
        bot.answer_callback_query(call.id, "Loading details...")
        
        # Get cached result to verify
        cached = search_cache.get(call.from_user.id, [])
        if index >= len(cached):
            bot.edit_message_text("Result expired. Please search again.", call.message.chat.id, call.message.message_id)
            return
        
        item = cached[index]
        if item["id"] != item_id or item["media_type"] != media_type:
            bot.edit_message_text("Result mismatch. Please search again.", call.message.chat.id, call.message.message_id)
            return
        
        # Fetch detailed info
        details = tmdb_get_details(media_type, item_id)
        if not details:
            bot.edit_message_text("Failed to fetch details. Try again.", call.message.chat.id, call.message.message_id)
            return
        
        # Format and send details
        msg_text, poster_url = format_details(details, media_type)
        
        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("🔍 New Search", callback_data="search_again"))
        
        if poster_url:
            try:
                bot.send_photo(call.message.chat.id, poster_url, caption=msg_text, parse_mode="HTML", reply_markup=markup)
            except:
                bot.send_message(call.message.chat.id, msg_text, parse_mode="HTML", reply_markup=markup)
        else:
            bot.send_message(call.message.chat.id, msg_text, parse_mode="HTML", reply_markup=markup)
        
        # Delete the results message
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except:
            pass


@bot.inline_handler(func=lambda query: len(query.query) > 0)
def inline_search(query):
    """Handle inline queries (@bot query)"""
    results = tmdb_search(query.query)
    
    inline_results = []
    for i, item in enumerate(results):
        if item["media_type"] == "movie":
            title = item.get("title", "Unknown")
            release = item.get("release_date", "N/A")[:4] if item.get("release_date") else "N/A"
            rating = item.get("vote_average", 0)
            overview = item.get("overview", "No description")[:200]
            poster_path = item.get("poster_path")
            thumb_url = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None
            
            desc = f"🎬 {title} ({release}) ⭐ {rating}/10"
            content = f"<b>{title}</b> ({release})\n⭐ {rating}/10\n\n{overview}"
            
            r = telebot.types.InlineQueryResultArticle(
                id=f"movie_{item['id']}",
                title=f"🎬 {title} ({release})",
                description=f"⭐ {rating}/10 - {overview[:100]}",
                input_message_content=telebot.types.InputTextMessageContent(
                    message_text=content,
                    parse_mode="HTML"
                ),
                thumb_url=thumb_url
            )
        else:
            name = item.get("name", "Unknown")
            air = item.get("first_air_date", "N/A")[:4] if item.get("first_air_date") else "N/A"
            rating = item.get("vote_average", 0)
            overview = item.get("overview", "No description")[:200]
            poster_path = item.get("poster_path")
            thumb_url = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None
            
            desc = f"📺 {name} ({air}) ⭐ {rating}/10"
            content = f"<b>{name}</b> ({air})\n⭐ {rating}/10\n\n{overview}"
            
            r = telebot.types.InlineQueryResultArticle(
                id=f"tv_{item['id']}",
                title=f"📺 {name} ({air})",
                description=f"⭐ {rating}/10 - {overview[:100]}",
                input_message_content=telebot.types.InputTextMessageContent(
                    message_text=content,
                    parse_mode="HTML"
                ),
                thumb_url=thumb_url
            )
        inline_results.append(r)
    
    bot.answer_inline_query(query.id, inline_results, cache_time=300)


# Flask webhook
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    if request.headers.get("content-type") == "application/json":
        json_str = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return "OK", 200
    abort(403)


@app.route("/")
def set_webhook():
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    return f"Webhook set to {WEBHOOK_URL}", 200


@app.route("/health")
def health():
    return "OK", 200


if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)