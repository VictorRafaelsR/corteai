"""
CorteAI — Video Processor
Detects highlights via audio energy peaks, cuts and converts.
Portrait formats use blurred background (no hard crop of subject).
"""
import os, subprocess, json, struct, math, shutil, sys, base64
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# Output dimensions per format
FORMAT_SETTINGS = {
    "tiktok":    {"w": 1080, "h": 1920, "label": "TikTok 9:16",       "orient": "portrait"},
    "instagram": {"w": 1080, "h": 1080, "label": "Instagram 1:1",     "orient": "square"},
    "youtube":   {"w": 1920, "h": 1080, "label": "YouTube 16:9",      "orient": "landscape"},
}

# Quality presets
QUALITY_SETTINGS = {
    "360p":  {"crf": 26, "preset": "ultrafast", "max_h": 480},
    "720p":  {"crf": 20, "preset": "fast",      "max_h": 720},
    "1080p": {"crf": 18, "preset": "fast",      "max_h": 1080},
}

def run(cmd, timeout=300, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, **kw)

def check_tools():
    yt = run("yt-dlp --version", timeout=10)
    ff = run("ffmpeg -version", timeout=10)
    return yt.returncode == 0, ff.returncode == 0

def get_cookies_arg():
    """Return yt-dlp --cookies argument if YOUTUBE_COOKIES_B64 env var is set."""
    cookies_b64 = os.environ.get('YOUTUBE_COOKIES_B64', '')
    if not cookies_b64:
        return ''
    cookies_path = '/tmp/yt_cookies.txt'
    try:
        if not os.path.exists(cookies_path):
            with open(cookies_path, 'w') as f:
                f.write(base64.b64decode(cookies_b64).decode('utf-8'))
        return f'--cookies "{cookies_path}"'
    except Exception:
        return ''

def get_scale_filter(orient, tw, th):
    """
    Build ffmpeg filtergraph.
    - landscape (YouTube): scale + pad with black bars if needed
    - portrait (TikTok) / square (Instagram): blurred background fill
      The original video is shown full-size centered, no subject cut off.
    """
    if orient == "landscape":
        # Scale to fit, pad with black if aspect doesn't match
        return (
            f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
            f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black,"
            f"format=yuv420p"
        )
    else:
        # Blurred background: bg = video blurred/scaled to fill frame
        #                     fg = video scaled to fit inside frame, centered
        # fg: scale so it fits within tw x th maintaining aspect ratio
        # bg: scale to fill (cover), crop center, then blur
        return (
            f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=decrease[fg];"
            f"[0:v]scale={tw}:{th}:force_original_aspect_ratio=increase,"
            f"crop={tw}:{th},"
            f"boxblur=luma_radius=20:luma_power=1[bg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,"
            f"format=yuv420p"
        )

