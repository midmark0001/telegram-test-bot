import os
import requests
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

app = Flask(__name__)

# Use a webhook secret (first part of token) instead of full token with colon
WEBHOOK_SECRET = BOT_TOKEN.split(":")[0]
WEBHOOK_PATH = f"/{WEBHOOK_SECRET}"
WEBHOOK_URL = f"{RENDER_URL}{WEBHOOK_PATH}"

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

logger.info(f"WEBHOOK_PATH: {WEBHOOK_PATH}")
logger.info(f"WEBHOOK_URL: {WEBHOOK_URL}")

def send_message(chat_id, text, reply_to_message_id=None):
    """Send message directly via Telegram API"""
    url = f"{TELEGRAM_API}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    try:
        resp = requests.post(url, json=payload, timeout=10)
        logger.info(f"sendMessage response: {resp.status_code} - {resp.text}")
        return resp.json()
    except Exception as e:
        logger.error(f"sendMessage error: {e}")
        return None

def handle_message(message):
    """Process incoming message and send response"""
    chat_id = message["chat"]["id"]
    text = message.get("text", "")
    message_id = message.get("message_id")
    
    logger.info(f"Handling message from chat_id={chat_id}: {text}")
    
    # Determine response
    if text.startswith("/"):
        command = text.lower()
        if command in ["/start", "/help"]:
            response = "Hi! I'm a test bot. Try saying 'hi', 'ping', or 'hello'!"
        elif command == "/ping":
            response = "Pong! 🏓"
        else:
            response = f"Unknown command: {text}"
    else:
        text_lower = text.lower()
        if text_lower in ["hi", "hello", "hey"]:
            response = "Hi there! 👋"
        elif text_lower == "ping":
            response = "Pong! 🏓"
        else:
            response = f"You said: {text}"
    
    # Send response
    send_message(chat_id, response, reply_to_message_id=message_id)

# Flask webhook endpoint
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    logger.info(f"Webhook received request from {request.remote_addr}")
    
    if request.headers.get("content-type") != "application/json":
        logger.warning(f"Invalid content-type: {request.headers.get('content-type')}")
        abort(403)
    
    try:
        update = request.get_json()
        logger.info(f"Received update: {update}")
        
        if "message" in update:
            handle_message(update["message"])
        elif "edited_message" in update:
            handle_message(update["edited_message"])
        else:
            logger.info("Update has no message field")
        
        return "OK", 200
    except Exception as e:
        logger.exception(f"Error processing webhook: {e}")
        return "Error", 500

@app.route("/")
def set_webhook():
    """Health check endpoint that also sets the webhook"""
    logger.info("Setting webhook...")
    try:
        # Remove existing webhook
        requests.post(f"{TELEGRAM_API}/deleteWebhook", timeout=10)
        # Set new webhook
        resp = requests.post(
            f"{TELEGRAM_API}/setWebhook",
            json={"url": WEBHOOK_URL},
            timeout=10
        )
        logger.info(f"Webhook set result: {resp.status_code} - {resp.text}")
        return f"Webhook set to {WEBHOOK_URL}", 200
    except Exception as e:
        logger.exception(f"Error setting webhook: {e}")
        return f"Error: {e}", 500

@app.route("/health")
def health():
    return "OK", 200

if __name__ == "__main__":
    # Set webhook on startup
    logger.info("Starting bot...")
    try:
        requests.post(f"{TELEGRAM_API}/deleteWebhook", timeout=10)
        resp = requests.post(
            f"{TELEGRAM_API}/setWebhook",
            json={"url": WEBHOOK_URL},
            timeout=10
        )
        logger.info(f"Startup webhook set: {resp.status_code} - {resp.text}")
    except Exception as e:
        logger.exception(f"Error on startup: {e}")
    
    # Run Flask app on port 10000 (Render default)
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=True)