from flask import Flask, request, jsonify
import yt_dlp
import os
import re
import time

app = Flask(__name__)
REQUEST_DELAY = 1.0

# Path to your cookies.txt file
COOKIES_FILE = 'cookies.txt'  # Update if needed (e.g., full path on Render)

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

def get_yt_formats_and_meta(youtube_url: str):
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extract_flat': False,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9',
        },
    }

    # Correct option for Netscape cookies.txt
    if os.path.exists(COOKIES_FILE):
        ydl_opts['cookies'] = COOKIES_FILE  # <-- Fixed: 'cookies' not 'cookiefile'
        app.logger.info(f"Loading cookies from {COOKIES_FILE}")
    else:
        app.logger.warning(f"Cookies file not found: {COOKIES_FILE}")

    try:
        time.sleep(REQUEST_DELAY)
        app.logger.info(f"Extracting info for URL: {youtube_url}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)

        if not info:
            return None, None, []

        title = info.get('title')
        video_id = info.get('id')
        formats_raw = info.get('formats', [])

        app.logger.info(f"Found {len(formats_raw)} formats for {title or video_id}")

        formats = []
        for f in formats_raw:
            if not f.get('url'):
                continue

            ext = f.get('ext') or 'unknown'
            mime = f.get('mimeType') or f.get('format', '')
            if '/' in mime:
                ext = mime.split('/')[1].split(';')[0]

            has_video = f.get('vcodec') != 'none' if f.get('vcodec') else (f.get('height') is not None)
            has_audio = f.get('acodec') != 'none' if f.get('acodec') else False

            norm = {
                'itag': f.get('format_id'),
                'url': f.get('url'),
                'ext': ext,
                'mimeType': mime or f.get('format'),
                'qualityLabel': f.get('quality_label') or f.get('resolution'),
                'height': safe_int(f.get('height')),
                'width': safe_int(f.get('width')),
                'fps': safe_int(f.get('fps')),
                'abr': safe_int(f.get('tbr') or f.get('abr')),
                'vbr': safe_int(f.get('tbr')),
                'filesize': safe_int(f.get('filesize')) or safe_int(f.get('filesize_approx')),
                'vcodec': f.get('vcodec', 'unknown'),
                'acodec': f.get('acodec', 'none'),
                'has_video': has_video,
                'has_audio': has_audio,
            }
            formats.append(norm)

        return title, video_id, formats

    except Exception as e:
        app.logger.exception(f"yt-dlp error: {e}")
        error_msg = str(e)
        if "Sign in to confirm" in error_msg or "bot" in error_msg.lower() or "LOGIN_REQUIRED" in error_msg:
            return None, None, [], "LOGIN_REQUIRED", "Sign in to confirm you’re not a bot (cookies may be invalid, expired, or need refresh/proxy)"
        return None, None, [], "ERROR", error_msg

@app.route('/', methods=['GET', 'HEAD'])
@app.route('/online', methods=['GET'])
def formats_endpoint():
    youtube_url = request.args.get('url') or request.args.get('u')

    if not youtube_url:
        return jsonify({"status": "ok", "service": "yt-formats-api (yt-dlp backend with cookies)", "version": "2.2"}), 200

    if not any(domain in youtube_url for domain in ('youtube.com', 'youtu.be')):
        return jsonify({'error': 'url does not look like a YouTube URL'}), 400

    video_id = extract_video_id(youtube_url)
    if not video_id:
        return jsonify({'error': 'could not extract video id from url'}), 400

    # Fixed: Call the function once and handle return properly
    result = get_yt_formats_and_meta(youtube_url)
    if len(result) == 5:
        title, vid_id, formats, err_status, err_reason = result
    else:  # len == 3
        title, vid_id, formats = result
        err_status = err_reason = None

    if err_status:
        return jsonify({
            'error': 'failed to extract formats',
            'video_id': vid_id or video_id,
            'requested_url': youtube_url,
            'playability_status': err_status,
            'playability_reason': err_reason,
            'note': 'yt-dlp restriction encountered. Check cookies validity, update yt-dlp, or add a proxy.'
        }), 500 if err_status == "ERROR" else 403

    if not formats:
        return jsonify({
            'error': 'no formats found for this video',
            'video_id': vid_id or video_id,
            'title': title,
            'requested_url': youtube_url,
            'note': 'Video may be unavailable or restricted.'
        }), 404

    # Categorize and sort
    muxed = [f for f in formats if f['has_video'] and f['has_audio']]
    videos = [f for f in formats if f['has_video'] and not f['has_audio']]
    audios = [f for f in formats if f['has_audio'] and not f['has_video']]

    muxed.sort(key=lambda e: (e.get('height') or 0, e.get('fps') or 0, e.get('filesize') or 0), reverse=True)
    videos.sort(key=lambda e: (e.get('height') or 0, e.get('fps') or 0, e.get('filesize') or 0), reverse=True)
    audios.sort(key=lambda e: (e.get('abr') or 0, e.get('filesize') or 0), reverse=True)

    def build_entry(f):
        return {
            'itag': f['itag'],
            'ext': f['ext'],
            'mimeType': f['mimeType'],
            'qualityLabel': f['qualityLabel'],
            'height': f['height'],
            'width': f['width'],
            'fps': f['fps'],
            'vcodec': f['vcodec'],
            'acodec': f['acodec'],
            'abr': f['abr'],
            'vbr': f['vbr'],
            'filesize': f['filesize'],
            'url': f['url'],
        }

    cookies_note = f'Using cookies from {COOKIES_FILE}' if os.path.exists(COOKIES_FILE) else 'No cookies file - running anonymously'

    return jsonify({
        'status': 'ok',
        'video_id': vid_id or video_id,
        'title': title,
        'requested_url': youtube_url,
        'muxed_formats': [build_entry(f) for f in muxed],
        'video_formats': [build_entry(f) for f in videos],
        'audio_formats': [build_entry(f) for f in audios],
        'total_formats': len(formats),
        'note': cookies_note,
    }), 200

@app.route('/webhook', methods=['GET', 'POST'])
def webhook():
    return jsonify({"status": "webhook-alive"}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
