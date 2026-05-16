from flask import Flask, request, jsonify, Response, stream_with_context, send_file
import yt_dlp
import os
import re
import time
import uuid
import threading
import subprocess
import tempfile
import requests
from datetime import datetime, timezone
from pymongo import MongoClient, ASCENDING
from pymongo.errors import PyMongoError, DuplicateKeyError

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
REQUEST_DELAY = 1.0
MAX_FILE_SIZE = 500 * 1024 * 1024
COOKIES_FILE  = "cookies.txt"
TEMP_DIR      = tempfile.gettempdir()
TEMP_TTL      = 300
CACHE_TTL_SEC = 600

# ── MongoDB ───────────────────────────────────────────────────────────────────
MONGO_URI = os.environ.get("MONGO_URI", "mongodb+srv://paradox:AcerDom9088@cluster0.0bg3lex.mongodb.net/?appName=Cluster0")
MONGO_DB  = os.environ.get("MONGO_DB", "yt_downloader")

_db = None

def get_db():
    global _db
    if _db is None:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
        _db = client[MONGO_DB]
        _setup_indexes(_db)
    return _db

def _setup_indexes(db):
    db["format_cache"].create_index("video_id", unique=True)
    db["format_cache"].create_index(
        [("cached_at", ASCENDING)],
        expireAfterSeconds=CACHE_TTL_SEC,
        name="cache_ttl",
    )
    # Unique on (video_id + type) — one-time download enforcement
    db["downloads"].create_index(
        [("video_id", ASCENDING), ("type", ASCENDING)],
        unique=True,
        name="one_time_lock",
    )
    db["downloads"].create_index("downloaded_at")


# ── One-time download helpers ─────────────────────────────────────────────────

def is_already_downloaded(video_id: str, dl_type: str) -> bool:
    try:
        return get_db()["downloads"].find_one(
            {"video_id": video_id, "type": dl_type}, {"_id": 1}
        ) is not None
    except PyMongoError:
        return False

def mark_downloaded(video_id: str, dl_type: str, title: str, url: str) -> bool:
    """Returns True if successfully marked, False if already existed (race condition)."""
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
        return True


# ── Format cache ──────────────────────────────────────────────────────────────

def cache_get(video_id: str):
    try:
        doc = get_db()["format_cache"].find_one({"video_id": video_id})
        if not doc:
            return None, None
        age = (datetime.now(timezone.utc) - doc["cached_at"]).total_seconds()
        if age > CACHE_TTL_SEC:
            return None, None
        return doc.get("title"), doc.get("formats", [])
    except PyMongoError:
        return None, None

def cache_set(video_id: str, title: str, formats: list):
    try:
        get_db()["format_cache"].update_one(
            {"video_id": video_id},
            {"$set": {"video_id": video_id, "title": title, "formats": formats,
                       "cached_at": datetime.now(timezone.utc)}},
            upsert=True,
        )
    except PyMongoError:
        pass


# ── Helpers ───────────────────────────────────────────────────────────────────

VIDEO_ID_PATTERNS = [
    r"(?:v=|\/)([0-9A-Za-z_-]{11})",
    r"youtu\.be\/([0-9A-Za-z_-]{11})",
]

def extract_video_id(url: str):
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

def base_ydl_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if os.path.exists(COOKIES_FILE):
        opts["cookies"] = COOKIES_FILE
    return opts

def schedule_delete(path: str, delay: int = TEMP_TTL):
    def _delete():
        time.sleep(delay)
        try:
            os.remove(path)
        except OSError:
            pass
    threading.Thread(target=_delete, daemon=True).start()

def get_formats(youtube_url: str):
    video_id = extract_video_id(youtube_url)
    if video_id:
        cached_title, cached_formats = cache_get(video_id)
        if cached_formats is not None:
            return cached_title, video_id, cached_formats, None, None

    ydl_opts = base_ydl_opts()
    ydl_opts["extract_flat"] = False
    try:
        time.sleep(REQUEST_DELAY)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
        if not info:
            return None, None, [], "ERROR", "No info returned"

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
                "itag": f.get("format_id"), "url": f.get("url"), "ext": ext,
                "height": safe_int(f.get("height")), "fps": safe_int(f.get("fps")),
                "abr": safe_int(f.get("tbr") or f.get("abr")),
                "filesize": safe_int(f.get("filesize") or f.get("filesize_approx")),
                "vcodec": f.get("vcodec", "unknown"), "acodec": f.get("acodec", "none"),
                "has_video": has_video, "has_audio": has_audio,
            })

        if video_id:
            cache_set(video_id, title, formats)
        return title, video_id, formats, None, None

    except Exception as e:
        msg = str(e)
        if any(k in msg for k in ("Sign in", "bot", "LOGIN_REQUIRED")):
            return None, None, [], "LOGIN_REQUIRED", msg
        return None, None, [], "ERROR", msg

