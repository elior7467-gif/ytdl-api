from flask import Flask, request, jsonify
import yt_dlp
import os
import re
import time
from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING
from pymongo.errors import PyMongoError, DuplicateKeyError

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
REQUEST_DELAY  = 1.0
COOKIES_FILE   = "cookies.txt"
CACHE_TTL_SEC  = 600   # 10 min format cache

# ── MongoDB ───────────────────────────────────────────────────────────────────
MONGO_URI = os.environ.get(
    "MONGO_URI",
    "mongodb+srv://paradox:AcerDom9088@cluster0.0bg3lex.mongodb.net/?appName=Cluster0"
)
MONGO_DB = os.environ.get("MONGO_DB", "yt_formats")

_db = None

def get_db():
    global _db
    if _db is None:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        _db = client[MONGO_DB]
        _setup_indexes(_db)
        app.logger.info("MongoDB connected ✓")
    return _db

def _setup_indexes(db):
    # Format cache — auto-expire after CACHE_TTL_SEC
    db["format_cache"].create_index("video_id", unique=True)
    db["format_cache"].create_index(
        [("cached_at", ASCENDING)],
        expireAfterSeconds=CACHE_TTL_SEC,
        name="cache_ttl",
    )
    # Download log — unique per (video_id + type) to block re-downloads
    db["downloads"].create_index(
        [("video_id", ASCENDING), ("type", ASCENDING)],
        unique=True,
        name="one_time_lock",
    )
    db["downloads"].create_index("downloaded_at")
    # Name search index on title field
    db["format_cache"].create_index([("title", "text")], name="title_text_search")


# ── Cache helpers ─────────────────────────────────────────────────────────────

def cache_get(video_id: str):
    try:
        doc = get_db()["format_cache"].find_one({"video_id": video_id})
        if not doc:
            return None, None
        age = (datetime.now(timezone.utc) - doc["cached_at"]).total_seconds()
        if age > CACHE_TTL_SEC:
            return None, None
        return doc.get("title"), doc.get("formats", [])
    except PyMongoError as e:
        app.logger.warning(f"cache_get error: {e}")
        return None, None

