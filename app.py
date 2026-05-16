from flask import Flask, request, jsonify
import yt_dlp
import os
import re
import time

app = Flask(__name__)
REQUEST_DELAY = 1.0

COOKIES_FILE = 'cookies.txt'

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

def safe_int(v):
    try:
        if v is None:
            return None
        if isinstance(v, int):
            return v
        return int(v)
    except Exception:
        return None

def base_ydl_opts():
    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extract_flat': False,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        },
    }
    if os.path.exists(COOKIES_FILE):
        opts['cookies'] = COOKIES_FILE
    return opts


# ── Pick best audio from formats list ─────────────────────────────────────────

def pick_best_audio(formats: list):
    audios = [f for f in formats if f['has_audio'] and not f['has_video']]
    if not audios:
        # fallback: muxed
        audios = [f for f in formats if f['has_audio']]
    if not audios:
        return None
    return max(audios, key=lambda f: (f.get('abr') or 0))

def pick_best_video(formats: list):
    videos = [f for f in formats if f['has_video'] and not f['has_audio']]
    if not videos:
        videos = [f for f in formats if f['has_video']]
    if not videos:
        return None
    return max(videos, key=lambda f: (f.get('height') or 0, f.get('fps') or 0))


# ── Core: search by name AND return stream URL in one shot ────────────────────

def search_and_get_stream(query: str, mode: str = 'audio'):
    """
    Searches YouTube by name and immediately fetches stream URLs for the top result.
    mode: 'audio' | 'video' | 'both'
    Returns a dict with all info + stream urls.
    """
    ydl_opts = base_ydl_opts()
    # Do NOT use extract_flat — we need full format info in one call
    ydl_opts['extract_flat'] = False
    ydl_opts['noplaylist']   = True

    search_url = f"ytsearch1:{query}"   # grab only top 1 result with full info

    try:
        time.sleep(REQUEST_DELAY)
        app.logger.info(f"Search+stream for: {query} [{mode}]")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_url, download=False)

        if not info or not info.get('entries'):
            return None, "No results found"

        entry = info['entries'][0]
        if not entry:
            return None, "Empty result"

        title    = entry.get('title')
        video_id = entry.get('id')
        yt_url   = f"https://www.youtube.com/watch?v={video_id}"
        duration = entry.get('duration')
        channel  = entry.get('uploader') or entry.get('channel')
        thumbnail= entry.get('thumbnail')

        # Parse formats
        formats = []
        for f in entry.get('formats', []):
            if not f.get('url'):
                continue
            ext  = f.get('ext') or 'unknown'
            mime = f.get('mimeType') or f.get('format', '')
            if '/' in mime:
                ext = mime.split('/')[1].split(';')[0]
            has_video = f.get('vcodec', 'none') != 'none'
            has_audio = f.get('acodec', 'none') != 'none'
            formats.append({
                'url':      f.get('url'),
                'ext':      ext,
                'height':   safe_int(f.get('height')),
                'fps':      safe_int(f.get('fps')),
                'abr':      safe_int(f.get('tbr') or f.get('abr')),
                'filesize': safe_int(f.get('filesize') or f.get('filesize_approx')),
                'vcodec':   f.get('vcodec', 'unknown'),
                'acodec':   f.get('acodec', 'none'),
                'has_video': has_video,
                'has_audio': has_audio,
            })

        result = {
            'video_id':  video_id,
            'title':     title,
            'url':       yt_url,
            'duration':  duration,
            'channel':   channel,
            'thumbnail': thumbnail,
        }

        if mode in ('audio', 'both'):
            best_audio = pick_best_audio(formats)
            result['audio_stream'] = {
                'stream_url': best_audio['url']      if best_audio else None,
                'ext':        best_audio['ext']      if best_audio else None,
                'abr':        best_audio['abr']      if best_audio else None,
                'filesize':   best_audio['filesize'] if best_audio else None,
            }

        if mode in ('video', 'both'):
            best_video = pick_best_video(formats)
            result['video_stream'] = {
                'stream_url': best_video['url']      if best_video else None,
                'ext':        best_video['ext']      if best_video else None,
                'height':     best_video['height']   if best_video else None,
                'fps':        best_video['fps']      if best_video else None,
                'filesize':   best_video['filesize'] if best_video else None,
            }

        return result, None

    except Exception as e:
        app.logger.exception(f"search_and_get_stream error: {e}")
        msg = str(e)
        if any(k in msg for k in ("Sign in", "bot", "LOGIN_REQUIRED")):
            return None, "LOGIN_REQUIRED — cookies may be invalid or expired"
        return None, msg


# ── Core: get stream by URL ───────────────────────────────────────────────────

