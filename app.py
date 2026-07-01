import os
import io
import json
import hashlib
import threading
import tempfile
import logging
import traceback
import uuid
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin
from flask import Flask, request, abort, jsonify, send_file, Response
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from curl_cffi import requests as cffi_requests, CurlMime

# =====================================================================
# CONFIGURATION
# =====================================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "7045bc4055c6293e84534dd8f6dbb024")
TMDB_BASE_URL = "https://api.themoviedb.org/3"

MAPPLE_WATCH_URL = "https://mapple.rip/watch/{media_type}/{item_id}"
MAPPLE_PLAYBACK_INIT = "https://mapple.rip/api/playback-init"
MAPPLE_STREAM_API = "https://mapple.rip/api/stream"
MAPPLE_API_KEY = "mptv_sk_a8f29c4e7b3d1f"

# RENDER service for catbox uploads (avoids HF IP blocks)
RENDER_CATBOX_BASE = "https://catbox-upload-service.onrender.com"

if not TMDB_API_KEY:
    raise ValueError("TMDB_API_KEY environment variable is required")

app = Flask(__name__)

JOBS_FILE = "/app/download_jobs.json"

def load_jobs():
    try:
        if os.path.exists(JOBS_FILE):
            with open(JOBS_FILE, "r") as f:
                return json.load(f)
    except:
        pass
    return {}

def save_jobs():
    try:
        with open(JOBS_FILE, "w") as f:
            json.dump(download_jobs, f)
    except:
        pass

download_jobs = load_jobs()

# =====================================================================
# MAPPLE SESSION
# =====================================================================
def get_mapple_session():
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=3)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://mapple.rip/",
        "Origin": "https://mapple.rip"
    })
    return session

# =====================================================================
# POW SOLVER
# =====================================================================
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

# =====================================================================
# MAPPLE HANDSHAKE
# =====================================================================
def execute_handshake(session: requests.Session, item_id: int, media_type: str):
    watch_url = MAPPLE_WATCH_URL.format(media_type=media_type, item_id=item_id)
    page_res = session.get(watch_url, timeout=30)

    token_match = re.search(r'window\.__REQUEST_TOKEN__\s*=\s*"([^"]+)"', page_res.text)
    request_token = token_match.group(1) if token_match else None
    if not request_token:
        raise Exception("Could not extract request token from page")

    init_payload = {"mediaId": item_id, "mediaType": media_type, "requestToken": request_token}
    res1 = session.post(MAPPLE_PLAYBACK_INIT, json=init_payload, timeout=30)
    if res1.status_code != 200:
        raise Exception(f"Playback init failed: {res1.text[:200]}")
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

# =====================================================================
# TMDB SEARCH
# =====================================================================
def tmdb_search(query: str, page: int = 1):
    url = f"{TMDB_BASE_URL}/search/multi"
    params = {"api_key": TMDB_API_KEY, "query": query, "page": page, "include_adult": "false", "language": "en-US"}
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return [r for r in data.get("results", []) if r.get("media_type") in ("movie", "tv")][:10]

# =====================================================================
# UPLOAD CHUNK VIA RENDER SERVICE (downloads segment + uploads to catbox)
# =====================================================================
def upload_chunk_via_worker(segment_url: str, chunk_index: int) -> str:
    """Send segment URL to Render service, which downloads and uploads to catbox.moe"""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            resp = requests.post(
                f"{RENDER_CATBOX_BASE}/segment_upload",
                json={"segmentUrl": segment_url, "index": chunk_index},
                timeout=120
            )
            logger.info(f"Chunk {chunk_index} render upload status: {resp.status_code} (attempt {attempt+1})")
            
            if resp.status_code == 200:
                data = resp.json()
                catbox_url = data.get("url")
                if catbox_url and catbox_url.startswith("https://files.catbox.moe/"):
                    return catbox_url
                logger.error(f"Chunk {chunk_index} unexpected response: {data}")
            elif resp.status_code == 412:
                # Stream URL expired
                logger.error(f"Chunk {chunk_index} stream expired (412)")
                return "EXPIRED"
        except Exception as e:
            logger.error(f"Chunk {chunk_index} render upload attempt {attempt+1} failed: {e}")
        
        if attempt < max_retries - 1:
            import time
            time.sleep(2 ** attempt)
    
    return None

