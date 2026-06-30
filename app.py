import os
import telebot
from flask import Flask, request, abort
import logging

# Setup logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Load configuration from environment variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
RENDER_URL = os.environ.get("RENDER_URL")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required")
if not RENDER_URL:
    raise ValueError("RENDER_URL environment variable is required")

logger.info(f"BOT_TOKEN loaded: {BOT_TOKEN[:10]}...")
logger.info(f"RENDER_URL loaded: {RENDER_URL}")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# Use a webhook secret (first part of token) instead of full token with colon
WEBHOOK_SECRET = BOT_TOKEN.split(":")[0]
WEBHOOK_PATH = f"/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{RENDER_URL}{WEBHOOK_PATH}"

logger.info(f"WEBHOOK_PATH: {WEBHOOK_PATH}")
logger.info(f"WEBHOOK_URL: {WEBHOOK_URL}")

# Simple message handlers
@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    logger.info(f"Handling /start or /help from {message.chat.id}")
    bot.reply_to(message, "Hi! I'm a test bot. Try saying 'hi', 'ping', or 'hello'!")

@bot.message_handler(func=lambda m: m.text and m.text.lower() in ["hi", "hello", "hey", "ping"])
def greet(message):
    logger.info(f"Handling greeting from {message.chat.id}: {message.text}")
    responses = {
        "hi": "Hi there! 👋",
        "hello": "Hello! How can I help?",
        "hey": "Hey! What's up?",
        "ping": "Pong! 🏓",
    }
    bot.reply_to(message, responses.get(message.text.lower(), "Hi there!"))

@bot.message_handler(commands=["ping"])
def ping_command(message):
    logger.info(f"Handling /ping from {message.chat.id}")
    bot.reply_to(message, "Pong! 🏓")

@bot.message_handler(func=lambda m: True)
def echo_all(message):
    logger.info(f"Echoing from {message.chat.id}: {message.text}")
    bot.reply_to(message, f"You said: {message.text}")

# Flask webhook endpoint
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    logger.info(f"Webhook received request from {request.remote_addr}")
    logger.info(f"Headers: {dict(request.headers)}")
    
    if request.headers.get("content-type") == "application/json":
        json_str = request.get_data().decode("utf-8")
        logger.info(f"Raw update: {json_str}")
        update = telebot.types.Update.de_json(json_str)
        logger.info(f"Parsed update: {update}")
        
        if update.message:
            logger.info(f"Message: chat_id={update.message.chat.id}, text={update.message.text}")
        
        bot.process_new_updates([update])
        logger.info("Update processed")
        return "OK", 200
    else:
        logger.warning(f"Invalid content-type: {request.headers.get('content-type')}")
        abort(403)

@app.route("/")
def set_webhook():
    """Health check endpoint that also sets the webhook"""
    logger.info("Setting webhook...")
    bot.remove_webhook()
    result = bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"Webhook set result: {result}")
    return f"Webhook set to {WEBHOOK_URL}", 200

@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    # Set webhook on startup
    logger.info("Starting bot...")
    bot.remove_webhook()
    result = bot.set_webhook(url=WEBHOOK_URL)
    logger.info(f"Startup webhook set: {result}")
    # Run Flask app on port 10000 (Render default)
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=True)