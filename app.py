"""
CorteAI - Flask Web App
"""
import os, uuid, threading, time
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_file, abort

from processor import process_video, RESULTS_DIR

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

@app.route("/health", methods=["GET", "OPTIONS"])
def health():
    return jsonify({"status": "ok", "service": "CorteAI", "time": time.time()})

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/process", methods=["POST", "OPTIONS"])
def process():
    if request.method == "OPTIONS":
        return jsonify({}), 200
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        return jsonify({"error": "Requisicao invalida"}), 400

    url     = (data.get("url") or "").strip()
    fmt     = data.get("format", "tiktok")
    dur     = int(data.get("duration", 30))
    clips   = max(1, min(10, int(data.get("clips", 5))))
    player  = (data.get("player") or "").strip()
    quality = data.get("quality", "720p")
    if quality not in ("360p", "720p", "1080p"):
        quality = "720p"

    if not url:
        return jsonify({"error": "URL nao fornecida"}), 400
    if fmt not in ("tiktok", "instagram", "youtube"):
        fmt = "tiktok"

    job_id = str(uuid.uuid4())
    status_file = RESULTS_DIR / f"{job_id}.json"
    import json
    status_file.write_text(json.dumps({
        "status": "processing", "progress": 0,
        "message": "Iniciando...", "quality": quality
    }))

    t = threading.Thread(
        target=process_video,
        args=(job_id, url, fmt, dur, clips, player, quality),
        daemon=True
    )
    t.start()
    return jsonify({"job_id": job_id})

@app.route("/status/<job_id>", methods=["GET", "OPTIONS"])
def status(job_id):
    if request.method == "OPTIONS":
        return jsonify({}), 200
    if not job_id.replace("-", "").isalnum():
        abort(400)
    status_file = RESULTS_DIR / f"{job_id}.json"
    if not status_file.exists():
        return jsonify({"status": "not_found"}), 404
    import json
    try:
        return jsonify(json.loads(status_file.read_text()))
    except Exception:
        return jsonify({"status": "processing", "progress": 5, "message": "Processando..."}), 200

@app.route("/download/<job_id>", methods=["GET"])
def download(job_id):
    if not job_id.replace("-", "").isalnum():
        abort(400)
    import json
    status_file = RESULTS_DIR / f"{job_id}.json"
    if not status_file.exists():
        abort(404)
    try:
        data = json.loads(status_file.read_text())
    except Exception:
        abort(500)
    if data.get("status") != "done":
        abort(400)
    output_file = data.get("output_file")
    if not output_file:
        abort(404)
    job_dir = RESULTS_DIR / job_id
    output_path = job_dir / output_file
    if not output_path.exists():
        abort(404)
    return send_file(str(output_path), as_attachment=True, download_name=output_file)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
