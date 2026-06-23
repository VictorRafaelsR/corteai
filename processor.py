"""
CorteAI — Video Processor
Detects highlights via audio energy peaks + scene change, cuts and converts.
"""
import os, subprocess, json, struct, math, tempfile, shutil, sys, base64
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

FORMAT_SETTINGS = {
    "tiktok":    {"w": 1080, "h": 1920, "label": "TikTok 9:16"},
    "instagram": {"w": 1080, "h": 1080, "label": "Instagram 1:1"},
    "youtube":   {"w": 1920, "h": 1080, "label": "YouTube 16:9"},
}

def run(cmd, timeout=300, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, **kw)

def get_cookies_arg():
    """Write YouTube cookies from env var to temp file, return --cookies arg."""
    cookies_b64 = os.environ.get('YOUTUBE_COOKIES_B64', '')
    if not cookies_b64:
        return ''
    cookies_path = '/tmp/yt_cookies.txt'
    try:
        if not os.path.exists(cookies_path):
            decoded = base64.b64decode(cookies_b64).decode('utf-8')
            with open(cookies_path, 'w') as f:
                f.write(decoded)
        return f'--cookies "{cookies_path}"'
    except Exception:
        return ''

def check_tools():
    yt = run("yt-dlp --version", timeout=10)
    ff = run("ffmpeg -version", timeout=10)
    return yt.returncode == 0, ff.returncode == 0

