from flask import Flask, request, jsonify, send_file
import yt_dlp
import os
import re
import time
import tempfile
import threading
import unicodedata

app = Flask(__name__)

REQUEST_DELAY = 1.0
COOKIES_FILE  = 'cookies.txt'
TEMP_DIR      = tempfile.gettempdir()
TEMP_TTL      = 300   # delete temp file after 5 minutes

VIDEO_ID_PATTERNS = [
    r'(?:v=|\/)([0-9A-Za-z_-]{11})',
    r'youtu\.be\/([0-9A-Za-z_-]{11})'
]

def extract_video_id(url: str):
    if not url:
        return None
    for p in VIDEO_ID_PATTERNS:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None

def safe_filename(title: str) -> str:
    """Convert title to a safe ASCII filename."""
    # Normalize unicode → ASCII as much as possible
    normalized = unicodedata.normalize('NFKD', title or 'audio')
    ascii_str   = normalized.encode('ascii', 'ignore').decode('ascii')
    # Remove anything that's not alphanumeric, space, dash, dot
    safe        = re.sub(r'[^\w\s\-]', '', ascii_str).strip()
    safe        = re.sub(r'\s+', '_', safe)
    return safe[:100] or 'audio'   # max 100 chars

def schedule_delete(path: str, delay: int = TEMP_TTL):
    """Delete temp file after delay seconds."""
    def _delete():
        time.sleep(delay)
        try:
            os.remove(path)
            app.logger.info(f"Deleted temp file: {path}")
        except OSError:
            pass
    threading.Thread(target=_delete, daemon=True).start()

def base_ydl_opts():
    opts = {
        'quiet':       True,
        'no_warnings': True,
        'http_headers': {
            'User-Agent':      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        },
    }
    if os.path.exists(COOKIES_FILE):
        opts['cookies'] = COOKIES_FILE
    return opts


# ── Download MP3 by name ───────────────────────────────────────────────────────

def download_mp3_by_name(query: str):
    """
    Searches YouTube for query, downloads top result as MP3.
    Returns (file_path, title, error_message)
    """
    # Use a unique temp filename to avoid collisions
    import uuid
    job_id   = uuid.uuid4().hex[:8]
    out_tmpl = os.path.join(TEMP_DIR, f"{job_id}_%(title)s.%(ext)s")

    ydl_opts = base_ydl_opts()
    ydl_opts.update({
        'format':           'bestaudio/best',
        'outtmpl':          out_tmpl,
        'noplaylist':       True,
        'postprocessors':   [{
            'key':            'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        # Search YouTube and take top result
        'default_search':   'ytsearch1',
    })

    try:
        time.sleep(REQUEST_DELAY)
        app.logger.info(f"Downloading MP3 for: {query}")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=True)

        # If search result, unwrap entries
        if 'entries' in info:
            info = info['entries'][0]

        title    = info.get('title', query)
        video_id = info.get('id', job_id)

        # Find the downloaded MP3 file
        mp3_path = None
        for fname in os.listdir(TEMP_DIR):
            if fname.startswith(job_id) and fname.endswith('.mp3'):
                mp3_path = os.path.join(TEMP_DIR, fname)
                break

        if not mp3_path or not os.path.exists(mp3_path):
            return None, title, "MP3 file not found after download"

        return mp3_path, title, None

    except Exception as e:
        msg = str(e)
        app.logger.exception(f"Download error: {e}")
        if any(k in msg for k in ("Sign in", "bot", "LOGIN_REQUIRED")):
            return None, None, "LOGIN_REQUIRED — cookies may be invalid or expired"
        return None, None, msg


# ── Download MP3 by URL ────────────────────────────────────────────────────────

def download_mp3_by_url(youtube_url: str):
    """
    Downloads a specific YouTube URL as MP3.
    Returns (file_path, title, error_message)
    """
    import uuid
    job_id   = uuid.uuid4().hex[:8]
    out_tmpl = os.path.join(TEMP_DIR, f"{job_id}_%(title)s.%(ext)s")

    ydl_opts = base_ydl_opts()
    ydl_opts.update({
        'format':         'bestaudio/best',
        'outtmpl':        out_tmpl,
        'noplaylist':     True,
        'postprocessors': [{
            'key':              'FFmpegExtractAudio',
            'preferredcodec':   'mp3',
            'preferredquality': '192',
        }],
    })

    try:
        time.sleep(REQUEST_DELAY)
        app.logger.info(f"Downloading MP3 for URL: {youtube_url}")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=True)

        title = info.get('title', 'audio')

        mp3_path = None
        for fname in os.listdir(TEMP_DIR):
            if fname.startswith(job_id) and fname.endswith('.mp3'):
                mp3_path = os.path.join(TEMP_DIR, fname)
                break

        if not mp3_path or not os.path.exists(mp3_path):
            return None, title, "MP3 file not found after download"

        return mp3_path, title, None

    except Exception as e:
        msg = str(e)
        if any(k in msg for k in ("Sign in", "bot", "LOGIN_REQUIRED")):
            return None, None, "LOGIN_REQUIRED — cookies may be invalid or expired"
        return None, None, msg


