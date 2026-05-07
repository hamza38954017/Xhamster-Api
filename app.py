
import re
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
from curl_cffi import requests as cffi_requests

app = Flask(__name__)
CORS(app)

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def _clean_url(url: str) -> str:
    return url.replace('\\/', '/').replace('\\u0026', '&')

def _detect_sub_format(url: str) -> str:
    """Handles versioned URLs like .vtt.v1733255317"""
    path = url.lower().split("?")[0]
    if re.search(r'\.vtt(\.|$|/)', path):
        return "vtt"
    if re.search(r'\.srt(\.|$|/)', path):
        return "srt"
    if re.search(r'\.(ass|ssa)(\.|$|/)', path):
        return "ass"
    if path.endswith(".html"):
        return "html"
    return "unknown"

# ─────────────────────────────────────────────
#  SUBTITLE EXTRACTION
# ─────────────────────────────────────────────
def extract_subtitles(html: str, data: dict) -> list[dict]:
    subtitles = []
    seen = set()

    def add(label, language, url, fmt):
        url = _clean_url(url)
        if url and url not in seen:
            seen.add(url)
            subtitles.append({
                "label": label, 
                "language": language,
                "url": url, 
                "format": fmt
            })

    # ── Method 1: xplayerPluginSettings.subtitles.tracks ───────────────────
    if data:
        xplayer_subs = (
            data.get("xplayerPluginSettings", {})
            .get("subtitles", {})
            .get("tracks", [])
        )
        for track in xplayer_subs:
            label = track.get("label", "Subtitle")
            language = track.get("lang", track.get("language", "und"))
            urls = track.get("urls", {})

            for fmt in ("vtt", "srt", "ass"):
                url = urls.get(fmt, "")
                if url:
                    add(label, language, url, fmt)
                    break
            else:
                for fmt, url in urls.items():
                    if url:
                        add(label, language, url, _detect_sub_format(url))

    # ── Method 2: videoModel.tracks / textTracks ───────────────────────────
    if data:
        vm = data.get("videoModel", {})

        def _iter_tracks(v):
            if isinstance(v, list):
                yield from v
            elif isinstance(v, dict):
                for k in ("subtitles", "captions", "closedCaptions", "text"):
                    yield from v.get(k, [])

        for track in _iter_tracks(vm.get("tracks")):
            url = track.get("file") or track.get("src") or track.get("url", "")
            if url:
                add(track.get("label", "Subtitle"),
                    track.get("language", track.get("lang", "und")),
                    url, _detect_sub_format(url))

        for track in vm.get("textTracks", []):
            url = track.get("src") or track.get("url", "")
            if url:
                add(track.get("label", "Subtitle"),
                    track.get("srclang", track.get("language", "und")),
                    url, _detect_sub_format(url))

    # ── Method 3: top-level captions / subtitles keys ──────────────────────
    if data:
        for key in ("captions", "subtitles", "closedCaptions"):
            for item in data.get(key, []):
                url = item.get("url") or item.get("src") or item.get("file", "")
                if url:
                    add(item.get("label", key),
                        item.get("language", item.get("lang", "und")),
                        url, _detect_sub_format(url))

    # ── Method 4: regex sweep ──────────────────────────────────────────────
    for url in re.findall(r'https?://[^\s<>"\'\\]+\.vtt(?:\.[^\s<>"\'\\]*)?', html, re.I):
        add("VTT Subtitle", "und", url, "vtt")
    for url in re.findall(r'https?:\\/\\/[^\s<>"\'\\]+\.vtt(?:\.[^\s<>"\'\\]*)?', html, re.I):
        add("VTT Subtitle", "und", url, "vtt")
    for url in re.findall(r'https?://[^\s<>"\'\\]+\.srt(?:\.[^\s<>"\'\\]*)?', html, re.I):
        add("SRT Subtitle", "und", url, "srt")

    return subtitles

# ─────────────────────────────────────────────
#  M3U8 EXTRACTION
# ─────────────────────────────────────────────
def extract_m3u8(page_url: str, user_ip: str = None) -> tuple[str | None, list[dict], str]:
    try:
        headers = {}
        # If we received a user IP from the PHP proxy, spoof it in the headers
        if user_ip:
            headers['X-Forwarded-For'] = user_ip
            headers['Client-IP'] = user_ip

        # Pass the headers to curl_cffi
        resp = cffi_requests.get(page_url, impersonate="chrome120", timeout=20, headers=headers)
        html = resp.text

        data = {}
        m = re.search(r'window\.initials\s*=\s*(\{.+?\});\s*</script>', html, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        stream_url = None

        if data:
            sources = data.get("videoModel", {}).get("sources", {})
            if isinstance(sources, dict):
                stream_url = _clean_url(sources.get("hls", {}).get("url", "") or "")

        if not stream_url:
            matches = [_clean_url(u) for u in
                       re.findall(r'https?://[^\s<>"\'\\]+\.m3u8[^\s<>"\'\\]*', html)
                       if 'tsyndicate' not in u]
            if matches:
                stream_url = matches[0]

        if not stream_url:
            escaped = re.findall(r'https?:\\/\\/[^\s<>"\'\\]+\.m3u8[^\s<>"\']*', html)
            if escaped:
                stream_url = _clean_url(escaped[0])

        subtitles = extract_subtitles(html, data)
        return stream_url, subtitles, html

    except Exception as e:
        import traceback
        traceback.print_exc()
        return None, [], ""

# ─────────────────────────────────────────────
#  API ROUTES
# ─────────────────────────────────────────────
@app.route('/', methods=['GET'])
def home():
    """Root endpoint to verify the API is online."""
    return jsonify({
        "status": "online", 
        "message": "Send GET or POST requests with a 'url' parameter to /api/extract"
    }), 200

@app.route('/api/extract', methods=['GET', 'POST'])
def extract_endpoint():
    """Main extraction endpoint."""
    user_ip = None
    if request.method == 'POST':
        payload = request.get_json()
        target_url = payload.get('url') if payload else None
        user_ip = payload.get('user_ip') if payload else None
    else:
        target_url = request.args.get('url')
        user_ip = request.args.get('user_ip')

    # Fallback to the direct requester's IP if one wasn't passed in the payload
    if not user_ip:
        user_ip = request.headers.get('X-Forwarded-For', request.remote_addr)

    if not target_url:
        return jsonify({"success": False, "error": "No URL provided."}), 400

    # Pass the user_ip into the extraction function
    stream_url, subtitles, _ = extract_m3u8(target_url, user_ip)

    if not stream_url and not subtitles:
        return jsonify({
            "success": False, 
            "error": "Could not extract stream or subtitles."
        }), 404

    return jsonify({
        "success": True,
        "stream_url": stream_url,
        "subtitles": subtitles,
        "subtitle_count": len(subtitles)
    }), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
