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

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
REQUEST_DELAY  = 1.0
MAX_FILE_SIZE  = 500 * 1024 * 1024   # 500 MB
SUPPORTED_Q    = ["720p", "1080p", "4k"]
COOKIES_FILE   = "cookies.txt"
TEMP_DIR       = tempfile.gettempdir()
TEMP_TTL       = 300                  # seconds before temp files are deleted

QUALITY_HEIGHT = {"720p": 720, "1080p": 1080, "4k": 2160}

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
    """Delete a file after `delay` seconds in a background thread."""
    def _delete():
        time.sleep(delay)
        try:
            os.remove(path)
        except OSError:
            pass
    threading.Thread(target=_delete, daemon=True).start()

def get_formats(youtube_url: str):
    """
    Returns (title, video_id, formats, err_status, err_reason).
    On success err_status and err_reason are None.
    """
    ydl_opts = base_ydl_opts()
    ydl_opts["extract_flat"] = False

    try:
        time.sleep(REQUEST_DELAY)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)

        if not info:
            return None, None, [], "ERROR", "No info returned"

        title       = info.get("title")
        video_id    = info.get("id")
        formats_raw = info.get("formats", [])

        formats = []
        for f in formats_raw:
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

        return title, video_id, formats, None, None

    except Exception as e:
        msg = str(e)
        if any(k in msg for k in ("Sign in", "bot", "LOGIN_REQUIRED")):
            return None, None, [], "LOGIN_REQUIRED", msg
        return None, None, [], "ERROR", msg

def pick_muxed(formats: list, quality: str):
    """
    Return the best muxed (video+audio) format at or below the requested height.
    Falls back to the best available muxed if none match exactly.
    """
    target = QUALITY_HEIGHT.get(quality, 1080)
    muxed  = [f for f in formats if f["has_video"] and f["has_audio"]]
    if not muxed:
        return None
    candidates = [f for f in muxed if (f["height"] or 0) <= target]
    pool       = candidates if candidates else muxed
    return max(pool, key=lambda f: ((f["height"] or 0), (f["fps"] or 0)))

def pick_adaptive(formats: list, quality: str):
    """
    Return (best_video_format, best_audio_format) for adaptive streams.
    Video track has no audio; picks closest height <= target.
    """
    target  = QUALITY_HEIGHT.get(quality, 1080)
    videos  = [f for f in formats if f["has_video"] and not f["has_audio"]]
    audios  = [f for f in formats if f["has_audio"] and not f["has_video"]]
    if not videos or not audios:
        return None, None
    candidates  = [f for f in videos if (f["height"] or 0) <= target]
    pool        = candidates if candidates else videos
    best_video  = max(pool,   key=lambda f: ((f["height"] or 0), (f["fps"] or 0)))
    best_audio  = max(audios, key=lambda f: (f["abr"] or 0))
    return best_video, best_audio

# ── Route: POST /download ─────────────────────────────────────────────────────
@app.route("/download", methods=["POST"])
def download_info():
    """Return available qualities and direct stream URLs (no file transfer)."""
    body        = request.get_json(silent=True) or {}
    youtube_url = body.get("url", "").strip()

    if not youtube_url:
        return jsonify({"error": "url is required"}), 400
    if not any(d in youtube_url for d in ("youtube.com", "youtu.be")):
        return jsonify({"error": "Not a YouTube URL"}), 400

    title, video_id, formats, err_status, err_reason = get_formats(youtube_url)

    if err_status:
        code = 403 if err_status == "LOGIN_REQUIRED" else 500
        return jsonify({"error": err_reason, "status": err_status}), code
    if not formats:
        return jsonify({"error": "No formats found"}), 404

    muxed  = sorted(
        [f for f in formats if f["has_video"] and f["has_audio"]],
        key=lambda f: (f["height"] or 0, f["fps"] or 0), reverse=True,
    )
    videos = sorted(
        [f for f in formats if f["has_video"] and not f["has_audio"]],
        key=lambda f: (f["height"] or 0, f["fps"] or 0), reverse=True,
    )
    audios = sorted(
        [f for f in formats if f["has_audio"] and not f["has_video"]],
        key=lambda f: (f["abr"] or 0), reverse=True,
    )

    def entry(f):
        return {k: v for k, v in f.items() if k not in ("has_video", "has_audio")}

    cookies_note = (
        f"Using cookies from {COOKIES_FILE}"
        if os.path.exists(COOKIES_FILE)
        else "No cookies — running anonymously"
    )

    return jsonify({
        "status":        "ok",
        "video_id":      video_id,
        "title":         title,
        "requested_url": youtube_url,
        "muxed_formats": [entry(f) for f in muxed],
        "video_formats": [entry(f) for f in videos],
        "audio_formats": [entry(f) for f in audios],
        "total_formats": len(formats),
        "note":          cookies_note,
    }), 200