def pick_best_video(formats):
    videos = [f for f in formats if f["has_video"] and not f["has_audio"]]
    if not videos:
        videos = [f for f in formats if f["has_video"] and f["has_audio"]]
    return max(videos, key=lambda f: ((f["height"] or 0), (f["fps"] or 0))) if videos else None

def pick_best_audio(formats):
    audios = [f for f in formats if f["has_audio"] and not f["has_video"]]
    return max(audios, key=lambda f: (f["abr"] or 0)) if audios else None

def safe_filename(title, video_id, suffix):
    base = re.sub(r"[^\w\s-]", "", title or video_id).strip().replace(" ", "_")
    return f"{base}{suffix}"

def proxy_stream(stream_url):
    hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    r = requests.get(stream_url, headers=hdrs, stream=True, timeout=30)
    r.raise_for_status()
    def generate():
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                yield chunk
    return generate, r.headers


# ── Route: GET /video ─────────────────────────────────────────────────────────
@app.route("/video", methods=["GET"])
def download_video():
    """
    Download best quality video merged with audio.
    ONE-TIME ONLY — blocked on second request.

    GET /video?url=https%3A%2F%2Fyoutu.be%2FdQw4w9WgXcQ
    """
    youtube_url = request.args.get("url", "").strip()
    if not youtube_url:
        return jsonify({"error": "url param is required"}), 400
    if not any(d in youtube_url for d in ("youtube.com", "youtu.be")):
        return jsonify({"error": "Not a YouTube URL"}), 400

    video_id = extract_video_id(youtube_url)
    if not video_id:
        return jsonify({"error": "Could not extract video ID"}), 400

    # Block if already downloaded
    if is_already_downloaded(video_id, "video"):
        return jsonify({
            "error":    "Already downloaded",
            "video_id": video_id,
            "message":  "This video was already downloaded once. Download blocked.",
        }), 403

    title, video_id, formats, err_status, err_reason = get_formats(youtube_url)
    if err_status:
        return jsonify({"error": err_reason}), 403 if err_status == "LOGIN_REQUIRED" else 500
    if not formats:
        return jsonify({"error": "No formats found"}), 404

    best_video = pick_best_video(formats)
    best_audio = pick_best_audio(formats)
    if not best_video:
        return jsonify({"error": "No video format found"}), 404

    # Mark BEFORE streaming to prevent race conditions
    if not mark_downloaded(video_id, "video", title, youtube_url):
        return jsonify({"error": "Already downloaded", "video_id": video_id,
                        "message": "Just downloaded by another request."}), 403

    has_ffmpeg = subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0

    # Merge video + audio via ffmpeg if possible
    if has_ffmpeg and best_audio and not best_video["has_audio"]:
        job_id   = uuid.uuid4().hex[:8]
        v_path   = os.path.join(TEMP_DIR, f"{job_id}_video.{best_video.get('ext','mp4')}")
        a_path   = os.path.join(TEMP_DIR, f"{job_id}_audio.{best_audio.get('ext','m4a')}")
        out_path = os.path.join(TEMP_DIR, f"{job_id}_merged.mp4")
        dl_hdrs  = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        def dl(url, dest):
            with requests.get(url, headers=dl_hdrs, stream=True, timeout=60) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=262144):
                        if chunk: fh.write(chunk)

        try:
            dl(best_video["url"], v_path)
            dl(best_audio["url"], a_path)
        except Exception as e:
            for p in (v_path, a_path):
                try: os.remove(p)
                except OSError: pass
            return jsonify({"error": f"Download failed: {e}"}), 502

        cmd = ["ffmpeg", "-y", "-i", v_path, "-i", a_path,
               "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", out_path]
        result = subprocess.run(cmd, capture_output=True, timeout=300)

        for p in (v_path, a_path):
            try: os.remove(p)
            except OSError: pass

        if result.returncode != 0:
            try: os.remove(out_path)
            except OSError: pass
            return jsonify({"error": "ffmpeg merge failed",
                            "detail": result.stderr.decode(errors="replace")[-500:]}), 500

        schedule_delete(out_path)
        return send_file(out_path, mimetype="video/mp4", as_attachment=True,
                         download_name=safe_filename(title, video_id, "_video.mp4"))

    # Fallback: proxy stream directly
    try:
        generate, upstream_headers = proxy_stream(best_video["url"])
    except Exception as e:
        return jsonify({"error": f"Stream failed: {e}"}), 502

    filename = safe_filename(title, video_id, f"_video.{best_video.get('ext','mp4')}")
    headers  = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type":        upstream_headers.get("Content-Type", "video/mp4"),
        "Accept-Ranges":       "bytes",
    }
    if "Content-Length" in upstream_headers:
        headers["Content-Length"] = upstream_headers["Content-Length"]

    return Response(stream_with_context(generate()), status=200, headers=headers)


