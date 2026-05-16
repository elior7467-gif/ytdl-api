from flask import Flask, request, jsonify
import yt_dlp
import os
import re
import time

app = Flask(__name__)
REQUEST_DELAY = 1.0

# Path to your cookies.txt file
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
        app.logger.info(f"Loading cookies from {COOKIES_FILE}")
    else:
        app.logger.warning(f"Cookies file not found: {COOKIES_FILE}")
    return opts


# ── Search by name ─────────────────────────────────────────────────────────────

def search_youtube_by_name(query: str, max_results: int = 5):
    """
    Uses yt-dlp ytsearch to find videos by name/title.
    Returns a list of result dicts.
    """
    ydl_opts = base_ydl_opts()
    ydl_opts['extract_flat'] = True   # fast — only metadata, no format fetch

    search_url = f"ytsearch{max_results}:{query}"

    try:
        time.sleep(REQUEST_DELAY)
        app.logger.info(f"Searching YouTube for: {query}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_url, download=False)

        if not info or 'entries' not in info:
            return [], None

        results = []
        for entry in info['entries']:
            if not entry:
                continue
            vid_id = entry.get('id') or ''
            results.append({
                'video_id':   vid_id,
                'title':      entry.get('title'),
                'url':        f"https://www.youtube.com/watch?v={vid_id}",
                'duration':   entry.get('duration'),
                'channel':    entry.get('uploader') or entry.get('channel'),
                'view_count': entry.get('view_count'),
                'thumbnail':  entry.get('thumbnail'),
            })

        return results, None

    except Exception as e:
        app.logger.exception(f"Search error: {e}")
        return [], str(e)


# ── Formats fetch ──────────────────────────────────────────────────────────────

def get_yt_formats_and_meta(youtube_url: str):
    ydl_opts = base_ydl_opts()

    try:
        time.sleep(REQUEST_DELAY)
        app.logger.info(f"Extracting info for URL: {youtube_url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)

        if not info:
            return None, None, []

        title       = info.get('title')
        video_id    = info.get('id')
        formats_raw = info.get('formats', [])

        app.logger.info(f"Found {len(formats_raw)} formats for {title or video_id}")

        formats = []
        for f in formats_raw:
            if not f.get('url'):
                continue

            ext  = f.get('ext') or 'unknown'
            mime = f.get('mimeType') or f.get('format', '')
            if '/' in mime:
                ext = mime.split('/')[1].split(';')[0]

            has_video = f.get('vcodec') != 'none' if f.get('vcodec') else (f.get('height') is not None)
            has_audio = f.get('acodec') != 'none' if f.get('acodec') else False

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
                'filesize':     safe_int(f.get('filesize')) or safe_int(f.get('filesize_approx')),
                'vcodec':       f.get('vcodec', 'unknown'),
                'acodec':       f.get('acodec', 'none'),
                'has_video':    has_video,
                'has_audio':    has_audio,
            })

        return title, video_id, formats

    except Exception as e:
        app.logger.exception(f"yt-dlp error: {e}")
        error_msg = str(e)
        if any(k in error_msg for k in ("Sign in to confirm", "bot", "LOGIN_REQUIRED")):
            return None, None, [], "LOGIN_REQUIRED", "Sign in required — cookies may be invalid/expired"
        return None, None, [], "ERROR", error_msg


