from flask import Flask, render_template, request, jsonify, send_file, after_this_request
import yt_dlp
import os
import uuid
import threading
import time

app = Flask(__name__)

DOWNLOAD_FOLDER = "/tmp/downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# ============================================================
# Global state — works with workers=1 + threads=4
# ============================================================
download_status = {}
semaphore = threading.Semaphore(3)   # max 3 downloads at the same time


# ============================================================
# Cleanup old files every 2 minutes
# ============================================================
def cleanup_old_files():
    while True:
        try:
            now = time.time()
            for fname in os.listdir(DOWNLOAD_FOLDER):
                fpath = os.path.join(DOWNLOAD_FOLDER, fname)
                if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > 600:
                    os.remove(fpath)
        except Exception:
            pass
        time.sleep(120)

threading.Thread(target=cleanup_old_files, daemon=True).start()


# ============================================================
# Common yt-dlp options
# ============================================================
COOKIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")

COMMON_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noprogress": True,
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.youtube.com/",
    },
    "extractor_args": {
        "youtube": {
            "player_client": ["web", "android", "ios"],
        },
        "youtubetab": {"skip": ["authcheck"]},
    },
    "socket_timeout": 60,
    "retries": 10,
    "fragment_retries": 10,
    "file_access_retries": 5,
    "ignoreerrors": False,
    "geo_bypass": True,
}

# Cookies
if os.path.exists(COOKIES_FILE):
    COMMON_OPTS["cookiefile"] = COOKIES_FILE

# Optional proxy from environment variable
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
if PROXY_URL:
    COMMON_OPTS["proxy"] = PROXY_URL


# ============================================================
# Friendly Arabic error messages
# ============================================================
def friendly_error(err: str) -> str:
    e = err.lower()
    if "sign in" in e or "login" in e or "confirm" in e:
        return "يوتيوب طلب تسجيل دخول أو اكتشف السيرفر. جدد cookies.txt وحاول تاني."
    if "bot" in e or "automated" in e:
        return "يوتيوب اعتبرك بوت. استنى 5 دقايق وحاول تاني، أو جدد الـ cookies."
    if "429" in e or "too many" in e:
        return "يوتيوب عامل Rate Limit. استنى 10 دقايق وجرب تاني."
    if "403" in e:
        return "يوتيوب رفض الطلب (403). جدد cookies.txt وحاول تاني."
    if "video unavailable" in e or "not available" in e:
        return "الفيديو ده مش متاح أو اتحذف."
    if "private video" in e:
        return "الفيديو خاص ومش متاح للعموم."
    if "format is not available" in e or "requested format" in e:
        return "الجودة المطلوبة مش متاحة لهذا الفيديو."
    if "ffmpeg" in e:
        return "مشكلة في ffmpeg أثناء تحويل الصوت. تأكد إن ffmpeg متنصب."
    if "urlopen" in e or "network" in e or "connection" in e or "timeout" in e:
        return "مشكلة في الاتصال. تحقق من الشبكة وحاول تاني."
    if "no such file" in e or "filenotfound" in e:
        return "الملف ما اتحملش صح. حاول تاني."
    if "no space" in e or "disk" in e:
        return "السيرفر ملهوش مساحة كافية دلوقتي. حاول بعد شوية."
    return err


