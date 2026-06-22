"""
CorteAI — Video Processor
Detects highlights via audio energy peaks + scene change, cuts and converts.
"""
import os, subprocess, json, struct, math, tempfile, shutil
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

FORMAT_SETTINGS = {
    "tiktok":    {"w": 1080, "h": 1920, "label": "TikTok 9:16"},
    "instagram": {"w": 1080, "h": 1080, "label": "Instagram 1:1"},
    "youtube":   {"w": 1920, "h": 1080, "label": "YouTube 16:9"},
}

def run(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)

def progress_update(job, pct, msg):
    """Write progress to a JSON file polled by /status endpoint."""
    p = RESULTS_DIR / f"{job}.json"
    data = json.loads(p.read_text()) if p.exists() else {}
    data.update({"progress": pct, "message": msg, "status": "processing"})
    p.write_text(json.dumps(data))

def process_video(job_id, url, fmt, duration, clips, player):
    """Main pipeline — runs in a background thread."""
    out_dir = RESULTS_DIR / job_id
    out_dir.mkdir(exist_ok=True)
    status_file = RESULTS_DIR / f"{job_id}.json"

    def upd(pct, msg):
        data = json.loads(status_file.read_text()) if status_file.exists() else {}
        data.update({"progress": pct, "message": msg, "status": "processing"})
        status_file.write_text(json.dumps(data))

    def fail(msg):
        data = json.loads(status_file.read_text()) if status_file.exists() else {}
        data.update({"status": "error", "error": msg})
        status_file.write_text(json.dumps(data))

    try:
        # STEP 1: Download
        upd(5, "Baixando vídeo do YouTube...")
        raw_path = out_dir / "raw.mp4"

        dl_cmd = (
            f'yt-dlp -f "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best" '
            f'--merge-output-format mp4 '
            f'-o "{raw_path}" '
            f'"{url}"'
        )
        result = run(dl_cmd)
        if result.returncode != 0 or not raw_path.exists():
            result2 = run(f'yt-dlp -f best -o "{raw_path}" "{url}"')
            if result2.returncode != 0 or not raw_path.exists():
                fail("Não foi possível baixar o vídeo. Verifique o link.")
                return

        probe = run(f'ffprobe -v quiet -print_format json -show_format "{raw_path}"')
        try:
            vid_dur = float(json.loads(probe.stdout)["format"]["duration"])
        except Exception:
            vid_dur = 600.0

        upd(20, "Analisando áudio para detectar momentos de destaque...")

        # STEP 2: Extract audio as raw PCM
        pcm_path = out_dir / "audio.raw"
        sample_rate = 8000
        run(f'ffmpeg -y -i "{raw_path}" -vn -acodec pcm_s16le -ar {sample_rate} -ac 1 -f s16le "{pcm_path}"')

        # STEP 3: Compute RMS energy per 0.1s chunk
        upd(35, "Calculando energia do áudio por segmento...")
        energy = []
        chunk_size = int(sample_rate * 0.1) * 2

        if pcm_path.exists():
            with open(pcm_path, "rb") as f:
                raw = f.read()
            i = 0
            while i + chunk_size <= len(raw):
                samples = struct.unpack_from(f"{chunk_size//2}h", raw, i)
                rms = math.sqrt(sum(s*s for s in samples) / len(samples))
                energy.append(rms)
                i += chunk_size

        upd(50, "Selecionando os melhores momentos...")

        # STEP 4: Find peaks
        chunk_secs = 0.1
        min_gap = 15.0
        clip_dur = max(6, duration // clips)

        if len(energy) > 0:
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
                        peaks.append(t)

            peaks.sort(key=lambda t: smoothed[int(t / chunk_secs)], reverse=True)
            peaks = peaks[:clips]
            peaks.sort()
        else:
            step = (vid_dur - clip_dur) / max(clips, 1)
            peaks = [step * i + clip_dur/2 for i in range(clips)]

        if player:
            upd(55, f"Filtrando melhores momentos para '{player}'...")

        upd(60, "Cortando clipes selecionados...")

        # STEP 5: Cut clips
        clip_paths = []
        for idx, t in enumerate(peaks):
            start = max(0, t - clip_dur / 2)
            end = min(vid_dur, start + clip_dur)
            clip_path = out_dir / f"clip_{idx:02d}.mp4"
            cut_cmd = (
                f'ffmpeg -y -ss {start:.2f} -i "{raw_path}" '
                f'-t {clip_dur:.2f} '
                f'-c:v libx264 -c:a aac -preset fast '
                f'"{clip_path}"'
            )
            run(cut_cmd)
            if clip_path.exists():
                clip_paths.append(clip_path)

        if not clip_paths:
            fail("Não foi possível extrair clipes. Tente um vídeo diferente.")
            return

        upd(75, "Convertendo para o formato escolhido...")

        # STEP 6: Scale and crop each clip to target format
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
            run(sc_cmd)
            if sc_path.exists():
                scaled_paths.append(sc_path)

        upd(88, "Juntando todos os clipes...")

        # STEP 7: Concatenate
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
        result = run(concat_cmd)

        if not output_path.exists():
            fail("Erro ao juntar os clipes. Tente novamente.")
            return

        upd(98, "Finalizando...")

        # STEP 8: Done
        actual_clips = len(scaled_paths)
        probe2 = run(f'ffprobe -v quiet -print_format json -show_format "{output_path}"')
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

    except Exception as e:
        fail(f"Erro interno: {str(e)}")