# ── Route: GET / — formats by URL ─────────────────────────────────────────────
@app.route('/', methods=['GET', 'HEAD'])
@app.route('/online', methods=['GET'])
def formats_endpoint():
    youtube_url = (request.args.get('url') or request.args.get('u') or '').strip()

    if not youtube_url:
        return jsonify({
            "status":  "ok",
            "service": "yt-formats-api (yt-dlp + name search)",
            "version": "2.3",
            "endpoints": {
                "GET /?url=<yt_url>":        "Get all formats for a YouTube URL",
                "GET /search?q=<song name>": "Search YouTube by song/video name",
                "GET /search?q=<name>&n=10": "Search with custom result count (default 5, max 15)",
            }
        }), 200

    if not any(domain in youtube_url for domain in ('youtube.com', 'youtu.be')):
        return jsonify({'error': 'url does not look like a YouTube URL'}), 400

    video_id = extract_video_id(youtube_url)
    if not video_id:
        return jsonify({'error': 'could not extract video id from url'}), 400

    result = get_yt_formats_and_meta(youtube_url)
    if len(result) == 5:
        title, vid_id, formats, err_status, err_reason = result
    else:
        title, vid_id, formats = result
        err_status = err_reason = None

    if err_status:
        return jsonify({
            'error':              'failed to extract formats',
            'video_id':           vid_id or video_id,
            'requested_url':      youtube_url,
            'playability_status': err_status,
            'playability_reason': err_reason,
            'note':               'yt-dlp restriction encountered. Check cookies validity, update yt-dlp, or add a proxy.',
        }), 500 if err_status == "ERROR" else 403

    if not formats:
        return jsonify({
            'error':         'no formats found for this video',
            'video_id':      vid_id or video_id,
            'title':         title,
            'requested_url': youtube_url,
            'note':          'Video may be unavailable or restricted.',
        }), 404

    muxed  = [f for f in formats if f['has_video'] and f['has_audio']]
    videos = [f for f in formats if f['has_video'] and not f['has_audio']]
    audios = [f for f in formats if f['has_audio'] and not f['has_video']]

    muxed.sort( key=lambda e: (e.get('height') or 0, e.get('fps') or 0, e.get('filesize') or 0), reverse=True)
    videos.sort(key=lambda e: (e.get('height') or 0, e.get('fps') or 0, e.get('filesize') or 0), reverse=True)
    audios.sort(key=lambda e: (e.get('abr') or 0, e.get('filesize') or 0), reverse=True)

    def build_entry(f):
        return {k: f[k] for k in
                ('itag','ext','mimeType','qualityLabel','height','width',
                 'fps','vcodec','acodec','abr','vbr','filesize','url')}

    cookies_note = (f'Using cookies from {COOKIES_FILE}'
                    if os.path.exists(COOKIES_FILE) else 'No cookies file — running anonymously')

    return jsonify({
        'status':        'ok',
        'video_id':      vid_id or video_id,
        'title':         title,
        'requested_url': youtube_url,
        'muxed_formats': [build_entry(f) for f in muxed],
        'video_formats': [build_entry(f) for f in videos],
        'audio_formats': [build_entry(f) for f in audios],
        'total_formats': len(formats),
        'note':          cookies_note,
    }), 200


# ── Route: GET /search — search by song/video name ────────────────────────────
@app.route('/search', methods=['GET'])
def search_endpoint():
    """
    Search YouTube by name/title using yt-dlp ytsearch.

    GET /search?q=shape+of+you
    GET /search?q=blinding+lights&n=10
    GET /search?q=eminem+lose+yourself&n=3

    Returns top N results with video_id, title, url, duration, channel, thumbnail.
    Then pass the url to /?url=... to get download formats.
    """
    query = (request.args.get('q') or request.args.get('name') or '').strip()
    if not query:
        return jsonify({
            'error':   'q param is required',
            'example': '/search?q=shape+of+you',
        }), 400

    try:
        n = min(int(request.args.get('n', 5)), 15)
    except ValueError:
        n = 5

    results, err = search_youtube_by_name(query, max_results=n)

    if err:
        return jsonify({
            'error':  'Search failed',
            'detail': err,
            'note':   'Check cookies or try again.',
        }), 500

    if not results:
        return jsonify({
            'status':  'ok',
            'query':   query,
            'results': [],
            'note':    'No results found.',
        }), 200

    return jsonify({
        'status':  'ok',
        'query':   query,
        'count':   len(results),
        'results': results,
        'tip':     'Use the url field from any result with /?url=... to get download formats',
    }), 200


# ── Route: GET /webhook ────────────────────────────────────────────────────────
@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    return jsonify({"status": "webhook-alive"}), 200


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