# ── Route: GET /direct-download ───────────────────────────────────────────────
@app.route("/direct-download", methods=["GET"])
def direct_download():
    """
    Proxy-stream a muxed (video+audio) format with Range request support.

    Query params (BOTH must be percent-encoded if the YouTube URL contains & or ?):
      url     — YouTube watch URL
      quality — 720p | 1080p | 4k  (default: 1080p)

    Example (correct encoding):
      /direct-download?url=https%3A%2F%2Fyoutu.be%2FdQw4w9WgXcQ&quality=1080p
    """
    youtube_url = request.args.get("url", "").strip()
    quality     = request.args.get("quality", "1080p").strip().lower()

    if not youtube_url:
        return jsonify({"error": "url param is required"}), 400
    if not any(d in youtube_url for d in ("youtube.com", "youtu.be")):
        return jsonify({"error": "Not a YouTube URL"}), 400
    if quality not in SUPPORTED_Q:
        return jsonify({"error": f"quality must be one of {SUPPORTED_Q}"}), 400

    title, video_id, formats, err_status, err_reason = get_formats(youtube_url)
    if err_status:
        code = 403 if err_status == "LOGIN_REQUIRED" else 500
        return jsonify({"error": err_reason}), code
    if not formats:
        return jsonify({"error": "No formats found"}), 404

    fmt = pick_muxed(formats, quality)
    if not fmt:
        return jsonify({"error": "No muxed format available for this quality"}), 404

    stream_url = fmt["url"]
    filesize   = fmt.get("filesize")
    if filesize and filesize > MAX_FILE_SIZE:
        return jsonify({
            "error": (
                f"File too large ({filesize // (1024**2)} MB). "
                f"Max is {MAX_FILE_SIZE // (1024**2)} MB."
            )
        }), 413

    # Proxy to YouTube CDN, forwarding Range header if present
    range_header    = request.headers.get("Range")
    upstream_hdrs   = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    if range_header:
        upstream_hdrs["Range"] = range_header

    try:
        r = requests.get(stream_url, headers=upstream_hdrs, stream=True, timeout=30)
    except requests.RequestException as e:
        return jsonify({"error": f"Failed to fetch stream: {e}"}), 502

    safe_title = re.sub(r"[^\w\s-]", "", title or video_id).strip().replace(" ", "_")
    ext        = fmt.get("ext", "mp4")
    filename   = f"{safe_title}_{quality}.{ext}"

    resp_headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type":        r.headers.get("Content-Type", "video/mp4"),
        "Accept-Ranges":       "bytes",
    }
    if "Content-Length" in r.headers:
        resp_headers["Content-Length"] = r.headers["Content-Length"]
    if "Content-Range" in r.headers:
        resp_headers["Content-Range"] = r.headers["Content-Range"]

    status_code = 206 if range_header else 200

    def generate():
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                yield chunk

    return Response(
        stream_with_context(generate()),
        status=status_code,
        headers=resp_headers,
    )