# ============================================================
# Download worker
# ============================================================
def do_download(task_id: str, url: str, format_type: str):
    download_status[task_id].update({
        "status": "downloading",
        "progress": 0,
        "title": "",
        "error": "",
    })

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 1
            downloaded = d.get("downloaded_bytes", 0)
            pct = int(min(downloaded / total * 100, 99))
            download_status[task_id]["progress"] = pct
        elif d["status"] == "finished":
            download_status[task_id]["progress"] = 99

    output_path = os.path.join(DOWNLOAD_FOLDER, task_id)

    with semaphore:   # max 3 concurrent downloads
        try:
            if format_type == "audio":
                ydl_opts = {
                    **COMMON_OPTS,
                    "format": (
                        "bestaudio[ext=m4a]/bestaudio[ext=webm]/"
                        "bestaudio[ext=opus]/bestaudio/best"
                    ),
                    "outtmpl": output_path + ".%(ext)s",
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "192",
                        },
                        {"key": "FFmpegMetadata", "add_metadata": True},
                    ],
                    "postprocessor_args": {
                        "ffmpeg": ["-ar", "44100", "-ac", "2"],
                    },
                    "progress_hooks": [progress_hook],
                }
                final_ext = "mp3"

            else:
                ydl_opts = {
                    **COMMON_OPTS,
                    # Priority: ready mp4 file → merge → anything available
                    "format": (
                        "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/"
                        "bestvideo[height<=720][ext=mp4]+bestaudio/"
                        "bestvideo[height<=720]+bestaudio[ext=m4a]/"
                        "bestvideo[height<=720]+bestaudio/"
                        "best[height<=720][ext=mp4]/"
                        "best[height<=720]/"
                        "best[ext=mp4]/best"
                    ),
                    "outtmpl": output_path + ".%(ext)s",
                    "merge_output_format": "mp4",
                    "postprocessors": [
                        {"key": "FFmpegMetadata", "add_metadata": True},
                    ],
                    "progress_hooks": [progress_hook],
                }
                final_ext = "mp4"

            # ── Download ──────────────────────────────────────
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = (info or {}).get("title", "video")
                download_status[task_id]["title"] = title

            # ── Find output file ──────────────────────────────
            file_path = output_path + f".{final_ext}"

            if not os.path.exists(file_path):
                for f in sorted(os.listdir(DOWNLOAD_FOLDER)):
                    if (f.startswith(task_id)
                            and not f.endswith(".part")
                            and not f.endswith(".ytdl")
                            and not f.endswith(".jpg")
                            and not f.endswith(".png")
                            and not f.endswith(".webp")):
                        candidate = os.path.join(DOWNLOAD_FOLDER, f)
                        if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                            file_path = candidate
                            final_ext = f.rsplit(".", 1)[-1] if "." in f else final_ext
                            break

            if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
                raise FileNotFoundError("الملف ما اتحملش صح أو حجمه صفر")

            download_status[task_id].update({
                "status": "done",
                "progress": 100,
                "file_path": file_path,
                "ext": final_ext,
            })

        except yt_dlp.utils.DownloadError as e:
            raw = str(e).replace("ERROR: ", "").strip()
            # أرجع الـ raw error كمان عشان نشوف السبب الحقيقي
            translated = friendly_error(raw)
            final_err  = f"{translated}\n\n[Debug: {raw[:300]}]"
            download_status[task_id].update({"status": "error", "error": final_err})

        except FileNotFoundError as e:
            download_status[task_id].update({"status": "error", "error": str(e)})

        except Exception as e:
            download_status[task_id].update({"status": "error", "error": friendly_error(str(e))})


# ============================================================
# Routes
# ============================================================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start_download():
    data = request.get_json(silent=True) or {}
    url  = (data.get("url") or "").strip()
    fmt  = data.get("format", "video")

    if not url:
        return jsonify({"error": "حط رابط الفيديو الأول!"}), 400
    if not (url.startswith("http://") or url.startswith("https://")):
        return jsonify({"error": "الرابط مش صحيح. تأكد إنه يبدأ بـ http أو https"}), 400
    if fmt not in ("audio", "video"):
        fmt = "video"

    # Simple rate limiting: max 10 active tasks
    active = sum(1 for v in download_status.values()
                 if v.get("status") in ("pending", "downloading"))
    if active >= 10:
        return jsonify({"error": "السيرفر مشغول دلوقتي. استنى شوية وحاول تاني."}), 429

    task_id = str(uuid.uuid4())
    download_status[task_id] = {
        "created_at": time.time(),
        "status": "pending",
        "progress": 0,
        "title": "",
        "error": "",
    }

    threading.Thread(target=do_download, args=(task_id, url, fmt), daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/status/<task_id>")
def get_status(task_id):
    info = download_status.get(task_id)
    if not info:
        return jsonify({"status": "not_found"}), 404
    safe = {k: v for k, v in info.items() if k not in ("file_path", "created_at")}
    return jsonify(safe)


@app.route("/download/<task_id>")
def download_file(task_id):
    info = download_status.get(task_id)
    if not info or info.get("status") != "done":
        return "الملف مش جاهز أو مش موجود", 404

    file_path = info.get("file_path")
    if not file_path or not os.path.exists(file_path):
        return "الملف اتمسح من السيرفر — حمّله تاني", 404

    title      = info.get("title", "download")
    ext        = info.get("ext", "mp4")
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-.()").strip()[:80] or "download"

    @after_this_request
    def remove_file(response):
        def _delete():
            try:
                if os.path.exists(file_path):
                    os.remove(file_path)
                # Remove thumbnail files if any
                base = file_path.rsplit(".", 1)[0]
                for thumb_ext in (".jpg", ".jpeg", ".png", ".webp"):
                    tp = base + thumb_ext
                    if os.path.exists(tp):
                        os.remove(tp)
            except Exception:
                pass
        threading.Timer(10.0, _delete).start()
        return response

    return send_file(file_path, as_attachment=True, download_name=f"{safe_title}.{ext}")


# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