# ── Route: GET /download — search by name and download MP3 ───────────────────
@app.route('/download', methods=['GET'])
def download_by_name():
    """
    Search YouTube by song name and download as MP3 directly to browser.

    GET /download?q=shape+of+you
    GET /download?q=eminem+lose+yourself
    GET /download?q=never+gonna+give+you+up

    The MP3 file downloads automatically in the browser.
    Requires ffmpeg installed on the server.
    """
    query = (request.args.get('q') or request.args.get('name') or '').strip()
    if not query:
        return jsonify({
            'error':    'q param is required',
            'examples': [
                '/download?q=shape+of+you',
                '/download?q=blinding+lights',
                '/download?q=never+gonna+give+you+up',
            ]
        }), 400

    mp3_path, title, err = download_mp3_by_name(query)

    if err:
        return jsonify({'error': err, 'query': query}), 500

    if not mp3_path:
        return jsonify({'error': 'Download failed', 'query': query}), 500

    # Schedule deletion after 5 minutes
    schedule_delete(mp3_path, TEMP_TTL)

    filename = safe_filename(title) + '.mp3'

    return send_file(
        mp3_path,
        mimetype='audio/mpeg',
        as_attachment=True,
        download_name=filename,
    )


# ── Route: GET /download-url — download MP3 from a YouTube URL ───────────────
@app.route('/download-url', methods=['GET'])
def download_by_url():
    """
    Download a specific YouTube video as MP3.

    GET /download-url?url=https://youtu.be/dQw4w9WgXcQ

    The MP3 file downloads automatically in the browser.
    """
    youtube_url = (request.args.get('url') or request.args.get('u') or '').strip()
    if not youtube_url:
        return jsonify({'error': 'url param is required'}), 400
    if not any(d in youtube_url for d in ('youtube.com', 'youtu.be')):
        return jsonify({'error': 'Not a YouTube URL'}), 400

    mp3_path, title, err = download_mp3_by_url(youtube_url)

    if err:
        return jsonify({'error': err, 'url': youtube_url}), 500

    if not mp3_path:
        return jsonify({'error': 'Download failed'}), 500

    schedule_delete(mp3_path, TEMP_TTL)

    filename = safe_filename(title) + '.mp3'

    return send_file(
        mp3_path,
        mimetype='audio/mpeg',
        as_attachment=True,
        download_name=filename,
    )


