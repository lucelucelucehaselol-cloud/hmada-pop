from flask import Flask, render_template, request, jsonify, send_file, after_this_request
import yt_dlp
import os
import uuid
import threading
import time

app = Flask(__name__)

DOWNLOAD_FOLDER = "/tmp/downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

download_status = {}


def cleanup_old_files():
    while True:
        try:
            now = time.time()
            for fname in os.listdir(DOWNLOAD_FOLDER):
                fpath = os.path.join(DOWNLOAD_FOLDER, fname)
                if os.path.isfile(fpath):
                    if now - os.path.getmtime(fpath) > 600:
                        os.remove(fpath)
        except Exception:
            pass
        time.sleep(120)


threading.Thread(target=cleanup_old_files, daemon=True).start()


COMMON_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    },
    "extractor_args": {
        "youtube": {"player_client": ["web", "android"]}
    },
    "socket_timeout": 30,
    "retries": 5,
}

# لو عندك cookies.txt ارفعه في نفس الفولدر
COOKIES_FILE = "cookies.txt"
if os.path.exists(COOKIES_FILE):
    COMMON_OPTS["cookiefile"] = COOKIES_FILE


def do_download(task_id, url, format_type):
    try:
        download_status[task_id] = {
            "status": "downloading",
            "progress": 0,
            "title": "",
            "error": ""
        }

        def progress_hook(d):
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate", 1)
                downloaded = d.get("downloaded_bytes", 0)
                pct = int(downloaded / total * 100) if total else 0
                download_status[task_id]["progress"] = pct
            elif d["status"] == "finished":
                download_status[task_id]["progress"] = 99

        output_path = os.path.join(DOWNLOAD_FOLDER, task_id)

        if format_type == "audio":
            ydl_opts = {
                **COMMON_OPTS,
                "format": "bestaudio/best",
                "outtmpl": output_path + ".%(ext)s",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }],
                "progress_hooks": [progress_hook],
            }
            final_ext = "mp3"
        else:
            ydl_opts = {
                **COMMON_OPTS,
                "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
                "outtmpl": output_path + ".%(ext)s",
                "merge_output_format": "mp4",
                "progress_hooks": [progress_hook],
            }
            final_ext = "mp4"

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "video")
            download_status[task_id]["title"] = title

        # دور على الملف
        file_path = output_path + f".{final_ext}"
        if not os.path.exists(file_path):
            for f in os.listdir(DOWNLOAD_FOLDER):
                if f.startswith(task_id):
                    file_path = os.path.join(DOWNLOAD_FOLDER, f)
                    final_ext = f.rsplit(".", 1)[-1]
                    break

        if not os.path.exists(file_path):
            raise FileNotFoundError("الملف ما اتحملش صح")

        download_status[task_id].update({
            "status": "done",
            "progress": 100,
            "file_path": file_path,
            "ext": final_ext,
        })

    except Exception as e:
        err = str(e)
        if "Sign in" in err or "bot" in err.lower():
            err = "يوتيوب بيبلوك السيرفر. جرب تضيف cookies.txt أو استنى وجرب تاني."
        elif "Video unavailable" in err:
            err = "الفيديو مش متاح أو محذوف."
        elif "Private video" in err:
            err = "الفيديو خاص."
        elif "429" in err:
            err = "يوتيوب عامل Rate Limit. استنى 5 دقايق وجرب تاني."
        download_status[task_id]["status"] = "error"
        download_status[task_id]["error"] = err


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start_download():
    data = request.get_json()
    url = (data.get("url") or "").strip()
    format_type = data.get("format", "video")

    if not url:
        return jsonify({"error": "حط رابط الفيديو الأول!"}), 400

    task_id = str(uuid.uuid4())
    download_status[task_id] = {"created_at": time.time()}
    threading.Thread(target=do_download, args=(task_id, url, format_type), daemon=True).start()

    return jsonify({"task_id": task_id})


@app.route("/status/<task_id>")
def get_status(task_id):
    return jsonify(download_status.get(task_id, {"status": "not_found"}))


@app.route("/download/<task_id>")
def download_file(task_id):
    info = download_status.get(task_id)
    if not info or info.get("status") != "done":
        return "الملف مش جاهز", 404

    file_path = info.get("file_path")
    if not file_path or not os.path.exists(file_path):
        return "الملف اتمسح، حمّله تاني", 404

    title = info.get("title", "download")
    ext = info.get("ext", "mp4")
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-").strip()[:80]

    @after_this_request
    def remove_file(response):
        threading.Timer(5.0, lambda: os.remove(file_path) if os.path.exists(file_path) else None).start()
        return response

    return send_file(file_path, as_attachment=True, download_name=f"{safe_title}.{ext}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