# ── Route: GET /audio ─────────────────────────────────────────────────────────
@app.route("/audio", methods=["GET"])
def download_audio():
    """
    Download best quality audio only.
    ONE-TIME ONLY — blocked on second request.

    GET /audio?url=https%3A%2F%2Fyoutu.be%2FdQw4w9WgXcQ
    """
    youtube_url = request.args.get("url", "").strip()
    if not youtube_url:
        return jsonify({"error": "url param is required"}), 400
    if not any(d in youtube_url for d in ("youtube.com", "youtu.be")):
        return jsonify({"error": "Not a YouTube URL"}), 400

    video_id = extract_video_id(youtube_url)
    if not video_id:
        return jsonify({"error": "Could not extract video ID"}), 400

    # Block if already downloaded
    if is_already_downloaded(video_id, "audio"):
        return jsonify({
            "error":    "Already downloaded",
            "video_id": video_id,
            "message":  "This audio was already downloaded once. Download blocked.",
        }), 403

    title, video_id, formats, err_status, err_reason = get_formats(youtube_url)
    if err_status:
        return jsonify({"error": err_reason}), 403 if err_status == "LOGIN_REQUIRED" else 500
    if not formats:
        return jsonify({"error": "No formats found"}), 404

    best_audio = pick_best_audio(formats)
    if not best_audio:
        return jsonify({"error": "No audio format found"}), 404

    # Mark BEFORE streaming
    if not mark_downloaded(video_id, "audio", title, youtube_url):
        return jsonify({"error": "Already downloaded", "video_id": video_id,
                        "message": "Just downloaded by another request."}), 403

    try:
        generate, upstream_headers = proxy_stream(best_audio["url"])
    except Exception as e:
        return jsonify({"error": f"Stream failed: {e}"}), 502

    ext      = best_audio.get("ext", "m4a")
    filename = safe_filename(title, video_id, f"_audio.{ext}")
    headers  = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type":        upstream_headers.get("Content-Type", f"audio/{ext}"),
        "Accept-Ranges":       "bytes",
    }
    if "Content-Length" in upstream_headers:
        headers["Content-Length"] = upstream_headers["Content-Length"]

    return Response(stream_with_context(generate()), status=200, headers=headers)


# ── Route: GET /status ────────────────────────────────────────────────────────
@app.route("/status", methods=["GET"])
def check_status():
    """
    Check if a video/audio URL has already been downloaded.

    GET /status?url=<yt_url>&type=both   (type: video | audio | both)
    """
    youtube_url = request.args.get("url", "").strip()
    dl_type     = request.args.get("type", "both").strip().lower()
    if not youtube_url:
        return jsonify({"error": "url param is required"}), 400

    video_id = extract_video_id(youtube_url)
    if not video_id:
        return jsonify({"error": "Could not extract video ID"}), 400

    result = {"video_id": video_id}
    if dl_type in ("video", "both"):
        result["video_downloaded"] = is_already_downloaded(video_id, "video")
    if dl_type in ("audio", "both"):
        result["audio_downloaded"] = is_already_downloaded(video_id, "audio")

    return jsonify(result), 200


# ── Health ────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET", "HEAD"])
@app.route("/online", methods=["GET"])
def health():
    try:
        get_db().command("ping")
        mongo_ok = True
    except Exception:
        mongo_ok = False

    return jsonify({
        "status":  "ok",
        "service": "yt-downloader-api",
        "mongo":   "connected" if mongo_ok else "unavailable",
        "endpoints": {
            "GET /video?url=<yt_url>":              "Best quality video (one-time only)",
            "GET /audio?url=<yt_url>":              "Best quality audio (one-time only)",
            "GET /status?url=<yt_url>&type=both":   "Check if already downloaded",
        },
    }), 200

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    return jsonify({"status": "webhook-alive"}), 200


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