def cache_set(video_id: str, title: str, formats: list):
    try:
        get_db()["format_cache"].update_one(
            {"video_id": video_id},
            {"$set": {
                "video_id":  video_id,
                "title":     title,
                "formats":   formats,
                "cached_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        )
    except PyMongoError as e:
        app.logger.warning(f"cache_set error: {e}")

def cache_search_by_name(query: str) -> list:
    """Full-text search on cached video titles. Returns list of {video_id, title} dicts."""
    try:
        cursor = get_db()["format_cache"].find(
            {"$text": {"$search": query}},
            {"video_id": 1, "title": 1, "_id": 0, "score": {"$meta": "textScore"}},
        ).sort([("score", {"$meta": "textScore"})]).limit(10)
        return list(cursor)
    except PyMongoError as e:
        app.logger.warning(f"cache_search error: {e}")
        return []


# ── Download-log helpers ──────────────────────────────────────────────────────

def is_already_downloaded(video_id: str, dl_type: str) -> bool:
    try:
        return get_db()["downloads"].find_one(
            {"video_id": video_id, "type": dl_type}, {"_id": 1}
        ) is not None
    except PyMongoError:
        return False

def mark_downloaded(video_id: str, dl_type: str, title: str, url: str) -> bool:
    """Returns True if newly marked, False if duplicate (already downloaded)."""
    try:
        get_db()["downloads"].insert_one({
            "video_id":      video_id,
            "type":          dl_type,
            "title":         title,
            "url":           url,
            "downloaded_at": datetime.now(timezone.utc),
        })
        return True
    except DuplicateKeyError:
        return False
    except PyMongoError:
        return True  # don't block on DB error


# ── Helpers ───────────────────────────────────────────────────────────────────

VIDEO_ID_PATTERNS = [
    r"(?:v=|\/)([0-9A-Za-z_-]{11})",
    r"youtu\.be\/([0-9A-Za-z_-]{11})",
]

def extract_video_id(url: str):
    if not url:
        return None
    for p in VIDEO_ID_PATTERNS:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

def safe_int(v):
    try:
        return int(v) if v is not None else None
    except Exception:
        return None

def get_yt_formats_and_meta(youtube_url: str):
    """Fetch formats via yt-dlp (with cache). Returns (title, video_id, formats, err_status, err_reason)."""
    video_id = extract_video_id(youtube_url)

    # Try cache first
    if video_id:
        cached_title, cached_formats = cache_get(video_id)
        if cached_formats is not None:
            app.logger.info(f"Cache HIT for {video_id}")
            return cached_title, video_id, cached_formats, None, None

    ydl_opts = {
        "quiet":        True,
        "no_warnings":  True,
        "skip_download": True,
        "extract_flat": False,
        "http_headers": {
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if os.path.exists(COOKIES_FILE):
        ydl_opts["cookies"] = COOKIES_FILE
        app.logger.info(f"Using cookies from {COOKIES_FILE}")
    else:
        app.logger.warning(f"Cookies file not found: {COOKIES_FILE}")

    try:
        time.sleep(REQUEST_DELAY)
        app.logger.info(f"Fetching formats for: {youtube_url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)

        if not info:
            return None, None, [], "ERROR", "No info returned by yt-dlp"

        title    = info.get("title")
        video_id = info.get("id")
        formats  = []

        for f in info.get("formats", []):
            if not f.get("url"):
                continue
            ext  = f.get("ext") or "unknown"
            mime = f.get("mimeType") or f.get("format", "")
            if "/" in mime:
                ext = mime.split("/")[1].split(";")[0]
            has_video = f.get("vcodec", "none") != "none"
            has_audio = f.get("acodec", "none") != "none"
            formats.append({
                "itag":         f.get("format_id"),
                "url":          f.get("url"),
                "ext":          ext,
                "mimeType":     mime or f.get("format"),
                "qualityLabel": f.get("quality_label") or f.get("resolution"),
                "height":       safe_int(f.get("height")),
                "width":        safe_int(f.get("width")),
                "fps":          safe_int(f.get("fps")),
                "abr":          safe_int(f.get("tbr") or f.get("abr")),
                "vbr":          safe_int(f.get("tbr")),
                "filesize":     safe_int(f.get("filesize") or f.get("filesize_approx")),
                "vcodec":       f.get("vcodec", "unknown"),
                "acodec":       f.get("acodec", "none"),
                "has_video":    has_video,
                "has_audio":    has_audio,
            })

        if video_id:
            cache_set(video_id, title, formats)

        return title, video_id, formats, None, None

    except Exception as e:
        msg = str(e)
        app.logger.exception(f"yt-dlp error: {e}")
        if any(k in msg for k in ("Sign in", "bot", "LOGIN_REQUIRED")):
            return None, None, [], "LOGIN_REQUIRED", "Sign in required — cookies may be invalid/expired"
        return None, None, [], "ERROR", msg


# ── Route: GET / or /online — formats lookup ──────────────────────────────────
@app.route("/", methods=["GET", "HEAD"])
@app.route("/online", methods=["GET"])
def formats_endpoint():
    youtube_url = (request.args.get("url") or request.args.get("u") or "").strip()

    if not youtube_url:
        return jsonify({
            "status":  "ok",
            "service": "yt-formats-api (yt-dlp + MongoDB cache)",
            "version": "3.0",
            "endpoints": {
                "GET /?url=<yt_url>":           "Get all formats for a YouTube URL",
                "GET /search?q=<name>":         "Search cached videos by name/title",
                "GET /status?url=<yt_url>":     "Check if video/audio already downloaded",
                "GET /history":                 "List all download history",
            },
        }), 200

    if not any(d in youtube_url for d in ("youtube.com", "youtu.be")):
        return jsonify({"error": "url does not look like a YouTube URL"}), 400

    video_id = extract_video_id(youtube_url)
    if not video_id:
        return jsonify({"error": "could not extract video id from url"}), 400

    result = get_yt_formats_and_meta(youtube_url)
    title, vid_id, formats, err_status, err_reason = result

    if err_status:
        return jsonify({
            "error":               "failed to extract formats",
            "video_id":            vid_id or video_id,
            "requested_url":       youtube_url,
            "playability_status":  err_status,
            "playability_reason":  err_reason,
            "note":                "Check cookies validity or update yt-dlp.",
        }), 403 if err_status == "LOGIN_REQUIRED" else 500

    if not formats:
        return jsonify({
            "error":       "no formats found",
            "video_id":    vid_id or video_id,
            "title":       title,
            "requested_url": youtube_url,
        }), 404

    muxed  = sorted([f for f in formats if f["has_video"] and f["has_audio"]],
                    key=lambda e: (e.get("height") or 0, e.get("fps") or 0), reverse=True)
    videos = sorted([f for f in formats if f["has_video"] and not f["has_audio"]],
                    key=lambda e: (e.get("height") or 0, e.get("fps") or 0), reverse=True)
    audios = sorted([f for f in formats if f["has_audio"] and not f["has_video"]],
                    key=lambda e: (e.get("abr") or 0), reverse=True)

    def build_entry(f):
        return {k: f[k] for k in
                ("itag","ext","mimeType","qualityLabel","height","width",
                 "fps","vcodec","acodec","abr","vbr","filesize","url")}

    already_video = is_already_downloaded(vid_id or video_id, "video")
    already_audio = is_already_downloaded(vid_id or video_id, "audio")

    return jsonify({
        "status":          "ok",
        "video_id":        vid_id or video_id,
        "title":           title,
        "requested_url":   youtube_url,
        "already_downloaded": {
            "video": already_video,
            "audio": already_audio,
        },
        "muxed_formats":   [build_entry(f) for f in muxed],
        "video_formats":   [build_entry(f) for f in videos],
        "audio_formats":   [build_entry(f) for f in audios],
        "total_formats":   len(formats),
        "cached":          True,
    }), 200


# ── Route: GET /search — search cached videos by name ─────────────────────────
@app.route("/search", methods=["GET"])
def search_by_name():
    """
    Search previously cached video titles by keyword.
    GET /search?q=shape+of+you
    """
    query = (request.args.get("q") or request.args.get("name") or "").strip()
    if not query:
        return jsonify({"error": "q param is required (e.g. ?q=song+name)"}), 400

    results = cache_search_by_name(query)

    if not results:
        return jsonify({
            "status":  "ok",
            "query":   query,
            "results": [],
            "note":    "No cached videos matched. Try fetching the URL first via /?url=...",
        }), 200

    return jsonify({
        "status":  "ok",
        "query":   query,
        "count":   len(results),
        "results": [{"video_id": r["video_id"], "title": r["title"]} for r in results],
    }), 200


# ── Route: GET /status — check if already downloaded ─────────────────────────
@app.route("/status", methods=["GET"])
def check_status():
    """
    GET /status?url=<yt_url>&type=both   (type: video | audio | both)
    """
    youtube_url = (request.args.get("url") or "").strip()
    dl_type     = (request.args.get("type") or "both").strip().lower()

    if not youtube_url:
        return jsonify({"error": "url param is required"}), 400

    video_id = extract_video_id(youtube_url)
    if not video_id:
        return jsonify({"error": "could not extract video id"}), 400

    result = {"video_id": video_id}
    if dl_type in ("video", "both"):
        result["video_downloaded"] = is_already_downloaded(video_id, "video")
    if dl_type in ("audio", "both"):
        result["audio_downloaded"] = is_already_downloaded(video_id, "audio")

    return jsonify(result), 200


# ── Route: POST /mark — mark a video/audio as downloaded ─────────────────────
@app.route("/mark", methods=["POST"])
def mark_endpoint():
    """
    Mark a video or audio as downloaded (called by your downloader service).
    POST /mark
    Body: { "url": "<yt_url>", "type": "video|audio", "title": "optional" }
    """
    data     = request.get_json(force=True, silent=True) or {}
    yt_url   = (data.get("url") or "").strip()
    dl_type  = (data.get("type") or "").strip().lower()
    title    = (data.get("title") or "").strip()

    if not yt_url:
        return jsonify({"error": "url is required"}), 400
    if dl_type not in ("video", "audio"):
        return jsonify({"error": "type must be 'video' or 'audio'"}), 400

    video_id = extract_video_id(yt_url)
    if not video_id:
        return jsonify({"error": "could not extract video id"}), 400

    if is_already_downloaded(video_id, dl_type):
        return jsonify({
            "status":   "blocked",
            "video_id": video_id,
            "type":     dl_type,
            "message":  f"Already downloaded as {dl_type}. Blocked.",
        }), 403

    success = mark_downloaded(video_id, dl_type, title or video_id, yt_url)
    if not success:
        return jsonify({
            "status":   "blocked",
            "video_id": video_id,
            "type":     dl_type,
            "message":  "Race condition — already marked by another request.",
        }), 403

    return jsonify({
        "status":   "marked",
        "video_id": video_id,
        "type":     dl_type,
        "title":    title or video_id,
    }), 200


# ── Route: GET /history — list all downloads ──────────────────────────────────
@app.route("/history", methods=["GET"])
def download_history():
    """
    GET /history?limit=50&type=audio
    """
    limit   = min(int(request.args.get("limit", 50)), 200)
    dl_type = (request.args.get("type") or "").strip().lower()

    query = {}
    if dl_type in ("video", "audio"):
        query["type"] = dl_type

    try:
        docs = list(
            get_db()["downloads"]
            .find(query, {"_id": 0})
            .sort("downloaded_at", -1)
            .limit(limit)
        )
        # Convert datetime to ISO string for JSON
        for d in docs:
            if isinstance(d.get("downloaded_at"), datetime):
                d["downloaded_at"] = d["downloaded_at"].isoformat()
        return jsonify({"status": "ok", "count": len(docs), "history": docs}), 200
    except PyMongoError as e:
        return jsonify({"error": str(e)}), 500


# ── Route: GET /cache — list cached video IDs ────────────────────────────────
@app.route("/cache", methods=["GET"])
def list_cache():
    """GET /cache?limit=50"""
    limit = min(int(request.args.get("limit", 50)), 200)
    try:
        docs = list(
            get_db()["format_cache"]
            .find({}, {"video_id": 1, "title": 1, "cached_at": 1, "_id": 0})
            .sort("cached_at", -1)
            .limit(limit)
        )
        for d in docs:
            if isinstance(d.get("cached_at"), datetime):
                d["cached_at"] = d["cached_at"].isoformat()
        return jsonify({"status": "ok", "count": len(docs), "cache": docs}), 200
    except PyMongoError as e:
        return jsonify({"error": str(e)}), 500


# ── Health / Webhook ──────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    try:
        get_db().command("ping")
        mongo_ok = True
    except Exception:
        mongo_ok = False
    return jsonify({
        "status": "ok",
        "mongo":  "connected" if mongo_ok else "unavailable",
    }), 200

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    return jsonify({"status": "webhook-alive"}), 200


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
