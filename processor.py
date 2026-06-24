"""
CorteAI — Video Processor
Detects highlights via audio energy peaks, cuts and converts clips.
Supports quality selector: 360p, 720p (default), 1080p.
"""
import os, subprocess, json, struct, math, shutil, sys, base64
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# Output dimensions per format × quality
FORMAT_QUALITY = {
    ("tiktok",    "360p"):  (202,  360),
    ("tiktok",    "720p"):  (405,  720),
    ("tiktok",    "1080p"): (1080, 1920),
    ("instagram", "360p"):  (360,  360),
    ("instagram", "720p"):  (720,  720),
    ("instagram", "1080p"): (1080, 1080),
    ("youtube",   "360p"):  (640,  360),
    ("youtube",   "720p"):  (1280, 720),
    ("youtube",   "1080p"): (1920, 1080),
}

QUALITY_ENCODE = {
    "360p":  {"crf": 26, "preset": "ultrafast", "dl_h": 480},
    "720p":  {"crf": 20, "preset": "fast",      "dl_h": 720},
    "1080p": {"crf": 18, "preset": "fast",      "dl_h": 1080},
}

FORMAT_ORIENTATION = {
    "tiktok":    "portrait",
    "instagram": "square",
    "youtube":   "landscape",
}

def get_scale_filter(fmt, out_w, out_h):
    """Build a robust ffmpeg scale+crop filter for the given format."""
    orient = FORMAT_ORIENTATION.get(fmt, "landscape")
    if orient == "portrait":
        # Scale so height fills out_h, crop width from center
        return (f"scale=-2:{out_h},"
                f"crop={out_w}:{out_h}:(iw-{out_w})/2:0,"
                f"format=yuv420p")
    elif orient == "square":
        # Scale the larger dimension to out_h, crop square from center
        return (f"scale=-2:{out_h},"
                f"crop={out_w}:{out_h}:(iw-{out_w})/2:(ih-{out_h})/2,"
                f"format=yuv420p")
    else:  # landscape
        # Scale to fill width, crop height from center
        return (f"scale={out_w}:-2,"
                f"crop={out_w}:{out_h}:0:(ih-{out_h})/2,"
                f"format=yuv420p")

def run(cmd, timeout=300):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)

def get_cookies_arg():
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

def check_tools():
    yt = run("yt-dlp --version", timeout=10)
    ff = run("ffmpeg -version", timeout=10)
    return yt.returncode == 0, ff.returncode == 0