def get_stream_by_url(youtube_url: str, mode: str = 'audio'):
    ydl_opts = base_ydl_opts()

    try:
        time.sleep(REQUEST_DELAY)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)

        if not info:
            return None, "No info returned"

        title    = info.get('title')
        video_id = info.get('id')
        channel  = info.get('uploader') or info.get('channel')
        thumbnail= info.get('thumbnail')
        duration = info.get('duration')

        formats = []
        for f in info.get('formats', []):
            if not f.get('url'):
                continue
            ext  = f.get('ext') or 'unknown'
            mime = f.get('mimeType') or f.get('format', '')
            if '/' in mime:
                ext = mime.split('/')[1].split(';')[0]
            has_video = f.get('vcodec', 'none') != 'none'
            has_audio = f.get('acodec', 'none') != 'none'
            formats.append({
                'url':      f.get('url'),
                'ext':      ext,
                'height':   safe_int(f.get('height')),
                'fps':      safe_int(f.get('fps')),
                'abr':      safe_int(f.get('tbr') or f.get('abr')),
                'filesize': safe_int(f.get('filesize') or f.get('filesize_approx')),
                'vcodec':   f.get('vcodec', 'unknown'),
                'acodec':   f.get('acodec', 'none'),
                'has_video': has_video,
                'has_audio': has_audio,
            })

        result = {
            'video_id':  video_id,
            'title':     title,
            'url':       youtube_url,
            'duration':  duration,
            'channel':   channel,
            'thumbnail': thumbnail,
        }

        if mode in ('audio', 'both'):
            best_audio = pick_best_audio(formats)
            result['audio_stream'] = {
                'stream_url': best_audio['url']      if best_audio else None,
                'ext':        best_audio['ext']      if best_audio else None,
                'abr':        best_audio['abr']      if best_audio else None,
                'filesize':   best_audio['filesize'] if best_audio else None,
            }

        if mode in ('video', 'both'):
            best_video = pick_best_video(formats)
            result['video_stream'] = {
                'stream_url': best_video['url']      if best_video else None,
                'ext':        best_video['ext']      if best_video else None,
                'height':     best_video['height']   if best_video else None,
                'fps':        best_video['fps']      if best_video else None,
                'filesize':   best_video['filesize'] if best_video else None,
            }

        return result, None

    except Exception as e:
        msg = str(e)
        if any(k in msg for k in ("Sign in", "bot", "LOGIN_REQUIRED")):
            return None, "LOGIN_REQUIRED — cookies may be invalid or expired"
        return None, msg


# ── Route: GET /search — search by name, get stream URL directly ──────────────
@app.route('/search', methods=['GET'])
def search_endpoint():
    """
    Search YouTube by song/video name and get stream URL directly.

    GET /search?q=shape+of+you                  → audio stream (default)
    GET /search?q=shape+of+you&mode=video       → video stream
    GET /search?q=shape+of+you&mode=both        → audio + video streams

    Response includes stream_url ready to plug into FFmpeg or music bot.
    """
    query = (request.args.get('q') or request.args.get('name') or '').strip()
    if not query:
        return jsonify({
            'error':    'q param is required',
            'examples': [
                '/search?q=shape+of+you',
                '/search?q=blinding+lights&mode=audio',
                '/search?q=never+gonna+give+you+up&mode=both',
            ]
        }), 400

    mode = (request.args.get('mode') or 'audio').strip().lower()
    if mode not in ('audio', 'video', 'both'):
        return jsonify({'error': "mode must be 'audio', 'video', or 'both'"}), 400

    result, err = search_and_get_stream(query, mode=mode)

    if err:
        return jsonify({'error': err, 'query': query}), 500

    if not result:
        return jsonify({'status': 'ok', 'query': query, 'result': None, 'note': 'No results found'}), 200

    return jsonify({
        'status': 'ok',
        'query':  query,
        'mode':   mode,
        'result': result,
    }), 200


# ── Route: GET /stream — get stream URL by YouTube URL ───────────────────────
@app.route('/stream', methods=['GET'])
def stream_endpoint():
    """
    Get stream URL directly from a YouTube URL.

    GET /stream?url=https://youtu.be/dQw4w9WgXcQ
    GET /stream?url=https://youtu.be/dQw4w9WgXcQ&mode=video
    GET /stream?url=https://youtu.be/dQw4w9WgXcQ&mode=both

    Perfect for music bots — one call, fresh stream URL, ready to play.
    """
    youtube_url = (request.args.get('url') or request.args.get('u') or '').strip()
    if not youtube_url:
        return jsonify({'error': 'url param is required'}), 400
    if not any(d in youtube_url for d in ('youtube.com', 'youtu.be')):
        return jsonify({'error': 'Not a YouTube URL'}), 400

    mode = (request.args.get('mode') or 'audio').strip().lower()
    if mode not in ('audio', 'video', 'both'):
        return jsonify({'error': "mode must be 'audio', 'video', or 'both'"}), 400

    result, err = get_stream_by_url(youtube_url, mode=mode)
    if err:
        return jsonify({'error': err, 'url': youtube_url}), 500

    return jsonify({
        'status': 'ok',
        'mode':   mode,
        'result': result,
    }), 200