def process_video(job_id, url, fmt, duration, clips, player):
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
            fail("ffmpeg nao encontrado no servidor. Contate o suporte.")
            return
        if not yt_ok:
            upd(3, "Atualizando yt-dlp...")
            run(f"{sys.executable} -m pip install -U yt-dlp -q", timeout=120)
            yt_ok, _ = check_tools()
            if not yt_ok:
                fail("yt-dlp nao encontrado no servidor. Contate o suporte.")
                return

        upd(5, "Baixando video do YouTube...")
        raw_path = out_dir / "raw.mp4"
        cookies_arg = get_cookies_arg()
        fmt_arg = '"bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best"'

        dl_cmd = (
            f'yt-dlp {cookies_arg} '
            f'--extractor-args "youtube:player_client=android,web" '
            f'-f {fmt_arg} '
            f'--merge-output-format mp4 --no-playlist --socket-timeout 30 '
            f'-o "{raw_path}" "{url}"'
        )
        result = run(dl_cmd, timeout=300)

        if result.returncode != 0 or not raw_path.exists():
            upd(7, "Tentando metodo alternativo...")
            dl_cmd2 = (
                f'yt-dlp {cookies_arg} '
                f'--extractor-args "youtube:player_client=tv_embedded,web" '
                f'-f {fmt_arg} '
                f'--merge-output-format mp4 --no-playlist '
                f'-o "{raw_path}" "{url}"'
            )
            result = run(dl_cmd2, timeout=300)

        if result.returncode != 0 or not raw_path.exists():
            upd(9, "Atualizando yt-dlp e tentando novamente...")
            run(f"{sys.executable} -m pip install -U yt-dlp -q", timeout=120)
            dl_cmd3 = (
                f'yt-dlp {cookies_arg} '
                f'--extractor-args "youtube:player_client=android" '
                f'--no-playlist -f best '
                f'-o "{raw_path}" "{url}"'
            )
            result = run(dl_cmd3, timeout=300)

            if result.returncode != 0 or not raw_path.exists():
                err_detail = (result.stderr or result.stdout or "").strip()
                if "Private video" in err_detail:
                    fail("Video privado. Apenas o dono pode acessar este video.")
                elif "Video unavailable" in err_detail or "is not available" in err_detail:
                    fail("Video indisponivel ou removido do YouTube.")
                elif "confirm your age" in err_detail.lower() or "Sign in" in err_detail:
                    if not cookies_arg:
                        fail("Este video requer autenticacao. Configure YOUTUBE_COOKIES_B64 no Railway para acessar videos restritos.")
                    else:
                        fail("Nao foi possivel baixar o video mesmo com autenticacao.")
                else:
                    fail("Nao foi possivel baixar o video. Verifique se o link e valido.")
                return

        if raw_path.stat().st_size < 10000:
            fail("Arquivo baixado invalido (muito pequeno). Tente outro video.")
            return

        probe = run(f'ffprobe -v quiet -print_format json -show_format "{raw_path}"', timeout=30)
        try:
            vid_dur = float(json.loads(probe.stdout)["format"]["duration"])
        except Exception:
            vid_dur = 600.0

        if vid_dur < 5:
            fail("Video muito curto (menos de 5 segundos).")
            return

        upd(20, "Analisando audio para detectar momentos de destaque...")

        pcm_path = out_dir / "audio.raw"
        sample_rate = 8000
        run(f'ffmpeg -y -i "{raw_path}" -vn -acodec pcm_s16le -ar {sample_rate} -ac 1 -f s16le "{pcm_path}"', timeout=120)

        upd(35, "Calculando energia do audio por segmento...")
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

        upd(60, "Cortando e convertendo clipes...")

        fs = FORMAT_SETTINGS.get(fmt, FORMAT_SETTINGS["tiktok"])
        tw, th = fs["w"], fs["h"]
        scale_filter = (
            f"scale={tw}:{th}:force_original_aspect_ratio=increase,"
            f"crop={tw}:{th},"
            f"format=yuv420p"
        )
        scaled_paths = []
        for idx, t in enumerate(peaks):
            start = max(0, t - clip_dur / 2)
            if start + clip_dur > vid_dur:
                start = max(0, vid_dur - clip_dur)
            sc_path = out_dir / f"scaled_{idx:02d}.mp4"
            sc_cmd = (
                f'ffmpeg -y -ss {start:.2f} -i "{raw_path}" '
                f'-t {clip_dur:.2f} '
                f'-vf "{scale_filter}" '
                f'-c:v libx264 -preset ultrafast -threads 1 -crf 28 -c:a aac '
                f'"{sc_path}"'
            )
            r = run(sc_cmd, timeout=180)
            if not (sc_path.exists() and sc_path.stat().st_size > 1000):
                sc_cmd2 = (
                    f'ffmpeg -y -ss {start:.2f} -i "{raw_path}" '
                    f'-t {clip_dur:.2f} '
                    f'-vf "{scale_filter}" '
                    f'"{sc_path}"'
                )
                r = run(sc_cmd2, timeout=180)
            if sc_path.exists() and sc_path.stat().st_size > 1000:
                scaled_paths.append(sc_path)

        if not scaled_paths:
            fail("Erro ao gerar clipes. Tente um video diferente.")
            return

        upd(88, "Juntando todos os clipes...")

        concat_list = out_dir / "concat.txt"
        with open(concat_list, "w") as f:
            for sp in scaled_paths:
                f.write(f"file '{sp.resolve()}'\n")

        output_name = f"corteai_{job_id[:8]}.mp4"
        output_path = out_dir / output_name

        concat_cmd = (
            f'ffmpeg -y -f concat -safe 0 -i "{concat_list}" '
            f'-c copy "{output_path}"'
        )
        result = run(concat_cmd, timeout=180)

        if not output_path.exists() or output_path.stat().st_size < 1000:
            if scaled_paths:
                shutil.copy(scaled_paths[0], output_path)
            else:
                fail("Erro ao finalizar o video. Tente novamente.")
                return

        upd(98, "Finalizando...")

        actual_clips = len(scaled_paths)
        probe2 = run(f'ffprobe -v quiet -print_format json -show_format "{output_path}"', timeout=30)
        try:
            out_dur = int(float(json.loads(probe2.stdout)["format"]["duration"]))
        except Exception:
            out_dur = duration

        final_data = {
            "status": "done",
            "progress": 100,
            "message": "Pronto!",
            "clips_count": actual_clips,
            "duration": out_dur,
            "output_file": output_name,
            "format": fmt,
        }
        status_file.write_text(json.dumps(final_data))

        try:
            for f in out_dir.iterdir():
                if f.name.startswith(("scaled_", "audio.", "concat.")):
                    f.unlink(missing_ok=True)
            if raw_path.exists():
                raw_path.unlink(missing_ok=True)
        except Exception:
            pass

    except subprocess.TimeoutExpired:
        fail("Tempo limite excedido. O video pode ser muito longo. Tente um video mais curto.")
    except Exception as e:
        fail(f"Erro interno: {str(e)}")
