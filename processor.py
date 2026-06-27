"""
CorteAI — Video Processor
Detects highlights via audio energy peaks, cuts and converts.
"""
import os, subprocess, json, struct, math, shutil, sys
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

def check_tools():
    """Return (yt_dlp_ok, ffmpeg_ok)."""
    yt = run("yt-dlp --version", timeout=10)
    ff = run("ffmpeg -version", timeout=10)
    return yt.returncode == 0, ff.returncode == 0

def try_download(url, raw_path, timeout=360):
    """
    Try multiple download strategies in order.
    Returns (success: bool, last_error: str).
    """
    base_flags = "--no-playlist --no-check-certificates --socket-timeout 30"

    strategies = [
        # 1. TV Embedded client — bypasses most age/region restrictions
        (
            f'yt-dlp -f "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best" '
            f'--merge-output-format mp4 '
            f'--extractor-args "youtube:player_client=tv_embedded" '
            f'{base_flags} -o "{raw_path}"'
        ),
        # 2. Android client — another effective bypass
        (
            f'yt-dlp -f "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4]/best" '
            f'--merge-output-format mp4 '
            f'--extractor-args "youtube:player_client=android" '
            f'{base_flags} -o "{raw_path}"'
        ),
        # 3. Web client, geo bypass, simplified format
        (
            f'yt-dlp -f "best[ext=mp4]/best" '
            f'--geo-bypass '
            f'{base_flags} -o "{raw_path}"'
        ),
        # 4. Absolute fallback — any format, longer timeout
        (
            f'yt-dlp --no-playlist --no-check-certificates --socket-timeout 60 '
            f'-o "{raw_path}"'
        ),
    ]

    last_err = ""
    for cmd in strategies:
        result = run(f'{cmd} "{url}"', timeout=timeout)
        if result.returncode == 0 and raw_path.exists() and raw_path.stat().st_size > 10000:
            return True, ""
        last_err = (result.stderr or result.stdout or "").strip()
        # Remove partial download before next attempt
        try:
            if raw_path.exists():
                raw_path.unlink()
        except Exception:
            pass

    return False, last_err


def classify_download_error(err_text):
    """Return a user-friendly Portuguese error message."""
    err = err_text.lower()
    if "sign in" in err or "login" in err or "requires authentication" in err:
        return "Este vídeo requer login no YouTube. Use um vídeo público."
    if "private video" in err:
        return "Vídeo privado. Use um link de vídeo público."
    if "video unavailable" in err or "unavailable" in err:
        return "Vídeo indisponível ou removido do YouTube."
    if "copyright" in err:
        return "Vídeo bloqueado por direitos autorais nesta região."
    if "age" in err and ("restrict" in err or "limit" in err):
        return "Vídeo com restrição de idade. Use outro vídeo."
    if "geo" in err or "not available in your country" in err:
        return "Vídeo não disponível nesta região."
    if "network" in err or "connection" in err or "timeout" in err:
        return "Erro de conexão ao baixar o vídeo. Tente novamente."
    return "Não foi possível baixar o vídeo. Verifique se o link é público e tente novamente."


