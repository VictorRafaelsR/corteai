"""
CorteAI — Video Processor
Detects highlights via audio energy peaks + scene change, cuts and converts.
"""
import os, subprocess, json, struct, math, tempfile, shutil, sys
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
    """Return (yt_dlp_ok, ffmpeg_ok) — fast pre-flight check."""
    yt = run("yt-dlp --version", timeout=10)
    ff = run("ffmpeg -version", timeout=10)
    return yt.returncode == 0, ff.returncode == 0

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
        # -- PRE-FLIGHT: check tools --
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

        # -- STEP 1: Download --
        upd(5, "Baixando video do YouTube...")
        raw_path = out_dir / "raw.mp4"

        dl_cmd = (
            f'yt-dlp -f "bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best" '
            f'--merge-output-format mp4 '
            f'--no-playlist '
            f'--socket-timeout 30 '
            f'-o "{raw_path}" '
            f'"{url}"'
        )
        result = run(dl_cmd, timeout=300)

        if result.returncode != 0 or not raw_path.exists():
            upd(8, "Tentando download alternativo...")
            result2 = run(f'yt-dlp --no-playlist -f best -o "{raw_path}" "{url}"', timeout=300)
            if result2.returncode != 0 or not raw_path.exists():
                upd(9, "Atualizando yt-dlp e tentando novamente...")
                run(f"{sys.executable} -m pip install -U yt-dlp -q", timeout=120)
                result3 = run(f'yt-dlp --no-playlist -f best -o "{raw_path}" "{url}"', timeout=300)
                if result3.returncode != 0 or not raw_path.exists():
                    err_detail = (result3.stderr or result.stderr or "").strip()
                    if "Sign in" in err_detail or "login" in err_detail.lower():
                        fail("Este video requer login no YouTube. Use um video publico.")
                    elif "Private video" in err_detail:
                        fail("Video privado. Use um link de video publico.")
                    elif "Video unavailable" in err_detail:
                        fail("Video indisponivel ou removido do YouTube.")
                    else:
                        fail("Nao foi possivel baixar o video. Verifique se o link e publico e tente novamente.")
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

        # -- STEP 2: Extract audio as raw PCM --
        pcm_path = out_dir / "audio.raw"
        sample_rate = 8000
        run(f'ffmpeg -y -i "{raw_path}" -vn -acodec pcm_s16le -ar {sample_rate} -ac 1 -f s16le "{pcm_path}"', timeout=120)

        # -- STEP 3: Compute RMS energy per 0.1s chunk --
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

        # -- STEP 4: Find peaks --
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

        # -- STEP 5: Cut clips --
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
                f'��\�]H�
B��H�[��]��Y[Y[�]LL�
B�Y��\�]�^\��
H[��\�]��]

K����^�H�L���\�]˘\[�
�\�]
B��Y����\�]΂��Z[
��[���H���]�[^�Z\��\\���Y[ˈ[�H[H�Y[�Y�\�[�K��B��]\����\

�K��۝�\�[��\�H��ܛX]�\���Yˋ���B���KH�T
����[H[�ܛ�KB���H�ԓPU��US��˙�]
�]�ԓPU��US���ȝZ��ȗJB��H��ȝȗK��Ț�B����[Y�]�H�B��܈Y�[�[�[Y\�]J�\�]�N�����]H�]�\������[Y��Y��K�\
����[Wٚ[\�H
�����[O^��N��N��ܘ�W�ܚY�[�[�\�X�ܘ][�Z[�ܙX\�K����ܛ�^��N��H��
B�����YH
��ٙ�\Y�^HZH���H�	��]������[Wٚ[\�H�	��XΝ�X���XΘHXX�\�\�]�\�	�Ȟ����]H�
B��H�[�����Y[Y[�]LN
B�Y����]�^\��
H[����]��]

K����^�H�L����[Y�]˘\[�
���]
B��Y�����[Y�]΂��Z[
�\���[��۝�\�\���Y[�\�H��ܛX]���X�]Yˈ�B��]\����\
��[�[��������\\ˋ���B���KH�T
Έ�ۘ�][�]HKB��ۘ�]�\�H�]�\����ۘ�]����]�[��ۘ�]�\��ȊH\�����܈�[���[Y�]΂���ܚ]J���[H	�����\���J
_I���B���]]ۘ[YHH���ܝXZW�ڛؗ�YΎ_K�\
���]]�]H�]�\���]]ۘ[YB���ۘ�]��YH
��ٙ�\Y�^HY��ۘ�]\�Y�HZH���ۘ�]�\�H�	��X���H���]]�]H�
B��\�[H�[��ۘ�]��Y[Y[�]LN
B��Y����]]�]�^\��
H܈�]]�]��]

K����^�HL��Y���[Y�]΂��][���J��[Y�]��K�]]�]
B�[�N���Z[
�\���[��[�[^�\���Y[ˈ[�H�ݘ[Y[�K��B��]\����\
N��[�[^�[�ˋ���B���KH�T�ۙHKB�X�X[��\�H[���[Y�]�B��ؙL�H�[��ٙ��ؙH]�]ZY]\�[�ٛܛX]��ۈ\���ٛܛX]���]]�]H��[Y[�]L�
B��N���]�\�H[�
��]
��ۋ��Y��ؙL����]
Vș�ܛX]�Vș\�][ۈ�JJB�^�\^�\[ێ���]�\�H\�][ۂ���[�[�]HH��]\Ȏ��ۙH�����ܙ\�Ȏ�L��Y\��Y�H����۝�H����\����[���X�X[��\���\�][ۈ���]�\����]]ٚ[H���]]ۘ[YK���ܛX]���]�B��]\�ٚ[K�ܚ]W�^
��ۋ�[\��[�[�]JJB���N���܈�[��]�\��]\�\�
N��Y����[YK��\���]

��\ȋ���[Yȋ�]Y[ˈ���ۘ�]��JN����[�[��Z\��[�����U�YJB�Y��]��]�^\��
N���]��]�[�[��Z\��[�����U�YJB�^�\^�\[ێ��\��^�\�X����\�˕[Y[�]^\�Y���Z[
�[\�[Z]H^�YYˈ��Y[��H�\�]Z]�ۙ�ˈ[�H[H�Y[�XZ\��\�ˈ�B�^�\^�\[ۈ\�N���Z[
��\���[�\��Έ���J_H�B