# ── Route: GET /search — search only, return stream URL (no download) ─────────
@app.route('/search', methods=['GET'])
def search_endpoint():
    """
    Search YouTube by name, return stream URL (no file download).

    GET /search?q=shape+of+you
    GET /search?q=shape+of+you&mode=both
    """
    from flask import abort
    query = (request.args.get('q') or request.args.get('name') or '').strip()
    if not query:
        return jsonify({'error': 'q param is required', 'example': '/search?q=shape+of+you'}), 400

    mode = (request.args.get('mode') or 'audio').strip().lower()
    if mode not in ('audio', 'video', 'both'):
        return jsonify({'error': "mode must be 'audio', 'video', or 'both'"}), 400

    ydl_opts = base_ydl_opts()
    ydl_opts['skip_download'] = True
    ydl_opts['extract_flat']  = False
    ydl_opts['noplaylist']    = True

    def safe_int(v):
        try: return int(v) if v is not None else None
        except: return None

    try:
        time.sleep(REQUEST_DELAY)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)

        if not info or not info.get('entries'):
            return jsonify({'status': 'ok', 'query': query, 'result': None}), 200

        entry = info['entries'][0]
        title    = entry.get('title')
        video_id = entry.get('id')

        formats = []
        for f in entry.get('formats', []):
            if not f.get('url'): continue
            has_video = f.get('vcodec', 'none') != 'none'
            has_audio = f.get('acodec', 'none') != 'none'
            formats.append({
                'url':      f['url'],
                'ext':      f.get('ext', 'unknown'),
                'abr':      safe_int(f.get('tbr') or f.get('abr')),
                'height':   safe_int(f.get('height')),
                'fps':      safe_int(f.get('fps')),
                'has_video': has_video,
                'has_audio': has_audio,
            })

        result = {
            'video_id':  video_id,
            'title':     title,
            'url':       f"https://www.youtube.com/watch?v={video_id}",
            'duration':  entry.get('duration'),
            'channel':   entry.get('uploader') or entry.get('channel'),
            'thumbnail': entry.get('thumbnail'),
            'download_mp3': f"/download-url?url=https://www.youtube.com/watch?v={video_id}",
        }

        if mode in ('audio', 'both'):
            audios = [f for f in formats if f['has_audio'] and not f['has_video']]
            best   = max(audios, key=lambda f: f.get('abr') or 0) if audios else None
            result['audio_stream'] = {
                'stream_url': best['url'] if best else None,
                'ext':        best['ext'] if best else None,
                'abr':        best['abr'] if best else None,
            }

        if mode in ('video', 'both'):
            videos = [f for f in formats if f['has_video'] and not f['has_audio']]
            best   = max(videos, key=lambda f: (f.get('height') or 0)) if videos else None
            result['video_stream'] = {
                'stream_url': best['url']    if best else None,
                'ext':        best['ext']    if best else None,
                'height':     best['height'] if best else None,
            }

        return jsonify({'status': 'ok', 'query': query, 'mode': mode, 'result': result}), 200

    except Exception as e:
        msg = str(e)
        if any(k in msg for k in ("Sign in", "bot", "LOGIN_REQUIRED")):
            return jsonify({'error': 'LOGIN_REQUIRED', 'detail': msg}), 403
        return jsonify({'error': msg}), 500


# ── Route: GET / ──────────────────────────────────────────────────────────────
@app.route('/', methods=['GET', 'HEAD'])
@app.route('/online', methods=['GET'])
def index():
    return jsonify({
        "status":  "ok",
        "service": "yt-mp3-api",
        "version": "4.0",
        "endpoints": {
            "GET /download?q=<song name>":          "🎵 Search by name → download MP3 instantly",
            "GET /download-url?url=<yt_url>":       "🎵 YouTube URL → download MP3 instantly",
            "GET /search?q=<song name>":            "🔍 Search by name → get stream URL (no download)",
            "GET /search?q=<name>&mode=both":       "🔍 Search → get audio + video stream URLs",
        },
        "note": "ffmpeg must be installed on the server for MP3 conversion",
    }), 200


# ── Route: GET /webhook ────────────────────────────────────────────────────────
@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    return jsonify({"status": "webhook-alive"}), 200


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
