"""
CorteAI - Flask Web App
Run: python app.py
Deploy: railway up / render.com / heroku
"""
import os, uuid, threading, time
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_file, abort

from processor import process_video, RESULTS_DIR

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2 MB max (JSON only)

# CORS - allow all origins (fixes "Erro de conexao" on mobile/cross-origin)
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

    url    = (data.get("url") or "").strip()
    fmt    = data.get("format", "tiktok")
    dur    = int(data.get("duration", 30))
    clips  = max(1, min(10, int(data.get("clips", 5))))
    player = (data.get("player") or "").strip()

    if not url or ("youtube.com" not in url and "youtu.be" not in url):
        return jsonify({"error": "Link do YouTube invalido. Cole um link youtube.com ou youtu.be"}), 400
    if fmt not in ("tiktok", "instagram", "youtube"):
        fmt = "tiktok"

    job_id = str(uuid.uuid4())
    status_file = RESULTS_DIR / f"{job_id}.json"
    import json
    status_file.write_text(json.dumps({
        "status": "processing", "progress": 2, "message": "Iniciando...", "job_id": job_id,
    }))

    t = threading.Thread(target=process_video, args=(job_id, url, fmt, dur, clips, player), daemon=True)
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
        abort(500)
    file_path = RESULTS_DIR / job_id / output_file
    if not file_path.exists():
        abort(404)
    return send_file(file_path, as_attachment=True, download_name=f"CorteAI_{output_file}", mimetype="video/mp4")

@app.errorhandler(400)
def bad_request(e): return jsonify({"error": "Requisicao invalida"}), 400

@app.errorhandler(404)
def not_found(e): return jsonify({"error": "Nao encontrado"}), 404

@app.errorhandler(413)
def too_large(e): return jsonify({"error": "Dados muito grandes"}), 413

@app.errorhandler(500)
def server_error(e): return jsonify({"error": "Erro interno do servidor. Tente novamente."}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    print(f"\nCorteAI rodando em http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=debug)