# =====================================================================
# DOWNLOAD WORKER - Stream chunks via Render to catbox.moe
# =====================================================================
def download_stream_worker(url: str, title: str, job_id: str):
    """Downloads HLS segments, uploads each to catbox.moe instantly, stores only URLs"""
    session = get_mapple_session()

    try:
        head_check = session.get(url, timeout=30)
        manifest_content = head_check.text.strip()

        if not manifest_content.startswith("#EXTM3U"):
            raise Exception("Not an HLS stream")

        # Parse variant playlist
        variant_playlist_url = url
        lines = manifest_content.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
                if next_line and not next_line.startswith("#"):
                    variant_playlist_url = urljoin(url, next_line)
                    break

        variant_res = session.get(variant_playlist_url, timeout=30)
        segment_urls = []
        for line in variant_res.text.splitlines():
            cleaned = line.strip()
            if cleaned and not cleaned.startswith("#"):
                segment_urls.append(urljoin(variant_playlist_url, cleaned))

        total_segments = len(segment_urls)
        if total_segments == 0:
            raise Exception("Stream manifest processing returned an empty map.")

        download_jobs[job_id]["status"] = "downloading"
        download_jobs[job_id]["total_segments"] = total_segments
        download_jobs[job_id]["chunk_urls"] = [None] * total_segments
        download_jobs[job_id]["cancelled"] = False
        save_jobs()

        completed_count = 0
        counter_lock = threading.Lock()

        def download_and_upload_chunk(chunk_index, chunk_url):
            # Check if cancelled before starting
            if download_jobs.get(job_id, {}).get("cancelled", False):
                return None
            try:
                # Send segment URL to Render service for download + catbox upload
                catbox_url = upload_chunk_via_worker(chunk_url, chunk_index)
                if catbox_url == "EXPIRED":
                    logger.error(f"Chunk {chunk_index} stream expired (412) - stopping download")
                    download_jobs[job_id]["cancelled"] = True
                    download_jobs[job_id]["status"] = "error"
                    download_jobs[job_id]["error"] = "Stream URL expired (412)"
                    save_jobs()
                    return None
                if catbox_url:
                    return chunk_index, catbox_url
            except Exception as e:
                logger.error(f"Chunk {chunk_index} error: {e}")
            return None

        with ThreadPoolExecutor(max_workers=40) as executor:
            futures = {executor.submit(download_and_upload_chunk, idx, chunk_url): idx for idx, chunk_url in enumerate(segment_urls)}

            for future in as_completed(futures):
                # Check if cancelled before processing result
                if download_jobs.get(job_id, {}).get("cancelled", False):
                    # Cancel remaining futures
                    for f in futures:
                        f.cancel()
                    break
                    
                result = future.result()
                if result:
                    chunk_idx, catbox_url = result
                    with counter_lock:
                        download_jobs[job_id]["chunk_urls"][chunk_idx] = catbox_url
                        completed_count += 1
                        download_jobs[job_id]["progress"] = (completed_count / total_segments) * 100
                        save_jobs()

        # Check if all uploaded
        if download_jobs.get(job_id, {}).get("cancelled", False):
            logger.info(f"Download cancelled for job {job_id}")
            return
            
        uploaded = sum(1 for u in download_jobs[job_id]["chunk_urls"] if u)
        if uploaded == total_segments:
            download_jobs[job_id]["status"] = "ready"
            download_jobs[job_id]["progress"] = 100
            logger.info(f"✅ All {total_segments} chunks uploaded to catbox.moe!")
        else:
            download_jobs[job_id]["status"] = "error"
            download_jobs[job_id]["error"] = f"Only {uploaded}/{total_segments} chunks uploaded"
        save_jobs()

    except Exception as e:
        logger.error(f"Download error: {e}\n{traceback.format_exc()}")
        download_jobs[job_id]["status"] = "error"
        download_jobs[job_id]["error"] = str(e)
        save_jobs()