# ── Route: GET /merge-download ────────────────────────────────────────────────
@app.route("/merge-download", methods=["GET"])
def merge_download():
    """
    Download adaptive video + audio tracks, merge via ffmpeg, return MP4.

    Query params (BOTH must be percent-encoded):
      url     — YouTube watch URL
      quality — 720p | 1080p | 4k  (default: 1080p)

    Falls back to streaming a muxed format if no adaptive tracks are found.
    Temp files are deleted automatically after 5 minutes.
    """
    youtube_url = request.args.get("url", "").strip()
    quality     = request.args.get("quality", "1080p").strip().lower()

    if not youtube_url:
        return jsonify({"error": "url param is required"}), 400
    if not any(d in youtube_url for d in ("youtube.com", "youtu.be")):
        return jsonify({"error": "Not a YouTube URL"}), 400
    if quality not in SUPPORTED_Q:
        return jsonify({"error": f"quality must be one of {SUPPORTED_Q}"}), 400

    # Verify ffmpeg is available before doing expensive work
    if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0:
        return jsonify({"error": "ffmpeg not found on server"}), 500

    title, video_id, formats, err_status, err_reason = get_formats(youtube_url)
    if err_status:
        code = 403 if err_status == "LOGIN_REQUIRED" else 500
        return jsonify({"error": err_reason}), code
    if not formats:
        return jsonify({"error": "No formats found"}), 404

    best_video, best_audio = pick_adaptive(formats, quality)

    # No adaptive tracks → fall back to muxed stream (no ffmpeg needed)
    if not best_video or not best_audio:
        fmt = pick_muxed(formats, quality)
        if not fmt:
            return jsonify({"error": "No suitable format found"}), 404

        safe_title = re.sub(r"[^\w\s-]", "", title or video_id).strip().replace(" ", "_")
        filename   = f"{safe_title}_{quality}.{fmt.get('ext', 'mp4')}"
        r          = requests.get(fmt["url"], stream=True, timeout=30)

        def gen():
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk

        return Response(
            stream_with_context(gen()),
            headers={
                "Content-Type":        "video/mp4",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    # Guard combined file size
    v_size = best_video.get("filesize") or 0
    a_size = best_audio.get("filesize") or 0
    if v_size + a_size > MAX_FILE_SIZE:
        return jsonify({
            "error": (
                f"Combined size ({(v_size + a_size) // (1024**2)} MB) "
                f"exceeds {MAX_FILE_SIZE // (1024**2)} MB limit"
            )
        }), 413

    # Download raw streams to temp files
    job_id   = uuid.uuid4().hex[:8]
    v_path   = os.path.join(TEMP_DIR, f"{job_id}_video.{best_video.get('ext', 'mp4')}")
    a_path   = os.path.join(TEMP_DIR, f"{job_id}_audio.{best_audio.get('ext', 'm4a')}")
    out_path = os.path.join(TEMP_DIR, f"{job_id}_merged.mp4")

    dl_hdrs = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    def download_stream(url, dest):
        with requests.get(url, headers=dl_hdrs, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=262144):
                    if chunk:
                        fh.write(chunk)

    try:
        download_stream(best_video["url"], v_path)
        download_stream(best_audio["url"], a_path)
    except Exception as e:
        for p in (v_path, a_path):
            try:
                os.remove(p)
            except OSError:
                pass
        return jsonify({"error": f"Download failed: {e}"}), 502

    # Merge with ffmpeg (-c:v copy keeps original video quality, re-encodes audio to AAC)
    cmd = [
        "ffmpeg", "-y",
        "-i", v_path,
        "-i", a_path,
        "-c:v", "copy",
        "-c:a", "aac",
        "-movflags", "+faststart",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=300)

    # Clean up raw streams right away
    for p in (v_path, a_path):
        try:
            os.remove(p)
        except OSError:
            pass

    if result.returncode != 0:
        try:
            os.remove(out_path)
        except OSError:
            pass
        stderr = result.stderr.decode(errors="replace")[-500:]
        return jsonify({"error": "ffmpeg merge failed", "detail": stderr}), 500

    schedule_delete(out_path)  # auto-clean after TEMP_TTL seconds

    safe_title = re.sub(r"[^\w\s-]", "", title or video_id).strip().replace(" ", "_")
    filename   = f"{safe_title}_{quality}_merged.mp4"

    return send_file(
        out_path,
        mimetype="video/mp4",
        as_attachment=True,
        download_name=filename,
    )


# ── Utility routes ────────────────────────────────────────────────────────────
@app.route("/", methods=["GET", "HEAD"])
@app.route("/online", methods=["GET"])
def health():
    return jsonify({
        "status":    "ok",
        "service":   "yt-downloader-api",
        "endpoints": ["/download", "/direct-download", "/merge-download"],
        "qualities": SUPPORTED_Q,
    }), 200

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    return jsonify({"status": "webhook-alive"}), 200


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
