# ======================================================================
# RENDER BOT - Lightweight Webhook Receiver & Telegram Sender
# ======================================================================
import os
import logging
import requests
from flask import Flask, request, abort

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
HF_API_URL = os.environ.get("HF_API_URL")  # e.g., https://mandyg8-telegram-movie-bot.hf.space
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN required")
if not HF_API_URL:
    raise ValueError("HF_API_URL required")

app = Flask(__name__)
WEBHOOK_SECRET = BOT_TOKEN.split(":")[0]
WEBHOOK_PATH = f"/{WEBHOOK_SECRET}"

# Create session with retries for Telegram API (Render has good connectivity)
_tg_session = requests.Session()
adapter = requests.adapters.HTTPAdapter(max_retries=3, pool_connections=20, pool_maxsize=20)
_tg_session.mount("https://", adapter)
_tg_session.mount("http://", adapter)

def tg_request(method, endpoint, **kwargs):
    timeout = kwargs.pop('timeout', 30)
    url = f"{TELEGRAM_API}/{endpoint}"
    return _tg_session.request(method, url, timeout=timeout, **kwargs)

def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = tg_request("POST", "sendMessage", json=payload)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"send_message error: {e}")
        return None

def edit_message(chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": parse_mode}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = tg_request("POST", "editMessageText", json=payload)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"edit_message error: {e}")
        return None

def answer_callback_query(callback_query_id, text=None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        tg_request("POST", "answerCallbackQuery", json=payload)
    except:
        pass

def send_video(chat_id, video_path, caption=None, reply_markup=None):
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
            resp = _tg_session.post(url, data=data, files=files, timeout=180)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"send_video error: {e}")
        return None

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    if request.headers.get("content-type") != "application/json":
        abort(403)
    
    update = request.get_json()
    
    # Forward to HF Spaces for processing
    try:
        resp = requests.post(
            f"{HF_API_URL}/process",
            json=update,
            timeout=10  # Quick forward, HF handles async
        )
        logger.info(f"HF process: {resp.status_code}")
    except Exception as e:
        logger.error(f"HF forward error: {e}")
    
    return "OK", 200

@app.route("/health")
def health():
    return "OK", 200

# Webhook setup at startup (Render has reliable network)
try:
    requests.post(f"{TELEGRAM_API}/deleteWebhook", timeout=10)
    WEBHOOK_URL = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}{WEBHOOK_PATH}"
    resp = requests.post(f"{TELEGRAM_API}/setWebhook", json={"url": WEBHOOK_URL}, timeout=10)
    logger.info(f"Webhook set: {resp.status_code} - {resp.text}")
except Exception as e:
    logger.error(f"Webhook setup error: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)