# =====================================================================
# REST API ENDPOINTS
# =====================================================================
@app.route("/search")
def api_search():
    q = request.args.get("q")
    if not q:
        return jsonify({"error": "missing query"}), 400
    try:
        results = tmdb_search(q)
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/stream")
def api_stream():
    item_id = request.args.get("id")
    media_type = request.args.get("type")
    if not item_id or not media_type:
        return jsonify({"error": "missing params"}), 400
    try:
        session = get_mapple_session()
        stream_url = execute_handshake(session, int(item_id), media_type)
        return jsonify({"stream_url": stream_url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/download/start", methods=["POST"])
def api_download_start():
    data = request.get_json()
    url = data.get("url") if data else None
    title = data.get("title", "video") if data else "video"
    if not url:
        return jsonify({"error": "missing url"}), 400

    job_id = str(uuid.uuid4())
    download_jobs[job_id] = {
        "status": "queued",
        "progress": 0,
        "url": url,
        "title": title,
        "created": __import__('time').time()
    }
    save_jobs()

    threading.Thread(target=download_stream_worker, args=(url, title, job_id), daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/download/status")
def api_download_status():
    job_id = request.args.get("id")
    if not job_id or job_id not in download_jobs:
        return jsonify({"error": "not found"}), 404
    return jsonify(download_jobs[job_id])

@app.route("/download/stop", methods=["POST"])
def api_download_stop():
    data = request.get_json()
    job_id = data.get("id") if data else None
    if not job_id or job_id not in download_jobs:
        return jsonify({"error": "not found"}), 404
    
    job = download_jobs[job_id]
    if job["status"] in ("ready", "error", "cancelled"):
        return jsonify({"error": f"job already {job['status']}"}), 400
    
    job["cancelled"] = True
    job["status"] = "cancelled"
    save_jobs()
    logger.info(f"Download stopped for job {job_id}")
    return jsonify({"status": "cancelled"})

@app.route("/download/chunks/<job_id>")
def api_get_chunks(job_id):
    """Get chunk URLs for the player"""
    job = download_jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    if job["status"] != "ready":
        return jsonify({"error": "not ready", "status": job["status"]}), 400
    return jsonify({
        "chunk_urls": job.get("chunk_urls", []),
        "title": job.get("title", "video"),
        "total_segments": job.get("total_segments", 0)
    })

@app.route("/health")
def health():
    return "OK"

# =====================================================================
# MEDIASOURCE PLAYER PAGE - Streams from catbox.moe chunks + IndexedDB
# =====================================================================
@app.route("/play/<job_id>")
def media_player_page(job_id):
    job = download_jobs.get(job_id)
    if not job or job["status"] != "ready":
        return """<!DOCTYPE html><html><head><title>Video Not Ready</title></head>
<body style="background:#111;color:#fff;font-family:sans-serif;padding:40px;text-align:center">
<h2>Video Not Ready</h2><p>Job not found or still processing...</p>
</body></html>""", 404

    chunk_urls = job.get("chunk_urls", [])
    title = job.get("title", "video")

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🎬 {title}</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{ margin:0; background:#000; color:#fff; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; }}
        .container {{ max-width:100%; margin:0 auto; padding:20px; }}
        .video-wrapper {{ position:relative; width:100%; aspect-ratio:16/9; background:#000; border-radius:8px; overflow:hidden; }}
        video {{ width:100%; height:100%; display:block; }}
        .controls {{ position:absolute; bottom:0; left:0; right:0; padding:16px; background:linear-gradient(transparent, rgba(0,0,0,0.9)); z-index:10; }}
        .progress-bar {{ height:4px; background:rgba(255,255,255,0.3); border-radius:2px; cursor:pointer; margin-bottom:8px; }}
        .progress-fill {{ height:100%; background:#e50914; border-radius:2px; width:0%; transition:width 0.1s; }}
        .control-row {{ display:flex; align-items:center; gap:12px; flex-wrap:wrap; }}
        button {{ background:#e50914; color:#fff; border:none; padding:10px 16px; border-radius:4px; cursor:pointer; font-size:14px; font-weight:600; }}
        button:hover {{ background:#f40612; }}
        button:disabled {{ background:#555; cursor:not-allowed; }}
        .status {{ font-size:12px; color:#aaa; flex:1; }}
        .storage-info {{ font-size:11px; color:#888; margin-top:4px; }}
        @media (max-width: 600px) {{ .container {{ padding:10px; }} button {{ padding:8px 12px; font-size:13px; }} }}
    </style>
</head>
<body>
    <div class="container">
        <div class="video-wrapper">
            <video id="video" controls playsinline preload="metadata"></video>
            <div class="controls">
                <div class="progress-bar" id="progressBar"><div class="progress-fill" id="progressFill"></div></div>
                <div class="control-row">
                    <button id="playPauseBtn">⏸️ Pause</button>
                    <button id="downloadBtn" disabled>📥 Download MP4</button>
                    <span class="status" id="statusText">Loading...</span>
                </div>
                <div class="storage-info" id="storageInfo">IndexedDB: 0%</div>
            </div>
        </div>
    </div>

    <script>
        const CHUNK_URLS = {json.dumps(chunk_urls)};
        const TOTAL_CHUNKS = CHUNK_URLS.length;
        const VIDEO_TITLE = {json.dumps(title)};
        const DB_NAME = 'videoChunks_' + location.pathname.split('/').pop();
        const STORE_NAME = 'chunks';
        const CHUNK_SIZE = 1024 * 1024; // 1MB chunks for IndexedDB

        const video = document.getElementById('video');
        const playPauseBtn = document.getElementById('playPauseBtn');
        const downloadBtn = document.getElementById('downloadBtn');
        const progressBar = document.getElementById('progressBar');
        const progressFill = document.getElementById('progressFill');
        const statusText = document.getElementById('statusText');
        const storageInfo = document.getElementById('storageInfo');

        let mediaSource = null;
        let sourceBuffer = null;
        let chunksBuffer = [];
        let currentChunk = 0;
        let isDownloading = false;
        let db = null;

        // Initialize IndexedDB
        function initDB() {{
            return new Promise((resolve, reject) => {{
                const request = indexedDB.open(DB_NAME, 1);
                request.onerror = () => reject(request.error);
                request.onsuccess = () => {{ db = request.result; resolve(); }};
                request.onupgradeneeded = (e) => {{
                    const database = e.target.result;
                    if (!database.objectStoreNames.contains(STORE_NAME)) {{
                        database.createObjectStore(STORE_NAME, {{ keyPath: 'index' }});
                    }}
                }};
            }});
        }}

        // Store chunk in IndexedDB
        function storeChunk(index, data) {{
            return new Promise((resolve, reject) => {{
                if (!db) return resolve();
                const tx = db.transaction(STORE_NAME, 'readwrite');
                const store = tx.objectStore(STORE_NAME);
                store.put({{ index, data }});
                tx.oncomplete = () => resolve();
                tx.onerror = () => reject(tx.error);
            }});
        }}

        // Fetch and download all chunks
        async function downloadChunks() {{
            if (isDownloading) return;
            isDownloading = true;
            statusText.textContent = 'Downloading chunks...';

            await initDB();

            for (let i = 0; i < TOTAL_CHUNKS; i++) {{
                if (CHUNK_URLS[i] === null || CHUNK_URLS[i] === undefined) continue;
                
                try {{
                    const res = await fetch(CHUNK_URLS[i], {{ mode: 'cors' }});
                    if (!res.ok) {{
                        console.error(`Chunk ${{i}} failed: ${{res.status}}`);
                        continue;
                    }}
                    const arrayBuffer = await res.arrayBuffer();
                    await storeChunk(i, arrayBuffer);
                    
                    const pct = Math.round((i + 1) / TOTAL_CHUNKS * 100);
                    storageInfo.textContent = `IndexedDB: ${{pct}}%`;
                }} catch (e) {{
                    console.error(`Chunk ${{i}} error:`, e);
                }}
            }}

            downloadBtn.disabled = false;
            statusText.textContent = 'Ready for offline viewing!';
        }}

        // Download stitched MP4 from IndexedDB
        async function downloadStitchedVideo() {{
            const chunks = [];
            for (let i = 0; i < TOTAL_CHUNKS; i++) {{
                const tx = db.transaction(STORE_NAME, 'readonly');
                const store = tx.objectStore(STORE_NAME);
                const request = store.get(i);
                await new Promise((resolve, reject) => {{
                    request.onsuccess = () => {{ chunks[i] = request.result?.data; resolve(); }};
                    request.onerror = () => reject(request.error);
                }});
            }}

            const blob = new Blob(chunks.filter(Boolean), {{ type: 'video/mp4' }});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = VIDEO_TITLE + '.mp4';
            a.click();
            URL.revokeObjectURL(url);
        }}

        downloadBtn.addEventListener('click', downloadStitchedVideo);
        downloadChunks();
    </script>
</body>
</html>"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port)