# ── Route: GET / — formats by URL (original endpoint kept) ───────────────────
@app.route('/', methods=['GET', 'HEAD'])
@app.route('/online', methods=['GET'])
def formats_endpoint():
    youtube_url = (request.args.get('url') or request.args.get('u') or '').strip()

    if not youtube_url:
        return jsonify({
            "status":  "ok",
            "service": "yt-stream-api (yt-dlp)",
            "version": "3.0",
            "endpoints": {
                "GET /search?q=<name>":              "Search by name → get stream URL directly (default: audio)",
                "GET /search?q=<name>&mode=video":   "Search by name → get video stream URL",
                "GET /search?q=<name>&mode=both":    "Search by name → get audio + video stream URLs",
                "GET /stream?url=<yt_url>":          "Get stream URL from YouTube URL (default: audio)",
                "GET /stream?url=<yt_url>&mode=both":"Get audio + video stream URLs from YouTube URL",
                "GET /?url=<yt_url>":                "Get ALL formats (raw)",
            }
        }), 200

    if not any(domain in youtube_url for domain in ('youtube.com', 'youtu.be')):
        return jsonify({'error': 'url does not look like a YouTube URL'}), 400

    video_id = extract_video_id(youtube_url)
    if not video_id:
        return jsonify({'error': 'could not extract video id from url'}), 400

    ydl_opts = base_ydl_opts()
    try:
        time.sleep(REQUEST_DELAY)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)

        if not info:
            return jsonify({'error': 'No info returned'}), 500

        title       = info.get('title')
        vid_id      = info.get('id')
        formats_raw = info.get('formats', [])

        formats = []
        for f in formats_raw:
            if not f.get('url'):
                continue
            ext  = f.get('ext') or 'unknown'
            mime = f.get('mimeType') or f.get('format', '')
            if '/' in mime:
                ext = mime.split('/')[1].split(';')[0]
            has_video = f.get('vcodec', 'none') != 'none'
            has_audio = f.get('acodec', 'none') != 'none'
            formats.append({
                'itag':         f.get('format_id'),
                'url':          f.get('url'),
                'ext':          ext,
                'mimeType':     mime or f.get('format'),
                'qualityLabel': f.get('quality_label') or f.get('resolution'),
                'height':       safe_int(f.get('height')),
                'width':        safe_int(f.get('width')),
                'fps':          safe_int(f.get('fps')),
                'abr':          safe_int(f.get('tbr') or f.get('abr')),
                'vbr':          safe_int(f.get('tbr')),
                'filesize':     safe_int(f.get('filesize') or f.get('filesize_approx')),
                'vcodec':       f.get('vcodec', 'unknown'),
                'acodec':       f.get('acodec', 'none'),
                'has_video':    has_video,
                'has_audio':    has_audio,
            })

        muxed  = sorted([f for f in formats if f['has_video'] and f['has_audio']],
                        key=lambda e: (e.get('height') or 0, e.get('fps') or 0), reverse=True)
        videos = sorted([f for f in formats if f['has_video'] and not f['has_audio']],
                        key=lambda e: (e.get('height') or 0, e.get('fps') or 0), reverse=True)
        audios = sorted([f for f in formats if f['has_audio'] and not f['has_video']],
                        key=lambda e: (e.get('abr') or 0), reverse=True)

        def build_entry(f):
            return {k: f[k] for k in
                    ('itag','ext','mimeType','qualityLabel','height','width',
                     'fps','vcodec','acodec','abr','vbr','filesize','url')}

        return jsonify({
            'status':        'ok',
            'video_id':      vid_id or video_id,
            'title':         title,
            'requested_url': youtube_url,
            'muxed_formats': [build_entry(f) for f in muxed],
            'video_formats': [build_entry(f) for f in videos],
            'audio_formats': [build_entry(f) for f in audios],
            'total_formats': len(formats),
        }), 200

    except Exception as e:
        msg = str(e)
        if any(k in msg for k in ("Sign in", "bot", "LOGIN_REQUIRED")):
            return jsonify({'error': 'LOGIN_REQUIRED', 'detail': msg}), 403
        return jsonify({'error': msg}), 500


# ── Route: GET /webhook ────────────────────────────────────────────────────────
@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    return jsonify({"status": "webhook-alive"}), 200


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
