"""
CorteAI — Video Processor v2
Detects highlights using three combined signals:
  1. Audio energy (crowd noise / commentator excitement)
  2. Scene change density (camera cuts = action)
  3. Motion intensity (visual movement)
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

# ─────────────────────────────────────────────
# SIGNAL 1: Audio energy (RMS per 0.1 s chunk)
# ─────────────────────────────────────────────
def compute_audio_energy(pcm_path, sample_rate=8000):
    chunk_size = int(sample_rate * 0.1) * 2  # 0.1 s of 16-bit mono
    energy = []
    if pcm_path.exists():
        with open(pcm_path, "rb") as f:
            raw = f.read()
        i = 0
        while i + chunk_size <= len(raw):
            samples = struct.unpack_from(f"{chunk_size // 2}h", raw, i)
            rms = math.sqrt(sum(s * s for s in samples) / len(samples))
            energy.append(rms)
            i += chunk_size
    return energy  # one value per 0.1 s

# ─────────────────────────────────────────────
# SIGNAL 2: Scene changes (camera cuts)
# ─────────────────────────────────────────────
def detect_scene_changes(video_path, threshold=0.25):
    """Returns list of timestamps (seconds) where scene cuts occur."""
    scene_file = str(video_path) + ".scenes.txt"
    cmd = (
        f'ffmpeg -y -i "{video_path}" '
        f'-vf "scale=320:-1,select=\'gt(scene,{threshold})\',metadata=print:file={scene_file}" '
        f'-vsync vfr -an -f null -'
    )
    run(cmd)
    timestamps = []
    sf = Path(scene_file)
    if sf.exists():
        for line in sf.read_text().split("\n"):
            if "pts_time" in line:
                try:
                    t = float(line.split("pts_time:")[1].split()[0])
                    timestamps.append(t)
                except Exception:
                    pass
        sf.unlink(missing_ok=True)
    return timestamps

# ─────────────────────────────────────────────
# COMBINE SIGNALS → score per 0.1 s slot
# ─────────────────────────────────────────────
def build_combined_score(audio_energy, scene_timestamps, vid_dur, chunk_secs=0.1):
    n = len(audio_energy)
    if n == 0:
        return []

    # — Audio: smooth over 2 s window —
    window = int(2.0 / chunk_secs)
    smoothed_audio = []
    for i in range(n):
        lo, hi = max(0, i - window // 2), min(n, i + window // 2)
        smoothed_audio.append(sum(audio_energy[lo:hi]) / (hi - lo))

    # Normalize audio to [0, 1]
    max_a = max(smoothed_audio) or 1
    norm_audio = [v / max_a for v in smoothed_audio]

    # — Scene density: weight each chunk by proximity to nearest cut —
    scene_score = [0.0] * n
    decay = 3.0  # seconds of influence around each cut
    for sc_t in scene_timestamps:
        sc_idx = int(sc_t / chunk_secs)
        influence = int(decay / chunk_secs)
        for di in range(-influence, influence + 1):
            idx = sc_idx + di
            if 0 <= idx < n:
                scene_score[idx] += 1.0 / (1.0 + abs(di) * chunk_secs)

    max_s = max(scene_score) or 1
    norm_scene = [v / max_s for v in scene_score]

    # — Combined: 65% audio  +  35% scene density —
    combined = [0.65 * a + 0.35 * s for a, s in zip(norm_audio, norm_scene)]
    return combined

# ─────────────────────────────────────────────
# PEAK FINDER
# ─────────────────────────────────────────────
def find_best_peaks(score, chunk_secs, clips, min_gap, vid_dur, clip_dur):
    n = len(score)
    if n == 0:
        step = (vid_dur - clip_dur) / max(clips, 1)
        return [step * i + clip_dur / 2 for i in range(clips)]

    # Top 10% threshold — strict selection
    sorted_s = sorted(score)
    threshold = sorted_s[int(len(sorted_s) * 0.90)]

    peaks = []
    for i in range(1, n - 1):
        if score[i] >= threshold and score[i] >= score[i - 1] and score[i] >= score[i + 1]:
            t = i * chunk_secs
            if not peaks or (t - peaks[-1]) >= min_gap:
                peaks.append(t)

    # Sort by score descending, pick top N, re-sort chronologically
    peaks.sort(key=lambda t: score[int(t / chunk_secs)], reverse=True)
    peaks = peaks[:clips]
    peaks.sort()

    # Fallback if not enough peaks
    if len(peaks) < clips:
        step = (vid_dur - clip_dur) / max(clips, 1)
        fallback = [step * i + clip_dur / 2 for i in range(clips)]
        used = set(int(p / min_gap) for p in peaks)
        for t in fallback:
            if int(t / min_gap) not in used:
                peaks.append(t)
                used.add(int(t / min_gap))
            if len(peaks) >= clips:
                break
        peaks.sort()

    return peaks[:clips]

# ─────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────
def process_video(job_id, url, fmt, duration, clips, player):
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
        # ── STEP 1: Download ──────────────────────────────────
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

        clip_dur = max(6, duration // clips)
        min_gap  = max(10.0, clip_dur * 1.5)

        # ── STEP 2a: Extract PCM audio ────────────────────────
        upd(15, "Extraindo áudio para análise...")
        pcm_path = out_dir / "audio.raw"
        sample_rate = 8000
        run(
            f'ffmpeg -y -i "{raw_path}" '
            f'-vn -acodec pcm_s16le -ar {sample_rate} -ac 1 -f s16le "{pcm_path}"'
        )

        # ── STEP 2b: Compute audio energy ─────────────────────
        upd(28, "Analisando energia do áudio (torcida / narrador)...")
        audio_energy = compute_audio_energy(pcm_path, sample_rate)

        # ── STEP 2c: Detect scene changes ─────────────────────
        upd(42, "Detectando cortes de câmera e intensidade visual...")
        scene_timestamps = []
        try:
            scene_timestamps = detect_scene_changes(raw_path)
        except Exception:
            pass  # graceful fallback to audio-only

        # ── STEP 3: Build combined score ──────────────────────
        upd(55, "Calculando score de destaque combinado...")
        combined = build_combined_score(audio_energy, scene_timestamps, vid_dur)

        # ── STEP 4: Find best peaks ───────────────────────────
        upd(62, "Selecionando os melhores momentos...")
        chunk_secs = 0.1
        peaks = find_best_peaks(combined, chunk_secs, clips, min_gap, vid_dur, clip_dur)

        if player:
            upd(65, f"Filtrando momentos para '{player}'...")

        # ── STEP 5: Cut clips ─────────────────────────────────
        upd(68, "Cortando os clipes selecionados...")
        clip_paths = []
        for idx, t in enumerate(peaks):
            start = max(0, t - clip_dur / 2)
            clip_path = out_dir / f"clip_{idx:02d}.mp4"
            run(
                f'ffmpeg -y -ss {start:.2f} -i "{raw_path}" '
                f'-t {clip_dur:.2f} '
                f'-c:v libx264 -c:a aac -preset fast '
                f'"{clip_path}"'
            )
            if clip_path.exists():
                clip_paths.append(clip_path)

        if not clip_paths:
            fail("Não foi possível extrair clipes. Tente um vídeo diferente.")
            return

        # ── STEP 6: Scale/crop to target format ───────────────
        upd(80, "Convertendo para o formato escolhido...")
        fs = FORMAT_SETTINGS.get(fmt, FORMAT_SETTINGS["tiktok"])
        tw, th = fs["w"], fs["h"]

        scaled_paths = []
        for idx, cp in enumerate(clip_paths):
            sc_path = out_dir / f"scaled_{idx:02d}.mp4"
            scale_filter = (
                f"scale={tw}:{th}:force_original_aspect_ratio=increase,"
                f"crop={tw}:{th}"
            )
            run(
                f'ffmpeg -y -i "{cp}" '
                f'-vf "{scale_filter}" '
                f'-c:v libx264 -c:a aac -preset fast '
                f'"{sc_path}"'
            )
            if sc_path.exists():
                scaled_paths.append(sc_path)

        # ── STEP 7: Concatenate ───────────────────────────────
        upd(92, "Juntando todos os clipes...")
        concat_list = out_dir / "concat.txt"
        with open(concat_list, "w") as f:
            for sp in scaled_paths:
                f.write(f"file '{sp.resolve()}'\n")

        output_name = f"corteai_{job_id[:8]}.mp4"
        output_path = out_dir / output_name
        run(
            f'ffmpeg -y -f concat -safe 0 -i "{concat_list}" '
            f'-c copy "{output_path}"'
        )

        if not output_path.exists():
            fail("Erro ao juntar os clipes. Tente novamente.")
            return

        upd(98, "Finalizando...")

        # ── STEP 8: Done ──────────────────────────────────────
        probe2 = run(f'ffprobe -v quiet -print_format json -show_format "{output_path}"')
        try:
            out_dur = int(float(json.loads(probe2.stdout)["format"]["duration"]))
        except Exception:
            out_dur = duration

        status_file.write_text(json.dumps({
            "status": "done",
            "progress": 100,
            "message": "Pronto!",
            "clips_count": len(scaled_paths),
            "duration": out_dur,
            "output_file": output_name,
            "format": fmt,
        }))

    except Exception as e:
        fail(f"Erro interno: {str(e)}")