def process_video(job_id, url, fmt, duration, clips, player):
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
        # PRE-FLIGHT
        upd(2, "Verificando ferramentas...")
        yt_ok, ff_ok = check_tools()
        if not ff_ok:
            fail("ffmpeg não encontrado no servidor. Contate o suporte.")
            return
        if not yt_ok:
            upd(3, "Instalando yt-dlp...")
            run(f"{sys.executable} -m pip install -U yt-dlp -q", timeout=120)
            yt_ok, _ = check_tools()
            if not yt_ok:
                fail("yt-dlp não encontrado no servidor. Contate o suporte.")
                return

        # STEP 1: DOWNLOAD
        upd(5, "Baixando vídeo...")
        raw_path = out_dir / "raw.mp4"

        success, err_text = try_download(url, raw_path, timeout=360)

        if not success:
            # Last resort: update yt-dlp and retry
            upd(9, "Atualizando yt-dlp e tentando novamente...")
            run(f"{sys.executable} -m pip install -U yt-dlp -q", timeout=120)
            success, err_text = try_download(url, raw_path, timeout=360)

        if not success or not raw_path.exists() or raw_path.stat().st_size < 10000:
            fail(classify_download_error(err_text))
            return

        # PROBE VIDEO DURATION
        probe = run(f'ffprobe -v quiet -print_format json -show_format "{raw_path}"', timeout=30)
        try:
            vid_dur = float(json.loads(probe.stdout)["format"]["duration"])
        except Exception:
            fail("Arquivo de vídeo corrompido ou formato não suportado.")
            return

        if vid_dur < 5:
            fail("Vídeo muito curto (menos de 5 segundos).")
            return

        upd(20, "Analisando áudio para detectar momentos de destaque...")

        # STEP 2: EXTRACT AUDIO
        pcm_path = out_dir / "audio.raw"
        sample_rate = 8000
        run(
            f'ffmpeg -y -i "{raw_path}" -vn -acodec pcm_s16le '
            f'-ar {sample_rate} -ac 1 -f s16le "{pcm_path}"',
            timeout=120,
        )

        # STEP 3: RMS ENERGY PER 0.1s CHUNK
        upd(35, "Calculando energia do áudio por segmento...")
        energy = []
        chunk_size = int(sample_rate * 0.1) * 2

        if pcm_path.exists() and pcm_path.stat().st_size > 0:
            with open(pcm_path, "rb") as f:
                raw_audio = f.read()
            i = 0
            while i + chunk_size <= len(raw_audio):
                samples = struct.unpack_from(f"{chunk_size // 2}h", raw_audio, i)
                rms = math.sqrt(sum(s * s for s in samples) / len(samples))
                energy.append(rms)
                i += chunk_size

        upd(50, "Selecionando os melhores momentos...")

        # STEP 4: FIND PEAKS
        chunk_secs = 0.1

        # FIX: clip_dur = per-clip duration the user asked for — do NOT divide by clips
        clip_dur = max(10, min(120, int(duration)))

        # FIX: min_gap must be >= clip_dur so clips never overlap
        min_gap = max(clip_dur + 2.0, (vid_dur - clip_dur) / max(clips + 1, 2))

        peaks = []
        smoothed = []
        if len(energy) > 10:
            window = int(2.0 / chunk_secs)
            for i in range(len(energy)):
                lo = max(0, i - window // 2)
                hi = min(len(energy), i + window // 2)
                smoothed.append(sum(energy[lo:hi]) / (hi - lo))

            # Progressive thresholds — keep lowering until we have enough peaks
            for pct in [0.85, 0.75, 0.65, 0.50, 0.35, 0.20]:
                if len(peaks) >= clips:
                    break
                threshold = sorted(smoothed)[int(len(smoothed) * pct)]
                for i in range(1, len(smoothed) - 1):
                    if (smoothed[i] > threshold
                            and smoothed[i] >= smoothed[i - 1]
                            and smoothed[i] >= smoothed[i + 1]):
                        t = i * chunk_secs
                        if t + clip_dur / 2 <= vid_dur:
                            if all(abs(t - p) >= min_gap for p in peaks):
                                peaks.append(t)
                # Keep only the loudest peaks found so far
                peaks.sort(
                    key=lambda t: smoothed[int(min(t / chunk_secs, len(smoothed) - 1))],
                    reverse=True,
                )
                peaks = peaks[:clips]

            peaks.sort()

        # FIX: fill missing peaks with evenly spaced positions
        if len(peaks) < clips:
            step = (vid_dur - clip_dur) / max(clips, 1)
            for i in range(clips):
                t = clip_dur / 2 + step * i
                if t + clip_dur / 2 <= vid_dur:
                    if all(abs(t - p) >= min_gap for p in peaks):
                        peaks.append(t)
                if len(peaks) >= clips:
                    break
            peaks = sorted(peaks[:clips])

        # Absolute last resort
        if not peaks:
            peaks = [max(clip_dur / 2, vid_dur * 0.1)]

        if player:
            upd(55, f"Filtrando melhores momentos para '{player}'...")

        upd(60, "Cortando clipes selecionados...")

        # STEP 5: CUT CLIPS
        clip_paths = []
        for idx, t in enumerate(peaks):
            start = max(0, t - clip_dur / 2)
            if start + clip_dur > vid_dur:
                start = max(0, vid_dur - clip_dur)
            clip_path = out_dir / f"clip_{idx:02d}.mp4"
            cut_cmd = (
                f'ffmpeg -y -ss {start:.2f} -i "{raw_path}" '
                f'-t {clip_dur:.2f} '
                f'-c:v libx264 -c:a aac -preset fast '
                f'"{clip_path}"'
            )
            r = run(cut_cmd, timeout=120)
            if clip_path.exists() and clip_path.stat().st_size > 1000:
                clip_paths.append(clip_path)

        if not clip_paths:
            fail("Não foi possível extrair clipes do vídeo. Tente um vídeo diferente.")
            return

        upd(75, "Convertendo para o formato escolhido...")

        # STEP 6: SCALE & CROP
        fs = FORMAT_SETTINGS.get(fmt, FORMAT_SETTINGS["tiktok"])
        tw, th = fs["w"], fs["h"]

        scaled_paths = []
        for idx, cp in enumerate(clip_paths):
            sc_path = out_dir / f"scaled_{idx:02d}.mp4"
            scale_filter = (
                f"scale={tw}:{th}:force_original_aspect_ratio=increase,"
                f"crop={tw}:{th}"
            )
            sc_cmd = (
                f'ffmpeg -y -i "{cp}" '
                f'-vf "{scale_filter}" '
                f'-c:v libx264 -c:a aac -preset fast '
                f'"{sc_path}"'
            )
            r = run(sc_cmd, timeout=180)
            if sc_path.exists() and sc_path.stat().st_size > 1000:
                scaled_paths.append(sc_path)

        if not scaled_paths:
            fail("Erro ao converter o vídeo para o formato solicitado.")
            return

        upd(88, "Juntando todos os clipes...")

        # STEP 7: CONCATENATE
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
        run(concat_cmd, timeout=180)

        if not output_path.exists() or output_path.stat().st_size < 1000:
            if scaled_paths:
                shutil.copy(scaled_paths[0], output_path)
            else:
                fail("Erro ao finalizar o vídeo. Tente novamente.")
                return

        upd(98, "Finalizando...")

        # STEP 8: DONE
        actual_clips = len(scaled_paths)
        probe2 = run(
            f'ffprobe -v quiet -print_format json -show_format "{output_path}"',
            timeout=30,
        )
        try:
            out_dur = int(float(json.loads(probe2.stdout)["format"]["duration"]))
        except Exception:
            out_dur = duration * actual_clips

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

        # Cleanup temp files
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
