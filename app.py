import re
import json
from flask import Flask, request, jsonify
from flask_cors import CORS
from curl_cffi import requests as cffi_requests
from urllib.parse import urljoin
import m3u8

app = Flask(__name__)
CORS(app) # Allows requests from your PHP/JS frontends

def extract_m3u8(page_url):
    """Fetches the page and extracts the raw m3u8 stream link."""
    try:
        resp = cffi_requests.get(page_url, impersonate="chrome120", timeout=15)
        html = resp.tex
        stream_url = None
        data = {}

        # Method 1: Extract from window.initials JSON object
        json_match = re.search(r'window\.initials\s*=\s*({.+?});\s*</script>', html, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        if data:
            sources = data.get("videoModel", {}).get("sources", {})
            if "hls" in sources:
                stream_url = sources["hls"].get("url")

        # Method 2: Fallback Regex for direct m3u8 links in HTML
        if not stream_url:
            m3u8_matches = re.findall(r'https?:\/\/[^\s<>"\'\\]+\.m3u8[^\s<>"\'\\]*', html)
            if m3u8_matches:
                valid = [m.replace('\\/', '/') for m in m3u8_matches if 'tsyndicate' not in m]
                if valid:
                    stream_url = valid[0]

        return stream_url
    except Exception as e:
        print(f"Error during extraction: {str(e)}")
        return None

def parse_qualities(m3u8_url):
    """Fetches the master m3u8 and extracts different quality URLs."""
    try:
        resp = cffi_requests.get(m3u8_url, impersonate="chrome120", timeout=15)
        playlist = m3u8.loads(resp.text)
        
        qualities = {}
        # Check if it's a master playlist with multiple qualities
        if playlist.is_variant:
            for p in playlist.playlists:
                # Determine resolution label (e.g., "1080p", "720p")
                if p.stream_info.resolution:
                    res_label = f"{p.stream_info.resolution[1]}p"
                else:
                    res_label = "unknown"
                
                # Ensure the URL is absolute
                stream_uri = p.uri if p.uri.startswith('http') else urljoin(m3u8_url, p.uri)
                qualities[res_label] = stream_uri
                
        return qualities
    except Exception as e:
        print(f"Error parsing qualities: {str(e)}")
        return {}

@app.route('/api/extract', methods=['GET', 'POST'])
def extract_video():
    # Handle both GET (query parameters) and POST (JSON body)
    if request.method == 'POST':
        data = request.get_json()
        target_url = data.get('url') if data else None
    else:
        target_url = request.args.get('url')

    if not target_url:
        return jsonify({"success": False, "error": "No URL provided."}), 400

    # Extract the master URL
    master_url = extract_m3u8(target_url)

    if not master_url:
        return jsonify({"success": False, "error": "Could not extract a valid .m3u8 link."}), 404

    # Parse out the specific qualities
    qualities = parse_qualities(master_url)

    return jsonify({
        "success": True,
        "master_url": master_url,
        "qualities": qualities
    }), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