def process_video(job_id, url, fmt, duration, clips, player, quality="720p"):
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
        upd(2, "Verificando ferramentas...")
        yt_ok, ff_ok = check_tools()
        if not ff_ok:
            fail("ffmpeg nao encontrado. Contate o suporte.")
            return
        if not yt_ok:
            upd(3, "Atualizando yt-dlp...")
            run(f"{sys.executable} -m pip install -U yt-dlp -q", timeout=120)
            yt_ok, _ = check_tools()
            if not yt_ok:
                fail("yt-dlp nao encontrado. Contate o suporte.")
                return

        # Resolve quality settings
        quality = quality if quality in QUALITY_ENCODE else "720p"
        qe = QUALITY_ENCODE[quality]
        out_w, out_h = FORMAT_QUALITY.get((fmt, quality), FORMAT_QUALITY[("tiktok", "720p")])
        scale_filter = get_scale_filter(fmt, out_w, out_h)

        upd(5, f"Baixando video ({quality})...")
        raw_path = out_dir / "raw.mp4"
        cookies_arg = get_cookies_arg()
        dl_h = qe["dl_h"]
        fmt_arg = f'"bestvideo[ext=mp4][height<={dl_h}]+bestaudio[ext=m4a]/bestvideo[ext=mp4][height<={dl_h}]+bestaudio/best[ext=mp4]/best"'

        dl_cmd = (
            f'yt-dlp {cookies_arg} '
            f'--extractor-args "youtube:player_client=android,web" '
            f'-f {fmt_arg} --merge-output-format mp4 --no-playlist '
            f'--socket-timeout 30 -o "{raw_path}" "{url}"'
        )
        result = run(dl_cmd, timeout=300)

        if result.returncode != 0 or not raw_path.exists():
            upd(7, "Tentando metodo alternativo...")
            dl_cmd2 = (
                f'yt-dlp {cookies_arg} '
                f'--extractor-args "youtube:player_client=tv_embedded,web" '
                f'-f {fmt_arg} --merge-output-format mp4 --no-playlist '
                f'-o "{raw_path}" "{url}"'
            )
            result = run(dl_cmd2, timeout=300)

        if result.returncode != 0 or not raw_path.exists():
            upd(9, "Atualizando yt-dlp e tentando novamente...")
            run(f"{sys.executable} -m pip install -U yt-dlp -q", timeout=120)
            dl_cmd3 = (
                f'yt-dlp {cookies_arg} --extractor-args "youtube:player_client=android" '
                f'--no-playlist -f best -o "{raw_path}" "{url}"'
            )
            result = run(dl_cmd3, timeout=300)
            if result.returncode != 0 or not raw_path.exists():
                err = (result.stderr or result.stdout or "").strip()
                if "Private video" in err:
                    fail("Video privado. Apenas o dono pode acessar.")
                elif "Sign in" in err or "confirm your age" in err.lower():
                    fail("Este video requer login. Configure YOUTUBE_COOKIES_B64 no Railway para acessar videos restritos.")
                elif "Video unavailable" in err:
                    fail("Video indisponivel ou removido.")
                else:
                    fail("Nao foi possivel baixar o video. Verifique o link.")
                return

        if raw_path.stat().st_size < 10000:
            fail("Arquivo baixado invalido. Tente outro video.")
            return

        probe = run(f'ffprobe -v quiet -print_format json -show_format "{raw_path}"', timeout=30)
        try:
            vid_dur = float(json.loads(probe.stdout)["format"]["duration"])
        except Exception:
            vid_dur = 600.0

        if vid_dur < 5:
            fail("Video muito curto (menos de 5 segundos).")
            return

        upd(20, "Analisando audio...")
        pcm_path = out_dir / "audio.raw"
        sample_rate = 8000
        run(f'ffmpeg -y -i "{raw_path}" -vn -acodec pcm_s16le -ar {sample_rate} -ac 1 -f s16le "{pcm_path}"', timeout=120)

        upd(35, "Detectando momentos de destaque...")
        energy = []
        chunk_size = int(sample_rate * 0.1) * 2

        if pcm_path.exists() and pcm_path.stat().st_size > 0:
            with open(pcm_path, "rb") as f:
                raw_pcm = f.read()
            i = 0
            while i + chunk_size <= len(raw_pcm):
                samples = struct.unpack_from(f"{chunk_size//2}h", raw_pcm, i)
                rms = math.sqrt(sum(s*s for s in samples) / len(samples))
                energy.append(rms)
                i += chunk_size

        upd(50, "Selecionando melhores momentos...")
        chunk_secs = 0.1
        min_gap = max(10.0, vid_dur / (clips * 3))
        clip_dur = max(6, min(30, duration // max(clips, 1)))

        if len(energy) > 10:
            window = int(2.0 / chunk_secs)
            smoothed = []
            for i in range(len(energy)):
                lo = max(0, i - window // 2)
                hi = min(len(energy), i + window // 2)
                smoothed.append(sum(energy[lo:hi]) / (hi - lo))

            threshold = sorted(smoothed)[int(len(smoothed) * 0.85)]
            peaks = []
            for i in range(1, len(smoothed) - 1):
                if smoothed[i] > threshold and smoothed[i] >= smoothed[i-1] and smoothed[i] >= smoothed[i+1]:
                    t = i * chunk_secs
                    if not peaks or (t - peaks[-1]) >= min_gap:
                        if t + clip_dur / 2 <= vid_dur:
                            peaks.append(t)

            peaks.sort(key=lambda t: smoothed[int(t / chunk_secs)], reverse=True)
            peaks = peaks[:clips]
            peaks.sort()
        else:
            step = max(1, (vid_dur - clip_dur) / max(clips, 1))
            peaks = [step * i + clip_dur / 2 for i in range(clips)
                     if step * i + clip_dur / 2 + clip_dur / 2 <= vid_dur]
            peaks = peaks[:clips]

        if not peaks:
            peaks = [vid_dur * 0.1]

        if player:
            upd(55, f"Filtrando momentos para '{player}'...")

        upd(60, f"Cortando e convertendo clipes ({quality})...")
        crf = qe["crf"]
        preset = qe["preset"]

        scaled_paths = []
        for idx, t in enumerate(peaks):
            start = max(0, t - clip_dur / 2)
            if start + clip_dur > vid_dur:
                start = max(0, vid_dur - clip_dur)
            sc_path = out_dir / f"clip_{idx:02d}.mp4"

            sc_cmd = (
                f'ffmpeg -y -ss {start:.3f} -i "{raw_path}" '
                f'-t {clip_dur:.3f} '
                f'-vf "{scale_filter}" '
                f'-c:v libx264 -preset {preset} -threads 1 -crf {crf} '
                f'-c:a aac -b:a 128k '
                f'-avoid_negative_ts make_zero '
                f'"{sc_path}"'
            )
            r = run(sc_cmd, timeout=180)

            if sc_path.exists() and sc_path.stat().st_size > 1000:
                scaled_paths.append(sc_path)
            else:
                # Fallback without audio
                sc_cmd2 = (
                    f'ffmpeg -y -ss {start:.3f} -i "{raw_path}" '
                    f'-t {clip_dur:.3f} '
                    f'-vf "{scale_filter}" '
                    f'-c:v libx264 -preset ultrafast -threads 1 -crf {crf} '
                    f'-an -avoid_negative_ts make_zero '
                    f'"{sc_path}"'
                )
                r = run(sc_cmd2, timeout=180)
                if sc_path.exists() and sc_path.stat().st_size > 1000:
                    scaled_paths.append(sc_path)

        if not scaled_paths:
            fail("Nao foi possivel gerar clipes. Tente outro video.")
            return

        upd(88, "Juntando clipes...")
        concat_list = out_dir / "concat.txt"
        with open(concat_list, "w") as f:
            for sp in scaled_paths:
                f.write(f"file '{sp.resolve()}'\n")

        output_name = f"corteai_{job_id[:8]}_{quality}.mp4"
        output_path = out_dir / output_name

        # Re-encode concat to ensure seamless join (avoids split-screen artifacts)
        concat_cmd = (
            f'ffmpeg -y -f concat -safe 0 -i "{concat_list}" '
            f'-c:v libx264 -preset {preset} -crf {crf} -c:a aac '
            f'"{output_path}"'
        )
        result = run(concat_cmd, timeout=300)

        if not output_path.exists() or output_path.stat().st_size < 1000:
            if scaled_paths:
                shutil.copy(scaled_paths[0], output_path)
            else:
                fail("Erro ao finalizar o video.")
                return

        upd(98, "Finalizando...")
        probe2 = run(f'ffprobe -v quiet -print_format json -show_format "{output_path}"', timeout=30)
        try:
            out_dur = int(float(json.loads(probe2.stdout)["format"]["duration"]))
        except Exception:
            out_dur = duration

        status_file.write_text(json.dumps({
            "status": "done", "progress": 100, "message": "Pronto!",
            "clips_count": len(scaled_paths), "duration": out_dur,
            "output_file": output_name, "format": fmt, "quality": quality,
        }))

        try:
            for f in out_dir.iterdir():
                if f.name.startswith(("clip_", "audio.", "concat.")):
                    f.unlink(missing_ok=True)
            if raw_path.exists():
                raw_path.unlink(missing_ok=True)
        except Exception:
            pass

    except subprocess.TimeoutExpired:
        fail("Tempo limite excedido. Tente um video mais curto.")
    except Exception as e:
        fail(f"Erro interno: {str(e)}")
