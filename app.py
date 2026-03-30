from flask import Flask, render_template, request, jsonify, send_file, after_this_request
import os
import uuid
import threading
import time
import requests

app = Flask(__name__)

DOWNLOAD_FOLDER = "/tmp/downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

# ============================================================
# Global state
# ============================================================
download_status = {}
semaphore = threading.Semaphore(3)


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
# Cobalt API instances — لو واحدة وقفت يجرب التانية
# ============================================================
COBALT_INSTANCES = [
    "https://api.cobalt.tools",
    "https://cobalt.api.onrender.com",
    "https://co.wuk.sh",
]


# ============================================================
# Friendly Arabic error messages
# ============================================================
def friendly_error(err: str) -> str:
    e = err.lower()
    if "unavailable" in e or "not available" in e:
        return "الفيديو ده مش متاح أو اتحذف."
    if "private" in e:
        return "الفيديو خاص ومش متاح للعموم."
    if "age" in e:
        return "الفيديو ده محدود بالسن ومش ممكن تحميله."
    if "rate" in e or "429" in e or "too many" in e:
        return "كتر الطلبات. استنى دقيقة وجرب تاني."
    if "timeout" in e or "connection" in e or "network" in e:
        return "مشكلة في الاتصال. تحقق من الشبكة وحاول تاني."
    if "unsupported" in e or "not supported" in e:
        return "الموقع ده مش مدعوم. جرب يوتيوب أو تيك توك أو انستجرام."
    if "error" in e:
        return f"حصل خطأ: {err[:150]}"
    return f"خطأ غير متوقع: {err[:150]}"


# ============================================================
# Call Cobalt API
# ============================================================
def call_cobalt(url: str, format_type: str) -> dict:
    payload = {
        "url": url,
        "downloadMode": "audio" if format_type == "audio" else "auto",
        "audioFormat": "mp3",
        "audioBitrate": "192",
        "videoQuality": "720",
        "filenameStyle": "basic",
    }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    last_error = "كل الـ servers مش شغالة دلوقتي."

    for instance in COBALT_INSTANCES:
        try:
            resp = requests.post(
                f"{instance}/",
                json=payload,
                headers=headers,
                timeout=30,
            )

            if resp.status_code == 429:
                last_error = "كتر الطلبات. استنى دقيقة وجرب تاني."
                continue

            if resp.status_code != 200:
                last_error = f"السيرفر رجع كود {resp.status_code}."
                continue

            data = resp.json()
            status = data.get("status", "")

            # نجاح مباشر — رابط تحميل جاهز
            if status in ("redirect", "stream", "tunnel"):
                return {"ok": True, "url": data.get("url"), "filename": data.get("filename", "")}

            # picker — في أكتر من فيديو (playlist أو stories)
            if status == "picker":
                items = data.get("picker", [])
                if items:
                    return {"ok": True, "url": items[0].get("url"), "filename": ""}

            # خطأ من Cobalt
            if status == "error":
                err_code = data.get("error", {}).get("code", "unknown")
                last_error = friendly_error(err_code)
                continue

            last_error = f"رد غير متوقع من السيرفر: {status}"

        except requests.exceptions.Timeout:
            last_error = "السيرفر استغرق وقت طويل. حاول تاني."
            continue
        except requests.exceptions.ConnectionError:
            last_error = "مش قادر يتصل بالسيرفر. تحقق من الاتصال."
            continue
        except Exception as ex:
            last_error = friendly_error(str(ex))
            continue

    return {"ok": False, "error": last_error}


# ============================================================
# Download worker
# ============================================================
def do_download(task_id: str, url: str, format_type: str):
    download_status[task_id].update({
        "status": "downloading",
        "progress": 10,
        "title": "",
        "error": "",
    })

    with semaphore:
        try:
            # ── اطلب من Cobalt رابط التحميل ──────────────────
            download_status[task_id]["progress"] = 20
            result = call_cobalt(url, format_type)

            if not result["ok"]:
                raise Exception(result["error"])

            dl_url   = result["url"]
            filename = result.get("filename", "")

            if not dl_url:
                raise Exception("Cobalt ما رجعش رابط تحميل.")

            # ── حدد الامتداد ──────────────────────────────────
            if format_type == "audio":
                final_ext = "mp3"
            else:
                # حاول تاخد الامتداد من اسم الملف
                if filename and "." in filename:
                    final_ext = filename.rsplit(".", 1)[-1].lower()
                else:
                    final_ext = "mp4"

            # ── نزّل الملف من الرابط ──────────────────────────
            download_status[task_id]["progress"] = 30

            output_path = os.path.join(DOWNLOAD_FOLDER, f"{task_id}.{final_ext}")

            with requests.get(dl_url, stream=True, timeout=300) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                downloaded = 0

                with open(output_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 64):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total > 0:
                                pct = int(min(30 + downloaded / total * 65, 95))
                                download_status[task_id]["progress"] = pct

            # ── تحقق من الملف ─────────────────────────────────
            if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
                raise Exception("الملف ما اتحملش صح أو حجمه صفر.")

            # ── اعمل عنوان من الـ URL ─────────────────────────
            title = filename.rsplit(".", 1)[0] if filename else url.split("/")[-1][:60] or "video"
            download_status[task_id]["title"] = title

            download_status[task_id].update({
                "status": "done",
                "progress": 100,
                "file_path": output_path,
                "ext": final_ext,
            })

        except Exception as e:
            download_status[task_id].update({
                "status": "error",
                "error": friendly_error(str(e)),
            })


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

    # Rate limiting
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
            except Exception:
                pass
        threading.Timer(10.0, _delete).start()
        return response

    return send_file(file_path, as_attachment=True, download_name=f"{safe_title}.{ext}")


# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
