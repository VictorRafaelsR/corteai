"""
CorteAI — Flask Web App
Run: python app.py
Deploy: railway up  /  render.com  /  heroku
"""
import os, uuid, threading
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_file, abort

from processor import process_video, RESULTS_DIR

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 0  # no uploads, only URLs

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/process", methods=["POST"])
def process():
    data = request.get_json(force=True)
    url    = (data.get("url") or "").strip()
    fmt    = data.get("format", "tiktok")
    dur    = int(data.get("duration", 30))
    clips  = max(1, min(10, int(data.get("clips", 5))))
    player = (data.get("player") or "").strip()

    if not url or ("youtube.com" not in url and "youtu.be" not in url):
        return jsonify({"error": "Link do YouTube inválido"}), 400
    if fmt not in ("tiktok", "instagram", "youtube"):
        fmt = "tiktok"

    job_id = str(uuid.uuid4())

    status_file = RESULTS_DIR / f"{job_id}.json"
    import json
    status_file.write_text(json.dumps({
        "status": "processing",
        "progress": 2,
        "message": "Iniciando...",
        "job_id": job_id,
    }))

    t = threading.Thread(
        target=process_video,
        args=(job_id, url, fmt, dur, clips, player),
        daemon=True
    )
    t.start()

    return jsonify({"job_id": job_id})

@app.route("/status/<job_id>")
def status(job_id):
    if not job_id.replace("-", "").isalnum():
        abort(400)
    status_file = RESULTS_DIR / f"{job_id}.json"
    if not status_file.exists():
        return jsonify({"status": "not_found"}), 404
    import json
    return jsonify(json.loads(status_file.read_text()))

@app.route("/download/<job_id>")
def download(job_id):
    if not job_id.replace("-", "").isalnum():
        abort(400)
    import json
    status_file = RESULTS_DIR / f"{job_id}.json"
    if not status_file.exists():
        abort(404)
    data = json.loads(status_file.read_text())
    if data.get("status") != "done":
        abort(400)
    output_file = data.get("output_file")
    file_path = RESULTS_DIR / job_id / output_file
    if not file_path.exists():
        abort(404)
    return send_file(
        file_path,
        as_attachment=True,
        download_name=f"CorteAI_{output_file}",
        mimetype="video/mp4"
    )

def cleanup_old_jobs():
    import time
    while True:
        time.sleep(3600)
        now = time.time()
        for p in RESULTS_DIR.iterdir():
            if p.is_dir() and (now - p.stat().st_mtime) > 3600:
                import shutil
                shutil.rmtree(p, ignore_errors=True)
            elif p.suffix == ".json" and (now - p.stat().st_mtime) > 3600:
                p.unlink(missing_ok=True)

threading.Thread(target=cleanup_old_jobs, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print(f"\n🎬 CorteAI rodando em http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=debug)