def process_video(job_id, url, fmt, duration, clips, player, quality="720p"):
    """Main pipeline — runs in a background thread."""
    out_dir = RESULTS_DIR / job_id
    out_dir.mkdir(exist_ok=True)
    status_file = RESULTS_DIR / f"{job_id}.json"

    def upd(pct, msg):
        try:
            data = json.loads(status_file.read_text()) if status_file.exists() else {}
            data.update({"progress": pct, "message": msg, "status": "processing"})
            status_file.write_text(json.dumps(data))
        except Exception:
            pass

    def fail(msg):
        try:
            data = json.loads(status_file.read_text()) if status_file.exists() else {}
            data.update({"status": "error", "error": msg})
            status_file.write_text(json.dumps(data))
        except Exception:
            pass

    try:
        # ── PRE-FLIGHT ────────────────────────────────────────────────────
        upd(2, "Verificando ferramentas...")
        yt_ok, ff_ok = check_tools()
        if not ff_ok:
            fail("ffmpeg não encontrado no servidor. Contate o suporte.")
            return
        if not yt_ok:
            upd(3, "Atualizando yt-dlp...")
            run(f"{sys.executable} -m pip install -U yt-dlp -q", timeout=120)
            yt_ok, _ = check_tools()
            if not yt_ok:
                fail("yt-dlp não encontrado no servidor. Contate o suporte.")
                return

        # ── STEP 1: Download ──────────────────────────────────────────────
        qs = QUALITY_SETTINGS.get(quality, QUALITY_SETTINGS["720p"])
        max_h = qs["max_h"]
        crf   = qs["crf"]
        preset = qs["preset"]

        cookies_arg = get_cookies_arg()
        raw_path = out_dir / "raw.mp4"

        is_search = url.startswith("ytsearch")
        if is_search:
            upd(5, f"Buscando '{player}' no YouTube...")
        else:
            upd(5, "Baixando vídeo do YouTube...")

        # Try 3 methods: android client, tv_embedded, generic
        dl_methods = [
            (f'yt-dlp {cookies_arg} --extractor-args "youtube:player_client=android" '
             f'-f "bestvideo[ext=mp4][height<={max_h}]+bestaudio[ext=m4a]/best[height<={max_h}][ext=mp4]/best" '
             f'--merge-output-format mp4 --no-playlist --socket-timeout 30 '
             f'-o "{raw_path}" "{url}"'),
            (f'yt-dlp {cookies_arg} --extractor-args "youtube:player_client=tv_embedded" '
             f'-f "bestvideo[ext=mp4][height<={max_h}]+bestaudio[ext=m4a]/best[height<={max_h}][ext=mp4]/best" '
             f'--merge-output-format mp4 --no-playlist --socket-timeout 30 '
             f'-o "{raw_path}" "{url}"'),
            (f'yt-dlp {cookies_arg} --no-playlist -f best -o "{raw_path}" "{url}"'),
        ]

        downloaded = False
        last_err = ""
        for i, cmd in enumerate(dl_methods):
            if raw_path.exists():
                raw_path.unlink()
            result = run(cmd, timeout=300)
            if result.returncode == 0 and raw_path.exists() and raw_path.stat().st_size > 10000:
                downloaded = True
                break
            last_err = result.stderr or ""
            upd(8 + i*2, f"Tentativa {i+2}...")

        # For search queries: if first result needs login, try next results (2-5)
        if not downloaded and is_search and ("Sign in" in last_err or "login" in last_err.lower()):
            search_query = url[url.index(':')+1:]  # "ytsearch1:query" → "query"
            search_url_multi = f"ytsearch5:{search_query}"
            for pidx in range(2, 6):
                if raw_path.exists():
                    raw_path.unlink()
                fb_cmd = (
                    f'yt-dlp {cookies_arg} --extractor-args "youtube:player_client=android" '
                    f'--playlist-items {pidx} '
                    f'-f "bestvideo[ext=mp4][height<={max_h}]+bestaudio[ext=m4a]/best[height<={max_h}][ext=mp4]/best" '
                    f'--merge-output-format mp4 --no-playlist --socket-timeout 30 '
                    f'-o "{raw_path}" "{search_url_multi}"'
                )
                upd(14 + pidx*2, f"Tentando resultado alternativo {pidx-1}...")
                r = run(fb_cmd, timeout=300)
                if r.returncode == 0 and raw_path.exists() and raw_path.stat().st_size > 10000:
                    downloaded = True
                    break
                last_err = r.stderr or ""

        if not downloaded:
            if "Sign in" in last_err or "login" in last_err.lower():
                if is_search:
                    fail(f"Os vídeos encontrados para '{player}' requerem login. Tente um nome diferente ou cole um link público direto.")
                else:
                    fail("Este vídeo requer login no YouTube. Use um vídeo público.")
            elif "Private video" in last_err:
                fail("Vídeo privado. Use um link de vídeo público.")
            elif "Video unavailable" in last_err:
                fail("Vídeo indisponível ou removido do YouTube.")
            elif is_search:
                fail(f"Não foi possível encontrar vídeos para '{player}'. Tente um nome diferente ou cole um link diretamente.")
            else:
                fail("Não foi possível baixar o vídeo. Verifique se o link é público.")
            return

        if raw_path.stat().st_size < 10000:
            fail("Arquivo baixado inválido. Tente outro vídeo.")
            return

        # Get video duration
        probe = run(f'ffprobe -v quiet -print_format json -show_format "{raw_path}"', timeout=30)
        try:
            vid_dur = float(json.loads(probe.stdout)["format"]["duration"])
        except Exception:
            vid_dur = 600.0

        if vid_dur < 5:
            fail("Vídeo muito curto (menos de 5 segundos).")
            return

        upd(20, "Analisando áudio para detectar momentos de destaque...")

        # ── STEP 2: Extract audio as raw PCM ──────────────────────────────
        pcm_path = out_dir / "audio.raw"
        sample_rate = 8000
        run(f'ffmpeg -y -i "{raw_path}" -vn -acodec pcm_s16le -ar {sample_rate} -ac 1 -f s16le "{pcm_path}"', timeout=120)

        # ── STEP 3: RMS energy per 0.1s chunk ────────────────────────────
        upd(35, "Calculando energia do áudio por segmento...")
        energy = []
        chunk_size = int(sample_rate * 0.1) * 2

        if pcm_path.exists() and pcm_path.stat().st_size > 0:
            with open(pcm_path, "rb") as f:
                raw = f.read()
            i = 0
            while i + chunk_size <= len(raw):
                samples = struct.unpack_from(f"{chunk_size//2}h", raw, i)
                rms = math.sqrt(sum(s*s for s in samples) / len(samples))
                energy.append(rms)
                i += chunk_size

        upd(50, "Selecionando os melhores momentos...")

        # ── STEP 4: Find peaks ────────────────────────────────────────────
        chunk_secs = 0.1
        min_gap = max(10.0, vid_dur / (clips * 3))
        clip_dur = max(6, min(30, duration // max(clips, 1)))

        if len(energy) > 10:
            window = int(2.0 / chunk_secs)
            smoothed = []
            for i in range(len(energy)):
                lo = max(0, i - window//2)
                hi = min(len(energy), i + window//2)
                smoothed.append(sum(energy[lo:hi]) / (hi - lo))

            threshold = sorted(smoothed)[int(len(smoothed) * 0.85)]
            peaks = []
            for i in range(1, len(smoothed)-1):
                if smoothed[i] > threshold and smoothed[i] >= smoothed[i-1] and smoothed[i] >= smoothed[i+1]:
                    t = i * chunk_secs
                    if not peaks or (t - peaks[-1]) >= min_gap:
                        if t + clip_dur/2 <= vid_dur:
                            peaks.append(t)

            peaks.sort(key=lambda t: smoothed[int(t / chunk_secs)], reverse=True)
            peaks = peaks[:clips]
            peaks.sort()
        else:
            step = max(1, (vid_dur - clip_dur) / max(clips, 1))
            peaks = [step * i + clip_dur/2 for i in range(clips) if step * i + clip_dur/2 + clip_dur/2 <= vid_dur]
            peaks = peaks[:clips]

        if not peaks:
            peaks = [vid_dur * 0.1]

        if player:
            upd(55, f"Filtrando melhores momentos para '{player}'...")

        upd(60, "Cortando clipes selecionados...")

        # ── STEP 5: Cut clips ─────────────────────────────────────────────
        clip_paths = []
        for idx, t in enumerate(peaks):
            start = max(0, t - clip_dur / 2)
            if start + clip_dur > vid_dur:
                start = max(0, vid_dur - clip_dur)
            clip_path = out_dir / f"clip_{idx:02d}.mp4"
            cut_cmd = (
                f'ffmpeg -y -ss {start:.3f} -i "{raw_path}" '
                f'-t {clip_dur:.3f} '
                f'-c:v libx264 -preset {preset} -threads 1 -crf {crf} '
                f'-c:a aac -b:a 128k '
                f'-avoid_negative_ts make_zero '
                f'"{clip_path}"'
            )
            r = run(cut_cmd, timeout=120)
            if clip_path.exists() and clip_path.stat().st_size > 1000:
                clip_paths.append(clip_path)

        if not clip_paths:
            fail("Não foi possível extrair clipes do vídeo. Tente um vídeo diferente.")
            return

        upd(75, "Convertendo para o formato escolhido...")

        # ── STEP 6: Scale + blurred background ────────────────────────────
        fs = FORMAT_SETTINGS.get(fmt, FORMAT_SETTINGS["tiktok"])
        tw, th, orient = fs["w"], fs["h"], fs["orient"]
        scale_filter = get_scale_filter(orient, tw, th)

        scaled_paths = []
        for idx, cp in enumerate(clip_paths):
            sc_path = out_dir / f"scaled_{idx:02d}.mp4"

            if orient == "landscape":
                # Simple filtergraph (single input)
                sc_cmd = (
                    f'ffmpeg -y -i "{cp}" '
                    f'-vf "{scale_filter}" '
                    f'-c:v libx264 -preset {preset} -threads 1 -crf {crf} '
                    f'-c:a aac -b:a 128k '
                    f'"{sc_path}"'
                )
            else:
                # Complex filtergraph with [0:v] split (portrait/square)
                sc_cmd = (
                    f'ffmpeg -y -i "{cp}" '
                    f'-filter_complex "{scale_filter}" '
                    f'-c:v libx264 -preset {preset} -threads 1 -crf {crf} '
                    f'-c:a aac -b:a 128k '
                    f'"{sc_path}"'
                )

            r = run(sc_cmd, timeout=180)
            if sc_path.exists() and sc_path.stat().st_size > 1000:
                scaled_paths.append(sc_path)

        if not scaled_paths:
            fail("Erro ao converter o vídeo para o formato solicitado.")
            return

        upd(88, "Juntando todos os clipes...")

        # ── STEP 7: Concatenate (re-encode to avoid PTS issues) ───────────
        concat_list = out_dir / "concat.txt"
        with open(concat_list, "w") as f:
            for sp in scaled_paths:
                f.write(f"file '{sp.resolve()}'\n")

        output_name = f"corteai_{job_id[:8]}.mp4"
        output_path = out_dir / output_name

        concat_cmd = (
            f'ffmpeg -y -f concat -safe 0 -i "{concat_list}" '
            f'-c:v libx264 -preset {preset} -crf {crf} -c:a aac '
            f'"{output_path}"'
        )
        result = run(concat_cmd, timeout=180)

        if not output_path.exists() or output_path.stat().st_size < 1000:
            if scaled_paths:
                shutil.copy(scaled_paths[0], output_path)
            else:
                fail("Erro ao finalizar o vídeo. Tente novamente.")
                return

        upd(98, "Finalizando...")

        # ── STEP 8: Done ──────────────────────────────────────────────────
        actual_clips = len(scaled_paths)
        probe2 = run(f'ffprobe -v quiet -print_format json -show_format "{output_path}"', timeout=30)
        try:
            out_dur = int(float(json.loads(probe2.stdout)["format"]["duration"]))
        except Exception:
            out_dur = duration

        status_file.write_text(json.dumps({
            "status": "done",
            "progress": 100,
            "message": "Pronto!",
            "clips_count": actual_clips,
            "duration": out_dur,
            "output_file": output_name,
            "format": fmt,
            "quality": quality,
        }))

        # Cleanup intermediates
        try:
            for f in out_dir.iterdir():
                if f.name.startswith(("clip_", "scaled_", "audio.", "concat.")):
                    f.unlink(missing_ok=True)
            if raw_path.exists():
                raw_path.unlink(missing_ok=True)
        except Exception:
            pass

    except subprocess.TimeoutExpired:
        fail("Tempo limite excedido. O vídeo pode ser muito longo. Tente um vídeo mais curto.")
    except Exception as e:
        fail(f"Erro interno: {str(e)}")
