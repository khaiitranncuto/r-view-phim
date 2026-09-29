import os
import re
import subprocess
import shutil
import zipfile
import pathlib
import concurrent.futures
from tts_engine import (
    create_srt,
    generate_all_tts,
    get_audio_duration,
    parse_sentences_and_time_ranges,
    split_text_into_sentences,
)
def get_ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"
def get_video_info(input_video_path):
    """Lay thong tin video (duration, width, height) bang FFmpeg thay vi MoviePy."""
    ffmpeg_exe = get_ffmpeg_exe()
    result = subprocess.run(
        [ffmpeg_exe, "-i", input_video_path, "-hide_banner"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    stderr = result.stderr
    
    # Parse duration
    duration = 0.0
    dur_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", stderr)
    if dur_match:
        h, m, s, cs = dur_match.groups()
        duration = int(h)*3600 + int(m)*60 + int(s) + int(cs)/100.0
    
    # Parse resolution (tranh match sai voi SAR/DAR)
    w_vid, h_vid = 1920, 1080
    res_match = re.search(r"(\d{2,5})x(\d{2,5})[\s,\[]", stderr)
    if res_match:
        w_vid, h_vid = int(res_match.group(1)), int(res_match.group(2))
    
    return duration, w_vid, h_vid
def _pause_duration_for_text(text):
    """Return a small, controlled pause without creating robotic one-second gaps."""
    text = (text or "").rstrip()
    if text.endswith(("...", "â€¦")):
        return 0.28
    if text.endswith((".", "!", "?", ";")):
        return 0.22
    return 0.12


def build_visual_edit_plan(
    durations,
    starts,
    ends,
    video_duration,
    min_playback_speed=0.90,
    fallback_starts=None,
):
    """Build an audio-led edit decision list for explicit Gemini ranges.

    Every narration item owns an independent source anchor. A long previous
    sentence must never push the next item past its declared action. When a
    visual range is slightly shorter than its narration, use at most a subtle
    slowdown and then continue into neighbouring footage instead of cloning a
    still frame.
    """
    plan = []
    video_duration = max(0.0, float(video_duration))
    min_playback_speed = min(1.0, max(0.50, float(min_playback_speed)))

    for index, raw_duration in enumerate(durations):
        output_duration = max(0.05, float(raw_duration))
        raw_start = starts[index] if starts and index < len(starts) else None
        if raw_start is None and fallback_starts and index < len(fallback_starts):
            raw_start = fallback_starts[index]
        raw_end = ends[index] if ends and index < len(ends) else None
        has_range = raw_start is not None and raw_end is not None and float(raw_end) > float(raw_start)

        if has_range:
            range_start = min(max(float(raw_start), 0.0), video_duration)
            range_end = min(max(float(raw_end), range_start), video_duration)
            range_duration = range_end - range_start
            if output_duration > range_duration:
                # Keep the complete declared action and add only the minimum
                # neighbouring footage needed to avoid an obvious slow motion.
                source_duration = min(output_duration, max(range_duration, output_duration * min_playback_speed))
            else:
                source_duration = output_duration
            source_start = range_start
        else:
            source_start = min(max(float(raw_start or 0.0), 0.0), video_duration)
            source_duration = output_duration

        source_duration = min(source_duration, video_duration) if video_duration else 0.0
        # Close to the physical end there is no footage to extend into. Shift
        # backwards so the output still consists entirely of real frames.
        if video_duration and source_start + source_duration > video_duration:
            source_start = max(0.0, video_duration - source_duration)

        playback_speed = source_duration / output_duration if output_duration else 1.0
        plan.append({
            "index": index,
            "source_start": source_start,
            "source_duration": source_duration,
            "output_duration": output_duration,
            "playback_speed": playback_speed,
            "has_range": has_range,
        })
    return plan


def trim_silence_from_audio(input_path, output_path, pause_duration=0.15):
    """Trim only the outer TTS silence, preserve pauses inside speech, then add a short pause."""
    ffmpeg_exe = get_ffmpeg_exe()
    # Reversing before the second silenceremove trims the tail only. A single
    # silenceremove with stop_periods could cut at a natural pause in the middle.
    audio_filter = (
        "silenceremove=start_periods=1:start_duration=0.02:start_threshold=-42dB,"
        "areverse,"
        "silenceremove=start_periods=1:start_duration=0.08:start_threshold=-42dB,"
        "areverse,"
        f"apad=pad_dur={max(0.0, float(pause_duration)):.3f}"
    )
    result = subprocess.run([
        ffmpeg_exe, "-y", "-i", input_path,
        "-af", audio_filter,
        "-c:a", "pcm_s16le", "-ar", "24000", "-ac", "1",
        output_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) < 100:
        # Safe fallback: keep the speech even if this FFmpeg build lacks a filter.
        subprocess.run([
            ffmpeg_exe, "-y", "-i", input_path,
            "-c:a", "pcm_s16le", "-ar", "24000", "-ac", "1",
            output_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    return get_audio_duration(output_path)

def create_master_audio(audio_paths, bgm_path, bgm_volume, output_path, temp_dir, sentences=None):
    ffmpeg_exe = get_ffmpeg_exe()
    
    # Kiem tra: Neu co marker _USE_MASTER_TTS_, dung truc tiep master file (lien mach)
    marker_path = os.path.join(temp_dir, "_USE_MASTER_TTS_")
    if os.path.exists(marker_path):
        with open(marker_path, 'r') as mf:
            master_tts_path = mf.read().strip()
        
        if os.path.exists(master_tts_path):
            print("[+] Su dung master TTS truc tiep (giong doc lien mach).")
            tts_master_wav = os.path.join(temp_dir, "master_tts.wav")
            # Convert MP3 to WAV cho chat luong tot nhat
            subprocess.run([
                ffmpeg_exe, "-y", "-i", master_tts_path, "-c:a", "pcm_s16le", tts_master_wav
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            
            if bgm_path and os.path.exists(bgm_path):
                ducking_filter = (
                    f"[0:a]asplit=2[voice_out][voice_ctrl];"
                    f"[1:a]volume={bgm_volume}[bgm_vol];"
                    f"[bgm_vol][voice_ctrl]sidechaincompress=threshold=0.08:ratio=5:attack=50:release=1000[bgm_ducked];"
                    f"[voice_out][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=2[aout]"
                )
                subprocess.run([
                    ffmpeg_exe, "-y",
                    "-i", tts_master_wav,
                    "-stream_loop", "-1", "-i", bgm_path,
                    "-filter_complex", ducking_filter,
                    "-map", "[aout]",
                    output_path
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            else:
                shutil.copy2(tts_master_wav, output_path)
            
            return None  # Khong can trimmed_durations vi da dung single-file
    
    # Fallback: Trim + Concat tung file (cach cu)
    trimmed_dir = os.path.join(temp_dir, "trimmed")
    os.makedirs(trimmed_dir, exist_ok=True)
    
    trimmed_paths = []
    trimmed_durations = []
    for i, ap in enumerate(audio_paths):
        trimmed_path = os.path.join(trimmed_dir, f"trimmed_{i:04d}.wav")
        sentence_text = sentences[i] if sentences and i < len(sentences) else ""
        pause_duration = _pause_duration_for_text(sentence_text)
        trimmed_duration = trim_silence_from_audio(ap, trimmed_path, pause_duration)
        trimmed_paths.append(trimmed_path)
        trimmed_durations.append(max(trimmed_duration, 0.3))
    
    concat_list_path = os.path.join(temp_dir, "concat_list.txt")
    with open(concat_list_path, 'w', encoding='utf-8') as f:
        for tp in trimmed_paths:
            safe_path = tp.replace("\\", "/")
            f.write(f"file '{safe_path}'\n")
            
    tts_master_path = os.path.join(temp_dir, "master_tts.wav")
    subprocess.run([
        ffmpeg_exe, "-y", "-f", "concat", "-safe", "0", 
        "-i", concat_list_path, "-c:a", "pcm_s16le", tts_master_path
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    
    if bgm_path and os.path.exists(bgm_path):
        ducking_filter = (
            f"[0:a]asplit=2[voice_out][voice_ctrl];"
            f"[1:a]volume={bgm_volume}[bgm_vol];"
            f"[bgm_vol][voice_ctrl]sidechaincompress=threshold=0.08:ratio=5:attack=50:release=1000[bgm_ducked];"
            f"[voice_out][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=2[aout]"
        )
        subprocess.run([
            ffmpeg_exe, "-y",
            "-i", tts_master_path,
            "-stream_loop", "-1", "-i", bgm_path,
            "-filter_complex", ducking_filter,
            "-map", "[aout]",
            output_path
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    else:
        shutil.copy2(tts_master_path, output_path)
    
    return trimmed_durations
def _step1_prefilter_video(input_video_path, filtered_path, options, w_vid, h_vid):
    """Buoc 1: Ap dung bo loc hinh anh (hflip, crop, zoom, contrast) cho TOAN BO video chi 1 lan."""
    ffmpeg_exe = get_ffmpeg_exe()
    
    mirror = options.get("mirror", True)
    zoom_factor = float(options.get("zoom_factor", 1.08))
    contrast = float(options.get("contrast", 1.05))
    
    enable_zoom_crop = options.get("enable_zoom_crop", True)
    enable_ken_burns = options.get("enable_ken_burns", False)
    
    vf_parts = []
    
    # Neu enable_zoom_crop = True -> thuc hien cat bo watermark 10% (5% moi canh)
    if enable_zoom_crop:
        vf_parts.append("crop=in_w*0.9:in_h*0.9:in_w*0.05:in_h*0.05")
        
    if mirror:
        vf_parts.append("hflip")
        
    if enable_ken_burns:
        print("[INFO] ÄÃ£ báº­t cháº¿ Ä‘á»™ Khung hÃ¬nh Biáº¿t thá»Ÿ (Dynamic Ken Burns).")
        # Sá»­ dá»¥ng biá»ƒu thá»©c toÃ¡n há»c thá»i gian t trong lá»‡nh crop Ä‘á»ƒ tá»‘i Æ°u hÃ³a CPU/GPU thay vÃ¬ zoompan
        # Zoom mÆ°á»£t mÃ  tá»« 1.0 lÃªn 1.15 dá»±a trÃªn thá»i gian (t)
        vf_parts.append("crop='iw/min(1.0+0.015*t,1.15)':'ih/min(1.0+0.015*t,1.15)':'(iw-iw/min(1.0+0.015*t,1.15))/2':'(ih-ih/min(1.0+0.015*t,1.15))/2'")
    elif enable_zoom_crop and zoom_factor != 1.0:
        print("[INFO] Cháº¿ Ä‘á»™ Khung hÃ¬nh Biáº¿t thá»Ÿ Ä‘ang Táº®T (Render tá»‘c Ä‘á»™ cao).")
        # Zoom-in chi duoc thuc hien neu co zoom_factor va dc phep crop
        vf_parts.append(f"crop=in_w/{zoom_factor}:in_h/{zoom_factor}:(in_w-in_w/{zoom_factor})/2:(in_h-in_h/{zoom_factor})/2")
    
    # Khuon hinh tro ve do phan giai cua video goc, truoc khi vao buoc 2 cat ty le chuan (chá»‰ scale náº¿u Ä‘Ã£ crop/zoom)
    if enable_zoom_crop:
        vf_parts.append(f"scale={w_vid}:{h_vid}")
    
    if contrast != 1.0:
        vf_parts.append(f"eq=contrast={contrast}")
    
    vf_str = ",".join(vf_parts) if vf_parts else "copy"
    
    # Force keyframes every 1s for frame-accurate cuts later
    # NVENC (GPU - nhanh nhat) + Chat luong cao 12M va lanczos scaler
    cmd = [
        ffmpeg_exe, "-y", "-hwaccel", "auto",
        "-i", input_video_path,
        "-vf", vf_str,
        "-sws_flags", "lanczos",
        "-an",
        "-c:v", "h264_nvenc", "-preset", "p1", "-b:v", "12M",
        "-force_key_frames", "expr:gte(t,n_forced*1)",
        filtered_path
    ]
    res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if res.returncode != 0:
        # Fallback CPU
        cmd = [
            ffmpeg_exe, "-y",
            "-i", input_video_path,
            "-vf", vf_str,
            "-sws_flags", "lanczos",
            "-an",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-force_key_frames", "expr:gte(t,n_forced*1)",
            filtered_path
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

def _step2_cut_segments(input_video_path, temp_dir, sentences, durations, D_video, speed_factor, timestamps=None, end_timestamps=None, scenes=None, assigned_scenes=None, options=None, audio_paths=None):
    """
    Buoc 2: Cat tung phan canh.
    
    PHASE 1.5: Neu co assigned_scenes (CLIP matching), cat tu scene duoc match.
    FALLBACK: Su dung piecewise linear interpolation giua cac timestamp.
    
    UPGRADE: Per-Segment Audio Muxing - nhung audio vao tung segment
    truoc concat de tranh drift tich luy.
    """
    ffmpeg_exe = get_ffmpeg_exe()
    N = len(sentences)
    total_audio_duration = sum(durations)
    
    options = options or {}
    aspect_ratio = options.get("aspect_ratio", "16:9")
    enable_ken_burns = options.get("enable_ken_burns", False)
    
    use_smart = (
        scenes is not None and assigned_scenes is not None and
        len(assigned_scenes) == N and len(scenes) > 0
    )
    use_ranges = bool(
        timestamps and end_timestamps
        and len(timestamps) == N and len(end_timestamps) == N
        and any(start is not None and end is not None for start, end in zip(timestamps, end_timestamps))
    )
    
    if use_ranges:
        print(f"[+] Step2: Su dung truc tiep timeline START-END cua Gemini")
    elif use_smart:
        print(f"[+] Step2: Su dung Adaptive Scene Matching 15-30-60s")
    else:
        print(f"[+] Step2: Su dung Linear Interpolation (fallback)")
    
    # === Xay dung MAP: sentence_index -> video_position (giay) ===
    # HYBRID: AI timestamps lam anchor chinh, audio duration lam tham chieu phu
    known_points = []  # list of (sentence_index, video_time_seconds)
    
    # Thu thap tat ca cac diem da biet (co timestamp AI)
    ai_anchors = {}
    last_valid_ts = -1.0
    if timestamps:
        for i in range(N):
            if i < len(timestamps) and timestamps[i] is not None:
                ts_val = float(timestamps[i])
                # Chi chap nhan timestamp tang dan de tranh AI hallucinate (di lui thoi gian)
                if last_valid_ts <= ts_val < D_video:
                    ai_anchors[i] = ts_val
                    known_points.append((i, ts_val))
                    last_valid_ts = ts_val
                else:
                    print(f"[-] Bo qua timestamp loi/di lui: {ts_val} tai cau {i}")
    
    # Tinh tong audio duration tich luy (de dung khi khong co AI timestamp)
    cumulative_audio = [0.0]
    for d in durations:
      ëžx¶‰žËkºwµçl»†êýÔ­£Ñ¹œÌÍÕ‰µ…­•È(€€€€€€€€€€€É•…Ñ•}ÍÉÐ¡±•…¹}Í•¹Ñ•¹•Ì°‘ÕÉ…Ñ¥½¹Ì°…ÍÍ}Á…Ñ ¤(€€€€€€€€(€€€€€€€€Œ€Ð¸5…ÍÑ•ÈY¥‘•¼I…Ü€¡A¥Á•±¥¹”€Ì‰Õ½ŒÍ¥•ÔÑ½Œ¤(€€€€€€€ÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸ ÔÀ°€‰…¹œáÔ±ä¡¥¹ …¹ ±… ‰…¸ÅÕå•¸€¡A¥Á•±¥¹”€Ì‰Õ½Œ¤¸¸¸ˆ¤(€€€€€€€µ…ÍÑ•É}Ù¥‘•½}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡Ñ•µÁ}‘¥È°€‰µ…ÍÑ•É}Ù¥‘•½}É…Ü¹µÀÐˆ¤(€€€€€€€É•…Ñ•}µ…ÍÑ•É}Ù¥‘•½}É…Ü¡¥¹ÁÕÑ}Ù¥‘•½}Á…Ñ °µ…ÍÑ•É}Ù¥‘•½}Á…Ñ °±•…¹}Í•¹Ñ•¹•Ì°‘ÕÉ…Ñ¥½¹Ì°½ÁÑ¥½¹Ì°ÕÁ‘…Ñ•}™¸õÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸°Ñ¥µ•ÍÑ…µÁÌõÑ¥µ•ÍÑ…µÁÌ°•¹‘}Ñ¥µ•ÍÑ…µÁÌõ•¹‘}Ñ¥µ•ÍÑ…µÁÌ°…Õ‘¥½}Á…Ñ¡Ìõ…Õ‘¥½}Á…Ñ¡Ì¤(€€€€€€€€(€€€€€€€€Œ€Ô¸i¥À¥ÐÕÀ(€€€€€€€ÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸ äÀ°€‹A…¹œƒGÍ¹œŸÍ¤i%@¡¼…ÁÕÐ¸¸¸ˆ¤(€€€€€€€½ÕÑÁÕÑ}™¥±•¹…µ”€ô˜‰…ÁÕÑ}Á…­…•}íÑ…Í­}¥‘ô¹é¥Àˆ(€€€€€€€½ÕÑÁÕÑ}‘¥È€ô½Ì¹Á…Ñ ¹©½¥¸¡½Ì¹Á…Ñ ¹‘¥É¹…µ”¡¥¹ÁÕÑ}Ù¥‘•½}Á…Ñ ¤°€‰ÍÑ…Ñ¥Œˆ°€‰½ÕÑÁÕÐˆ¤(€€€€€€€½Ì¹µ…­•‘¥ÉÌ¡½ÕÑÁÕÑ}‘¥È°•á¥ÍÑ}½¬õQÉÕ”¤(€€€€€€€é¥Á}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡½ÕÑÁÕÑ}‘¥È°½ÕÑÁÕÑ}™¥±•¹…µ”¤(€€€€€€€€(€€€€€€€Ý¥Ñ é¥Á™¥±”¹i¥Á¥±”¡é¥Á}Á…Ñ °€Üœ°é¥Á™¥±”¹i%A}1Q¤…Ìé¥Á˜è(€€€€€€€€€€€é¥Á˜¹ÝÉ¥Ñ”¡µ…ÍÑ•É}Ù¥‘•½}Á…Ñ °€ˆÅ}Y¥‘•½}½Œ¹µÀÐˆ¤(€€€€€€€€€€€é¥Á˜¹ÝÉ¥Ñ”¡µ…ÍÑ•É}…Õ‘¥½}Á…Ñ °€ˆÉ}µ}Q¡…¹ ¹µÀÌˆ¤(€€€€€€€€€€€é¥Á˜¹ÝÉ¥Ñ”¡…ÍÍ}Á…Ñ °€ˆÍ}A¡Õ}”¹…ÍÌˆ¤(€€€€€€€€€€€€(€€€€€€€ÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸ ÄÀÀ°˜‰½¹œ½¤¡½…¸Ñ…Ð„M…¸Í…¹œÑ…¤Ù”¸ˆ¤(€€€€€€€}±•…¹ÕÁ}½±‘}½ÕÑÁÕÑÌ¡½ÕÑÁÕÑ}‘¥È¤(€€€€€€€É•ÑÕÉ¸˜ˆ½ÍÑ…Ñ¥Œ½½ÕÑÁÕÐ½í½ÕÑÁÕÑ}™¥±•¹…µ•ôˆ(€€€€€€€€(€€€™¥¹…±±äè(€€€€€€€±•…¹ÕÁ}Ñ•µÁ}™¥±•Ì¡Ñ•µÁ}‘¥È¤()‘•˜É•¹‘•É}É•Ù¥•Ý}Ù¥‘•½}™™µÁ•œ¡Ñ…Í­}¥°¥¹ÁÕÑ}Ù¥‘•½}Á…Ñ °‰µ}Á…Ñ °‰µ}Ù½±Õµ”°Ù½¥•}¹…µ”°Ù½¥•}É…Ñ”°Í•¹Ñ•¹•Ì°½ÁÑ¥½¹Ì°ÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸°‘¥…¹½ÍÑ¥}±½œõ9½¹”¤è(€€€Ñ•µÁ}‘¥È€ô½Ì¹Á…Ñ ¹©½¥¸¡½Ì¹Á…Ñ ¹‘¥É¹…µ”¡¥¹ÁÕÑ}Ù¥‘•½}Á…Ñ ¤°˜‰Ñ•µÁ}íÑ…Í­}¥‘ôˆ¤(€€€½Ì¹µ…­•‘¥ÉÌ¡Ñ•µÁ}‘¥È°•á¥ÍÑ}½¬õQÉÕ”¤(€€€€(€€€±•…¹}Í•¹Ñ•¹•Ì°Ñ¥µ•ÍÑ…µÁÌ°•¹‘}Ñ¥µ•ÍÑ…µÁÌ€ôÁ…ÉÍ•}Í•¹Ñ•¹•Í}…¹‘}Ñ¥µ•}É…¹•Ì¡Í•¹Ñ•¹•Ì¤(€€€€(€€€ÑÉäè(€€€€€€€ÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸ ÄÀ°€‹A…¹œÓ†îU¹œ£†îÀ§†î5¹œ»Í¤$€¡QQLMÕ‰5…­•È¤¸¸¸ˆ¤(€€€€€€€…Õ‘¥½}Á…Ñ¡Ì°‘ÕÉ…Ñ¥½¹Ì€ô•¹•É…Ñ•}…±±}ÑÑÌ¡±•…¹}Í•¹Ñ•¹•Ì°Ù½¥•}¹…µ”°Ù½¥•}É…Ñ”°Ñ•µÁ}‘¥È°½ÁÑ¥½¹Ì°‘¥…¹½ÍÑ¥}±½œ¤(€€€€€€€…Õ‘¥½}Á…Ñ¡Ì°‘ÕÉ…Ñ¥½¹Ì°ÑÑÍ}…‘©ÕÍÑ•€ô™¥Ñ}ÑÑÍ}Ñ½}Ñ¥µ•}É…¹•Ì (€€€€€€€€€€€…Õ‘¥½}Á…Ñ¡Ì°‘ÕÉ…Ñ¥½¹Ì°Ñ¥µ•ÍÑ…µÁÌ°•¹‘}Ñ¥µ•ÍÑ…µÁÌ°Ñ•µÁ}‘¥È(€€€€€€€€¤(€€€€€€€€(€€€€€€€€Œ5…ÍÑ•ÈÕ‘¥¼(€€€€€€€ÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸ ÌÀ°€‹A…¹œÁ£†îE¤£†îÀƒ‰´Ñ¡…¹ Û€	4¸¸¸ˆ¤(€€€€€€€µ…ÍÑ•É}…Õ‘¥½}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡Ñ•µÁ}‘¥È°€‰µ…ÍÑ•É}…Õ‘¥¼¹µÀÌˆ¤(€€€€€€€¹…ÑÕÉ…±}‘ÕÉ…Ñ¥½¹Ì€ôÉ•…Ñ•}µ…ÍÑ•É}…Õ‘¥¼ (€€€€€€€€€€€…Õ‘¥½}Á…Ñ¡Ì°‰µ}Á…Ñ °‰µ}Ù½±Õµ”°µ…ÍÑ•É}…Õ‘¥½}Á…Ñ °Ñ•µÁ}‘¥È°±•…¹}Í•¹Ñ•¹•Ì(€€€€€€€€¤(€€€€€€€¥˜¹…ÑÕÉ…±}‘ÕÉ…Ñ¥½¹Ì…¹±•¸¡¹…ÑÕÉ…±}‘ÕÉ…Ñ¥½¹Ì¤€ôô±•¸¡‘ÕÉ…Ñ¥½¹Ì¤è(€€€€€€€€€€€ÑÑÍ}…‘©ÕÍÑ•€ôÑÑÍ}…‘©ÕÍÑ•½È…¹ä (€€€€€€€€€€€€€€€…‰Ì¡„€´ˆ¤€ø€À¸ÀÐ™½È„°ˆ¥¸é¥À¡¹…ÑÕÉ…±}‘ÕÉ…Ñ¥½¹Ì°‘ÕÉ…Ñ¥½¹Ì¤(€€€€€€€€€€€€¤(€€€€€€€€€€€‘ÕÉ…Ñ¥½¹Ì€ô¹…ÑÕÉ…±}‘ÕÉ…Ñ¥½¹Ì(€€€€€€€€(€€€€€€€€ŒML€´‘Õ¹œMÕ‰5…­•ÈML€¡¡¥¹ á…ŒÑÕ¹œÑÔ°‰…¼½´ÍÑå±”¤(€€€€€€€ÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸ ÐÀ°€‹A…¹œÓ†ê…¼Á£†î”ƒG†îƒG†îe¹œ¡×†ê¥¸ML€¡-…É…½­”½!½Éµ½é¤¤¸¸¸ˆ¤(€€€€€€€ÍÕ‰µ…­•É}…ÍÌ€ô½Ì¹Á…Ñ ¹©½¥¸¡Ñ•µÁ}‘¥È°€‰ÍÕ‰µ…­•É}ÍÕ‰Ñ¥Ñ±•Ì¹…ÍÌˆ¤(€€€€€€€…ÍÍ}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡Ñ•µÁ}‘¥È°€‰ÍÕ‰Ñ¥Ñ±•Ì¹…ÍÌˆ¤(€€€€€€€¥˜½Ì¹Á…Ñ ¹•á¥ÍÑÌ¡ÍÕ‰µ…­•É}…ÍÌ¤…¹¹½ÐÑÑÍ}…‘©ÕÍÑ•è(€€€€€€€€€€€Í¡ÕÑ¥°¹½ÁäÈ¡ÍÕ‰µ…­•É}…ÍÌ°…ÍÍ}Á…Ñ ¤(€€€€€€€•±Í”è(€€€€€€€€€€€É•…Ñ•}ÍÉÐ¡±•…¹}Í•¹Ñ•¹•Ì°‘ÕÉ…Ñ¥½¹Ì°…ÍÍ}Á…Ñ ¤(€€€€€€€€(€€€€€€€ÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸ ÔÀ°€‰…¹œáÔ±ä¡¥¹ …¹ ±… ‰…¸ÅÕå•¸€¡A¥Á•±¥¹”€Ì‰Õ½Œ¤¸¸¸ˆ¤(€€€€€€€µ…ÍÑ•É}Ù¥‘•½}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡Ñ•µÁ}‘¥È°€‰µ…ÍÑ•É}Ù¥‘•½}É…Ü¹µÀÐˆ¤(€€€€€€€É•…Ñ•}µ…ÍÑ•É}Ù¥‘•½}É…Ü¡¥¹ÁÕÑ}Ù¥‘•½}Á…Ñ °µ…ÍÑ•É}Ù¥‘•½}Á…Ñ °±•…¹}Í•¹Ñ•¹•Ì°‘ÕÉ…Ñ¥½¹Ì°½ÁÑ¥½¹Ì°ÕÁ‘…Ñ•}™¸õÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸°Ñ¥µ•ÍÑ…µÁÌõÑ¥µ•ÍÑ…µÁÌ°•¹‘}Ñ¥µ•ÍÑ…µÁÌõ•¹‘}Ñ¥µ•ÍÑ…µÁÌ°…Õ‘¥½}Á…Ñ¡Ìõ…Õ‘¥½}Á…Ñ¡Ì¤(€€€€€€€€(€€€€€€€ÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸ àÀ°€‰…¹œ¹Õ¹œÁ¡Ô‘”Ù…¼Ù¥‘•¼€¡!…É‘ÍÕˆ¤¸¸¸ˆ¤(€€€€€€€½ÕÑÁÕÑ}™¥±•¹…µ”€ô˜‰Ù¥‘•½}É•Ù¥•Ý}íÑ…Í­}¥‘ô¹µÀÐˆ(€€€€€€€½ÕÑÁÕÑ}‘¥È€ô½Ì¹Á…Ñ ¹©½¥¸¡½Ì¹Á…Ñ ¹‘¥É¹…µ”¡¥¹ÁÕÑ}Ù¥‘•½}Á…Ñ ¤°€‰ÍÑ…Ñ¥Œˆ°€‰½ÕÑÁÕÐˆ¤(€€€€€€€½Ì¹µ…­•‘¥ÉÌ¡½ÕÑÁÕÑ}‘¥È°•á¥ÍÑ}½¬õQÉÕ”¤(€€€€€€€½ÕÑÁÕÑ}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡½ÕÑÁÕÑ}‘¥È°½ÕÑÁÕÑ}™¥±•¹…µ”¤(€€€€€€€€(€€€€€€€™½¹Ñ}Í¥é•}ÕÍ•È€ô¥¹Ð¡½ÁÑ¥½¹Ì¹•Ð ‰™½¹Ñ}Í¥é”ˆ°€ÐÈ¤¤(€€€€€€€™½¹Ñ}½±½É}¡•à€ô½ÁÑ¥½¹Ì¹•Ð ‰™½¹Ñ}½±½Èˆ°€ˆˆ¤¹±ÍÑÉ¥À œŒœ¤(€€€€€€€€Œ…´‰…¼¡•à±Õ½¸±„€Ø­äÑÔ(€€€€€€€¥˜±•¸¡™½¹Ñ}½±½É}¡•à¤€ôô€Ìè(€€€€€€€€€€€™½¹Ñ}½±½É}¡•à€ô€œœ¹©½¥¸¡mŒ¨È™½ÈŒ¥¸™½¹Ñ}½±½É}¡•át¤(€€€€€€€•±¥˜±•¸¡™½¹Ñ}½±½É}¡•à¤€„ô€Øè(€€€€€€€€€€€™½¹Ñ}½±½É}¡•à€ô€‰ˆ(€€€€€€€‰}½Á…¥Ñä€ô¥¹Ð¡½ÁÑ¥½¹Ì¹•Ð ‰‰}½Á…¥Ñäˆ°€ÄÔÀ¤¤(€€€€€€€‰}…±Á¡…}¡•à€ô˜‰í‰}½Á…¥ÑäèÀÉaôˆ(€€€€€€€€(€€€€€€€€ŒML½¹ÑÍ¥é”‘Õ¹œA±…åI•Ídµ…Œ‘¥¹ €ô€ÌàÐ(€€€€€€€€Œ”¡Õå•¸ÑÔÁ¥á•°Ñ¡…ÐÑÉ•¸Ù¥‘•¼Í…¹œML½¹ÑÍ¥é”è(€€€€€€€€Œ…ÍÍ}Í¥é”€ôÕÍ•É}Áà€¨€ÌàÐ€¼Ù¥‘•½}¡•¥¡Ð(€€€€€€€|°|°¡}Ù¥€ô•Ñ}Ù¥‘•½}¥¹™¼¡µ…ÍÑ•É}Ù¥‘•½}Á…Ñ ¤(€€€€€€€…ÍÍ}™½¹Ñ}Í¥é”€ôµ…à ÄÈ°¥¹Ð¡™½¹Ñ}Í¥é•}ÕÍ•È€¨€ÌàÐ€¼¡}Ù¥¤¤(€€€€€€€€(€€€€€€€€ŒMLAÉ¥µ…Éå½±½ÕÈ™½Éµ…Ðè€™ ÀÁ		IH€¡	H½É‘•È¤(€€€€€€€È€ô™½¹Ñ}½±½É}¡•álÀèÉt(€€€€€€€œ€ô™½¹Ñ}½±½É}¡•álÈèÑt(€€€€€€€ˆ€ô™½¹Ñ}½±½É}¡•álÐèÙt(€€€€€€€ÁÉ¥µ…Éå}½±½È€ô˜ˆ™ ÀÁí‰õíõíÉôˆ(€€€€€€€€(€€€€€€€ÍÑå±”€ô€ (€€€€€€€€€€€˜‰½¹Ñ¹…µ”õÉ¥…°±½¹ÑÍ¥é”õí…ÍÍ}™½¹Ñ}Í¥é•ô±	½±ôÄ°ˆ(€€€€€€€€€€€˜‰AÉ¥µ…Éå½±½ÕÈõíÁÉ¥µ…Éå}½±½Éô°ˆ(€€€€€€€€€€€˜‰=ÕÑ±¥¹•½±½ÕÈô™ ÀÀÀÀÀÀÀÀ°ˆ(€€€€€€€€€€€˜‰	…­½±½ÕÈô™ àÀÀÀÀÀÀÀ°ˆ(€€€€€€€€€€€˜‰	½É‘•ÉMÑå±”ôÄ°ˆ(€€€€€€€€€€€˜‰=ÕÑ±¥¹”ôÈ¸Ô±M¡…‘½ÜôÄ¸Ô°ˆ(€€€€€€€€€€€˜‰5…É¥¹0ôØÀ±5…É¥¹HôØÀ±5…É¥¹XôÈÔ°ˆ(€€€€€€€€€€€˜‰]É…ÁMÑå±”ôÀ±MÁ…¥¹œôÀ¸Ôˆ(€€€€€€€€¤(€€€€€€€€(€€€€€€€™™µÁ•}•á”€ô•Ñ}™™µÁ•}•á” ¤(€€€€€€€€(€€€€€€€€Œ½ÁäMIPÙ…¼Õ¹œÑ¡ÔµÕŒ½ÕÑÁÕÐ‘”ÑÉ…¹ ±½¤•Í…Á”‘Õ½¹œ‘…¸]¥¹‘½ÝÌ(€€€€€€€…ÍÍ}½Áå}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡½ÕÑÁÕÑ}‘¥È°˜‰Ñ•µÁ}ÍÕ‰}íÑ…Í­}¥‘ô¹…ÍÌˆ¤(€€€€€€€Í¡ÕÑ¥°¹½ÁäÈ¡…ÍÍ}Á…Ñ °…ÍÍ}½Áå}Á…Ñ ¤(€€€€€€€€(€€€€€€€€ŒMÔ‘Õ¹œ‘Õ½¹œÁ…Ñ ÑÕ½¹œ‘½¤(€€€€€€€…ÍÍ}É•°€ô½Ì¹Á…Ñ ¹É•±Á…Ñ ¡…ÍÍ}½Áå}Á…Ñ °½Ì¹Á…Ñ ¹‘¥É¹…µ”¡¥¹ÁÕÑ}Ù¥‘•½}Á…Ñ ¤¤(€€€€€€€…ÍÍ}™™µÁ•œ€ô…ÍÍ}É•°¹É•Á±…” ‰qpˆ°€ˆ¼ˆ¤¹É•Á±…” ˆèˆ°€‰qpèˆ¤(€€€€€€€€(€€€€€€€•¹…‰±•}ÍÕ‰Ñ¥Ñ±•Ì€ô½ÁÑ¥½¹Ì¹•Ð ‰•¹…‰±•}ÍÕ‰Ñ¥Ñ±•Ìˆ°QÉÕ”¤(€€€€€€€€(€€€€€€€¥˜•¹…‰±•}ÍÕ‰Ñ¥Ñ±•Ìè(€€€€€€€€€€€€Œå¹œÑ£†êÍ¹œ‹†îd³†î5Œ…ÍÌ¡¿†êÝŒÍÕ‰Ñ¥Ñ±•Ì·€-#Q9£¡¸Ñ£©´™½É•}ÍÑå±”(€€€€€€€€€€€€ŒÛ°™¥±”€¹…ÍÌƒGŒ‰…¼Ÿ†îM´Ñ¿¸‹†îdÍÑå±”€£G†êµ´°Ù§†î¸°™½¹Ð°¸§†î½„¤(€€€€€€€€€€€Ù™}™¥±Ñ•È€ô˜‰…ÍÌôí…ÍÍ}™™µÁ•ôœˆ(€€€€€€€€€€€µ‘}ÍÕˆ€ôl(€€€€€€€€€€€€€€€™™µÁ•}•á”°€ˆµäˆ°(€€€€€€€€€€€€€€€€ˆµ¤ˆ°µ…ÍÑ•É}Ù¥‘•½}Á…Ñ °(€€€€€€€€€€€€€€€€ˆµ¤ˆ°µ…ÍÑ•É}…Õ‘¥½}Á…Ñ °(€€€€€€€€€€€€€€€€ˆµÙ˜ˆ°Ù™}™¥±Ñ•È°(€€€€€€€€€€€€€€€€ˆµÍÝÍ}™±…Ìˆ°€‰±…¹é½Ìˆ°(€€€€€€€€€€€€€€€€ˆµµ…Àˆ°€ˆÀéØˆ°€ˆµµ…Àˆ°€ˆÄé„ˆ°(€€€€€€€€€€€€€€€€ˆµŒéØˆ°€‰ ÈØÑ}¹Ù•¹Œˆ°€ˆµÁÉ•Í•Ðˆ°€‰ÀÄˆ°€ˆµˆéØˆ°€ˆÄÉ4ˆ°(€€€€€€€€€€€€€€€€ˆµŒé„ˆ°€‰……Œˆ°€ˆµˆé„ˆ°€ˆÄäÉ¬ˆ°(€€€€€€€€€€€€€€€€ˆµÍ¡½ÉÑ•ÍÐˆ°(€€€€€€€€€€€€€€€½ÕÑÁÕÑ}Á…Ñ (€€€€€€€€€€€t(€€€€€€€€€€€µ‘}ÍÕ‰}ÁÔ€ôl(€€€€€€€€€€€€€€€™™µÁ•}•á”°€ˆµäˆ°(€€€€€€€€€€€€€€€€ˆµ¤ˆ°µ…ÍÑ•É}Ù¥‘•½}Á…Ñ °(€€€€€€€€€€€€€€€€ˆµ¤ˆ°µ…ÍÑ•É}…Õ‘¥½}Á…Ñ °(€€€€€€€€€€€€€€€€ˆµÙ˜ˆ°Ù™}™¥±Ñ•È°(€€€€€€€€€€€€€€€€ˆµÍÝÍ}™±…Ìˆ°€‰±…¹é½Ìˆ°(€€€€€€€€€€€€€€€€ˆµµ…Àˆ°€ˆÀéØˆ°€ˆµµ…Àˆ°€ˆÄé„ˆ°(€€€€€€€€€€€€€€€€ˆµŒéØˆ°€‰±¥‰àÈØÐˆ°€ˆµÁÉ•Í•Ðˆ°€‰™…ÍÐˆ°€ˆµÉ˜ˆ°€ˆÈÈˆ°(€€€€€€€€€€€€€€€€ˆµŒé„ˆ°€‰……Œˆ°€ˆµˆé„ˆ°€ˆÄäÉ¬ˆ°(€€€€€€€€€€€€€€€€ˆµÍ¡½ÉÑ•ÍÐˆ°(€€€€€€€€€€€€€€€½ÕÑÁÕÑ}Á…Ñ (€€€€€€€€€€€t(€€€€€€€•±Í”è(€€€€€€€€€€€µ‘}ÍÕˆ€ôl(€€€€€€€€€€€€€€€™™µÁ•}•á”°€ˆµäˆ°(€€€€€€€€€€€€€€€€ˆµ¤ˆ°µ…ÍÑ•É}Ù¥‘•½}Á…Ñ °(€€€€€€€€€€€€€€€€ˆµ¤ˆ°µ…ÍÑ•É}…Õ‘¥½}Á…Ñ °(€€€€€€€€€€€€€€€€ˆµµ…Àˆ°€ˆÀéØˆ°€ˆµµ…Àˆ°€ˆÄé„ˆ°(€€€€€€€€€€€€€€€€ˆµŒéØˆ°€‰½Áäˆ°(€€€€€€€€€€€€€€€€ˆµŒé„ˆ°€‰……Œˆ°€ˆµˆé„ˆ°€ˆÄäÉ¬ˆ°(€€€€€€€€€€€€€€€€ˆµÍ¡½ÉÑ•ÍÐˆ°(€€€€€€€€€€€€€€€½ÕÑÁÕÑ}Á…Ñ (€€€€€€€€€€€t(€€€€€€€€€€€µ‘}ÍÕ‰}ÁÔ€ôµ‘}ÍÕˆ(€€€€€€€€(€€€€€€€É•Ì€ôÍÕ‰ÁÉ½•ÍÌ¹ÉÕ¸¡µ‘}ÍÕˆ°ÍÑ‘½ÕÐõÍÕ‰ÁÉ½•ÍÌ¹Y9U10°ÍÑ‘•ÉÈõÍÕ‰ÁÉ½•ÍÌ¹Y9U10°Ýõ½Ì¹Á…Ñ ¹‘¥É¹…µ”¡¥¹ÁÕÑ}Ù¥‘•½}Á…Ñ ¤¤(€€€€€€€¥˜É•Ì¹É•ÑÕÉ¹½‘”€„ô€Àè(€€€€€€€€€€€€ŒQ¡¥Ì½µµ…¹É•…±±ä¥ÌAT™…±±‰…¬€¡Ñ¡”ÁÉ•Ù¥½ÕÌ¥µÁ±•µ•¹Ñ…Ñ¥½¸(€€€€€€€€€€€€Œ…¥‘•¹Ñ…±±äÉ•ÑÉ¥•9Y9…¹™…¥±•……¥¸½¸µ…¡¥¹•ÌÝ¥Ñ¡½ÕÐ¥Ð¤¸(€€€€€€€€€€€ÍÕ‰ÁÉ½•ÍÌ¹ÉÕ¸¡µ‘}ÍÕ‰}ÁÔ°ÍÑ‘½ÕÐõÍÕ‰ÁÉ½•ÍÌ¹Y9U10°ÍÑ‘•ÉÈõÍÕ‰ÁÉ½•ÍÌ¹Y9U10°¡•¬õQÉÕ”°Ýõ½Ì¹Á…Ñ ¹‘¥É¹…µ”¡¥¹ÁÕÑ}Ù¥‘•½}Á…Ñ ¤¤(€€€€€€€€(€€€€€€€€Œ½¸‘•À™¥±”MIPÑ…´(€€€€€€€ÑÉäè(€€€€€€€€€€€½Ì¹É•µ½Ù”¡…ÍÍ}½Áå}Á…Ñ ¤(€€€€€€€•á•ÁÐá•ÁÑ¥½¸è(€€€€€€€€€€€Á…ÍÌ(€€€€€€€€(€€€€€€€ÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸ ÄÀÀ°˜‰!½…¸Ñ¡…¹ áÕ…ÐÙ¥‘•¼5@Ð„ˆ¤(€€€€€€€}±•…¹ÕÁ}½±‘}½ÕÑÁÕÑÌ¡½ÕÑÁÕÑ}‘¥È¤(€€€€€€€É•ÑÕÉ¸˜ˆ½ÍÑ…Ñ¥Œ½½ÕÑÁÕÐ½í½ÕÑÁÕÑ}™¥±•¹…µ•ôˆ(€€€€€€€€(€€€™¥¹…±±äè(€€€€€€€±•…¹ÕÁ}Ñ•µÁ}™¥±•Ì¡Ñ•µÁ}‘¥È¤()‘•˜}•¹•É…Ñ•}Í¥¹±•}¥µ…”¡±¥•¹Ð°ÁÉ½µÁÐ°½ÕÑÁÕÑ}Á…Ñ °…ÍÁ•Ñ}É…Ñ¥¼¤è(€€€ÑÉäè(€€€€€€€É•ÍÕ±Ð€ô±¥•¹Ð¹µ½‘•±Ì¹•¹•É…Ñ•}¥µ…•Ì (€€€€€€€€€€€µ½‘•°ô¥µ…•¸´Ì¸Àµ•¹•É…Ñ”´ÀÀÄœ°(€€€€€€€€€€€ÁÉ½µÁÐõÁÉ½µÁÐ°(€€€€€€€€€€€½¹™¥œõ‘¥Ð (€€€€€€€€€€€€€€€¹Õµ‰•É}½™}¥µ…•ÌôÄ°(€€€€€€€€€€€€€€€½ÕÑÁÕÑ}µ¥µ•}ÑåÁ”ô‰¥µ…”½©Á•œˆ°(€€€€€€€€€€€€€€€…ÍÁ•Ñ}É…Ñ¥¼õ…ÍÁ•Ñ}É…Ñ¥¼°(€€€€€€€€€€€€¤(€€€€€€€€¤(€€€€€€€¥˜É•ÍÕ±Ð¹•¹•É…Ñ•‘}¥µ…•Ìè(€€€€€€€€€€€¥µ…•}‰åÑ•Ì€ôÉ•ÍÕ±Ð¹•¹•É…Ñ•‘}¥µ…•ÍlÁt¹¥µ…”¹¥µ…•}‰åÑ•Ì(€€€€€€€€€€€Ý¥Ñ ½Á•¸¡½ÕÑÁÕÑ}Á…Ñ °€Ýˆœ¤…Ì¥µ}˜è(€€€€€€€€€€€€€€€¥µ}˜¹ÝÉ¥Ñ”¡¥µ…•}‰åÑ•Ì¤(€€€€€€€€€€€É•ÑÕÉ¸QÉÕ”(€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€ÁÉ¥¹Ð¡˜‰ÉÉ½È•¹•É…Ñ¥¹œ¥µ…”èí•ôˆ¤(€€€€€€€É•ÑÕÉ¸…±Í”(€€€É•ÑÕÉ¸…±Í”()‘•˜•¹•É…Ñ•}…±±}¥µ…•Ì¡…Á¥}­•ä°Í•¹Ñ•¹•Ì°ÕÍÑ½µ}ÍÑå±•}ÁÉ½µÁÐ°Ñ•µÁ}‘¥È°…ÍÁ•Ñ}É…Ñ¥¼ôˆÄØèäˆ°ÕÁ‘…Ñ•}ÁÉ½œõ9½¹”¤è(€€€™É½´½½±”¥µÁ½ÉÐ•¹…¤(€€€±¥•¹Ð€ô•¹…¤¹±¥•¹Ð¡…Á¥}­•äõ…Á¥}­•ä¤(€€€¥µ…•}Á…Ñ¡Ì€ômt(€€€€(€€€¥˜ÕÁ‘…Ñ•}ÁÉ½œèÕÁ‘…Ñ•}ÁÉ½œ ÄÔ°€‹A…¹œÛ†êôƒ†ê¹ ¡¼Ó†î­¹œ‰Ô€¡%µ…•¸€Ì¤¸¸¸ˆ¤(€€€€(€€€Ñ½Ñ…°€ô±•¸¡Í•¹Ñ•¹•Ì¤(€€€™½È¤°Ñ•áÐ¥¸•¹Õµ•É…Ñ”¡Í•¹Ñ•¹•Ì¤è(€€€€€€€¥µ}™¥±•¹…µ”€ô˜‰¥µ}í¤èÀÍ‘ô¹©Áœˆ(€€€€€€€¥µ}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡Ñ•µÁ}‘¥È°¥µ}™¥±•¹…µ”¤(€€€€€€€€(€€€€€€€ÁÉ½µÁÐ€ô˜‰íÑ•áÑô¸íÕÍÑ½µ}ÍÑå±•}ÁÉ½µÁÑôˆ(€€€€€€€€(€€€€€€€¥˜ÕÁ‘…Ñ•}ÁÉ½œèÕÁ‘…Ñ•}ÁÉ½œ ÄÔ€¬¥¹Ð ¡¤½Ñ½Ñ…°¤¨ÈÀ¤°˜‹A…¹œÛ†êôƒ†ê¹ í¤¬Åô½íÑ½Ñ…±ô€¡%µ…•¸€Ì¤¸¸¸ˆ¤(€€€€€€€€(€€€€€€€ÍÕ•ÍÌ€ô}•¹•É…Ñ•}Í¥¹±•}¥µ…”¡±¥•¹Ð°ÁÉ½µÁÐ°¥µ}Á…Ñ °…ÍÁ•Ñ}É…Ñ¥¼¤(€€€€€€€¥˜¹½ÐÍÕ•ÍÌè(€€€€€€€€€€€™™µÁ•}•á”€ô•Ñ}™™µÁ•}•á” ¤(€€€€€€€€€€€Ü° €ô€ ÄäÈÀ°€ÄÀàÀ¤¥˜…ÍÁ•Ñ}É…Ñ¥¼€ôô€ˆÄØèäˆ•±Í”€ ÄÀàÀ°€ÄäÈÀ¤(€€€€€€€€€€€ÍÕ‰ÁÉ½•ÍÌ¹ÉÕ¸¡l(€€€€€€€€€€€€€€€™™µÁ•}•á”°€ˆµäˆ°€ˆµ˜ˆ°€‰±…Ù™¤ˆ°€ˆµ¤ˆ°˜‰½±½ÈõŒõ‰±…¬éÌõíÝõáí¡ôˆ°€(€€€€€€€€€€€€€€€€ˆµÙ™É…µ•Ìˆ°€ˆÄˆ°¥µ}Á…Ñ (€€€€€€€€€€€t°ÍÑ‘½ÕÐõÍÕ‰ÁÉ½•ÍÌ¹Y9U10°ÍÑ‘•ÉÈõÍÕ‰ÁÉ½•ÍÌ¹Y9U10¤(€€€€€€€€€€€€(€€€€€€€¥µ…•}Á…Ñ¡Ì¹…ÁÁ•¹¡¥µ}Á…Ñ ¤(€€€€€€€€(€€€É•ÑÕÉ¸¥µ…•}Á…Ñ¡Ì()‘•˜É•¹‘•É}…¥}¥µ…•}Ù¥‘•½}™™µÁ•œ¡Ñ…Í­}¥°Í•¹Ñ•¹•Ì°‘ÕÉ…Ñ¥½¹Ì°…Õ‘¥½}Á…Ñ¡Ì°¥µ…•}Á…Ñ¡Ì°‰µ}Á…Ñ °‰µ}Ù½±Õµ”°½ÁÑ¥½¹Ì°ÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸¤è(€€€™™µÁ•}•á”€ô•Ñ}™™µÁ•}•á” ¤(€€€Ñ•µÁ}‘¥È€ô½Ì¹Á…Ñ ¹©½¥¸¡½Ì¹Á…Ñ ¹‘¥É¹…µ”¡‰µ}Á…Ñ ¤¥˜‰µ}Á…Ñ •±Í”½Ì¹Á…Ñ ¹‘¥É¹…µ”¡…Õ‘¥½}Á…Ñ¡ÍlÁt¤°˜‰Ñ•µÁ}É•¹‘•É}íÑ…Í­}¥‘ôˆ¤(€€€½Ì¹µ…­•‘¥ÉÌ¡Ñ•µÁ}‘¥È°•á¥ÍÑ}½¬õQÉÕ”¤(€€€€(€€€ÑÉäè(€€€€€€€ÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸ ÔÀ°€‹A…¹œã†î´³ô£±¹ ƒ†ê¹ €¡M±¥‘•Í¡½Ü¤¸¸¸ˆ¤(€€€€€€€½¹…Ñ}¥µ}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡Ñ•µÁ}‘¥È°€‰½¹…Ñ}¥µ…•Ì¹ÑáÐˆ¤(€€€€€€€Ý¥Ñ ½Á•¸¡½¹…Ñ}¥µ}Á…Ñ °€Üœ°•¹½‘¥¹œôÕÑ˜´àœ¤…Ì˜Èè(€€€€€€€€€€€™½È¤°¥µ}Á…Ñ ¥¸•¹Õµ•É…Ñ”¡¥µ…•}Á…Ñ¡Ì¤è(€€€€€€€€€€€€€€€Í…™•}¥µœ€ô¥µ}Á…Ñ ¹É•Á±…” ‰qpˆ°€ˆ¼ˆ¤(€€€€€€€€€€€€€€€˜È¹ÝÉ¥Ñ”¡˜‰™¥±”€íÍ…™•}¥µôq¸ˆ¤(€€€€€€€€€€€€€€€˜È¹ÝÉ¥Ñ”¡˜‰‘ÕÉ…Ñ¥½¸í‘ÕÉ…Ñ¥½¹Ím¥uõq¸ˆ¤(€€€€€€€€€€€€ŒAåÑ¡½¸‘½•Ì¹½Ð…±±½Ü„‰…­Í±…Í ¥¹Í¥‘”…¸˜µÍÑÉ¥¹œ•áÁÉ•ÍÍ¥½¸¸(€€€€€€€€€€€±…ÍÑ}Í…™•}¥µœ€ô¥µ…•}Á…Ñ¡Íl´Åt¹É•Á±…” ‰qpˆ°€ˆ¼ˆ¤(€€€€€€€€€€€˜È¹ÝÉ¥Ñ”¡˜‰™¥±”€í±…ÍÑ}Í…™•}¥µôq¸ˆ¤(€€€€€€€€€€€€(€€€€€€€É…Ý}Ù¥‘•½}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡Ñ•µÁ}‘¥È°€‰É…Ý}Ù¥‘•¼¹µÀÐˆ¤(€€€€€€€Ý}É•Ì°¡}É•Ì€ô€ ÄäÈÀ°€ÄÀàÀ¤¥˜½ÁÑ¥½¹Ì¹•Ð ‰…ÍÁ•Ñ}É…Ñ¥¼ˆ°€ˆÄØèäˆ¤€ôô€ˆÄØèäˆ•±Í”€ ÄÀàÀ°€ÄäÈÀ¤(€€€€€€€€(€€€€€€€µ‘}¥µœ€ôl(€€€€€€€€€€€™™µÁ•}•á”°€ˆµäˆ°(€€€€€€€€€€€€ˆµ˜ˆ°€‰½¹…Ðˆ°€ˆµÍ…™”ˆ°€ˆÀˆ°(€€€€€€€€€€€€ˆµ¤ˆ°½¹…Ñ}¥µ}Á…Ñ °(€€€€€€€€€€€€ˆµÙ˜ˆ°˜‰Í…±”õíÝ}É•Íôéí¡}É•Íôé™½É•}½É¥¥¹…±}…ÍÁ•Ñ}É…Ñ¥¼õ‘•É•…Í”±Á…õíÝ}É•Íôéí¡}É•Íôè¡½Üµ¥Ü¤¼Èè¡½ µ¥ ¤¼È±Í•ÑÍ…ÈôÄ±™ÁÌôÌÀˆ°(€€€€€€€€€€€€ˆµŒéØˆ°€‰ ÈØÑ}¹Ù•¹Œˆ°€ˆµÁÉ•Í•Ðˆ°€‰ÀØˆ°€ˆµÄˆ°€ˆÈÌˆ°(€€€€€€€€€€€€ˆµÁ¥á}™µÐˆ°€‰åÕØÐÈÁÀˆ°(€€€€€€€€€€€É…Ý}Ù¥‘•½}Á…Ñ (€€€€€€€t(€€€€€€€¥µÁ½ÉÐÍÕ‰ÁÉ½•ÍÌ(€€€€€€€ÍÕ‰ÁÉ½•ÍÌ¹ÉÕ¸¡µ‘}¥µœ°ÍÑ‘½ÕÐõÍÕ‰ÁÉ½•ÍÌ¹Y9U10°ÍÑ‘•ÉÈõÍÕ‰ÁÉ½•ÍÌ¹Y9U10°¡•¬õQÉÕ”¤(€€€€€€€€(€€€€€€€ÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸ ØÔ°€‹A…¹œÁ£†îE¤£†îÀƒ‰´Ñ¡…¹ Û€	4¸¸¸ˆ¤(€€€€€€€µ…ÍÑ•É}…Õ‘¥½}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡Ñ•µÁ}‘¥È°€‰µ…ÍÑ•É}…Õ‘¥¼¹µÀÌˆ¤(€€€€€€€É•…Ñ•}µ…ÍÑ•É}…Õ‘¥¼¡…Õ‘¥½}Á…Ñ¡Ì°‰µ}Á…Ñ °‰µ}Ù½±Õµ”°µ…ÍÑ•É}…Õ‘¥½}Á…Ñ °Ñ•µÁ}‘¥È¤(€€€€€€€€(€€€€€€€ÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸ ÜÔ°€‹A…¹œÓ†ê…¼Á£†î”ƒG†îƒG†îe¹œ¡×†ê¥¸ML€¡-…É…½­”½!½Éµ½é¤¤¸¸¸ˆ¤(€€€€€€€…ÍÍ}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡Ñ•µÁ}‘¥È°€‰ÍÕ‰Ñ¥Ñ±•Ì¹…ÍÌˆ¤(€€€€€€€€Œ•¹•É…Ñ”ML™½È$Y¥‘•¼(€€€€€€€ÍÕ‰µ…­•É}…ÍÌ€ô½Ì¹Á…Ñ ¹©½¥¸¡Ñ•µÁ}‘¥È°€‰ÍÕ‰µ…­•É}ÍÕ‰Ñ¥Ñ±•Ì¹…ÍÌˆ¤(€€€€€€€¥˜½Ì¹Á…Ñ ¹•á¥ÍÑÌ¡ÍÕ‰µ…­•É}…ÍÌ¤è(€€€€€€€€€€€¥µÁ½ÉÐÍ¡ÕÑ¥°(€€€€€€€€€€€Í¡ÕÑ¥°¹½ÁäÈ¡ÍÕ‰µ…­•É}…ÍÌ°…ÍÍ}Á…Ñ ¤(€€€€€€€•±Í”è(€€€€€€€€€€€É•…Ñ•}ÍÉÐ¡Í•¹Ñ•¹•Ì°‘ÕÉ…Ñ¥½¹Ì°…ÍÍ}Á…Ñ ¤(€€€€€€€€(€€€€€€€ÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸ àÔ°€‹A…¹œ£¥ÀÙ¥‘•¼Û€¹Õ¹œÁ£†î”ƒG†î€¡!…É‘ÍÕˆ¤¸¸¸ˆ¤(€€€€€€€€(€€€€€€€½ÕÑÁÕÑ}™¥±•¹…µ”€ô˜‰…¥}Ù¥‘•½}íÑ…Í­}¥‘ô¹µÀÐˆ(€€€€€€€¥µÁ½ÉÐ½Ì(€€€€€€€½ÕÑÁÕÑ}‘¥È€ô½Ì¹Á…Ñ ¹©½¥¸¡½Ì¹Á…Ñ ¹‘¥É¹…µ”¡½Ì¹Á…Ñ ¹‘¥É¹…µ”¡Ñ•µÁ}‘¥È¤¤°€‰ÍÑ…Ñ¥Œˆ°€‰½ÕÑÁÕÐˆ¤(€€€€€€€½Ì¹µ…­•‘¥ÉÌ¡½ÕÑÁÕÑ}‘¥È°•á¥ÍÑ}½¬õQÉÕ”¤(€€€€€€€½ÕÑÁÕÑ}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡½ÕÑÁÕÑ}‘¥È°½ÕÑÁÕÑ}™¥±•¹…µ”¤(€€€€€€€€(€€€€€€€™½¹Ñ}Í¥é•}ÕÍ•È€ô¥¹Ð¡½ÁÑ¥½¹Ì¹•Ð ‰™½¹Ñ}Í¥é”ˆ°€ÐÈ¤¤(€€€€€€€™½¹Ñ}½±½É}¡•à€ô½ÁÑ¥½¹Ì¹•Ð ‰™½¹Ñ}½±½Èˆ°€ˆˆ¤¹±ÍÑÉ¥À œŒœ¤(€€€€€€€¥˜±•¸¡™½¹Ñ}½±½É}¡•à¤€ôô€Ìè™½¹Ñ}½±½É}¡•à€ô€œœ¹©½¥¸¡mŒ¨È™½ÈŒ¥¸™½¹Ñ}½±½É}¡•át¤(€€€€€€€•±¥˜±•¸¡™½¹Ñ}½±½É}¡•à¤€„ô€Øè™½¹Ñ}½±½É}¡•à€ô€‰ˆ(€€€€€€€‰}½Á…¥Ñä€ô¥¹Ð¡½ÁÑ¥½¹Ì¹•Ð ‰‰}½Á…¥Ñäˆ°€ÄÔÀ¤¤(€€€€€€€‰}…±Á¡…}¡•à€ô˜‰í‰}½Á…¥ÑäèÀÉaôˆ(€€€€€€€€(€€€€€€€…ÍÍ}™½¹Ñ}Í¥é”€ôµ…à ÄÈ°¥¹Ð¡™½¹Ñ}Í¥é•}ÕÍ•È€¨€ÌàÐ€¼¡}É•Ì¤¤(€€€€€€€È€ô™½¹Ñ}½±½É}¡•álÀèÉt(€€€€€€€œ€ô™½¹Ñ}½±½É}¡•álÈèÑt(€€€€€€€ˆ€ô™½¹Ñ}½±½É}¡•álÐèÙt(€€€€€€€ÁÉ¥µ…Éå}½±½È€ô˜ˆ™ ÀÁí‰õíõíÉôˆ(€€€€€€€ÍÑå±”€ô€ (€€€€€€€€€€€˜‰½¹Ñ¹…µ”õÉ¥…°±½¹ÑÍ¥é”õí…ÍÍ}™½¹Ñ}Í¥é•ô±	½±ôÄ°ˆ(€€€€€€€€€€€˜‰AÉ¥µ…Éå½±½ÕÈõíÁÉ¥µ…Éå}½±½Éô°ˆ(€€€€€€€€€€€˜‰=ÕÑ±¥¹•½±½ÕÈô™ ÀÀÀÀÀÀÀÀ°ˆ(€€€€€€€€€€€˜‰	…­½±½ÕÈô™ àÀÀÀÀÀÀÀ°ˆ(€€€€€€€€€€€˜‰	½É‘•ÉMÑå±”ôÄ°ˆ(€€€€€€€€€€€˜‰=ÕÑ±¥¹”ôÈ¸Ô±M¡…‘½ÜôÄ¸Ô°ˆ(€€€€€€€€€€€˜‰5…É¥¹0ôØÀ±5…É¥¹HôØÀ±5…É¥¹XôÈÔ°ˆ(€€€€€€€€€€€˜‰]É…ÁMÑå±”ôÀ±MÁ…¥¹œôÀ¸Ôˆ(€€€€€€€€¤(€€€€€€€€(€€€€€€€¥µÁ½ÉÐÍ¡ÕÑ¥°(€€€€€€€…ÍÍ}½Áå}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡½ÕÑÁÕÑ}‘¥È°˜‰Ñ•µÁ}ÍÕ‰}íÑ…Í­}¥‘ô¹…ÍÌˆ¤(€€€€€€€Í¡ÕÑ¥°¹½ÁäÈ¡…ÍÍ}Á…Ñ °…ÍÍ}½Áå}Á…Ñ ¤€ŒÍÍÕµ¥¹œ…ÍÍ}Á…Ñ ¥ÌÁ…ÍÍ•½È…Ù…¥±…‰±”(€€€€€€€€(€€€€€€€¥µÁ½ÉÐÁ…Ñ¡±¥ˆ(€€€€€€€…ÍÍ}™™µÁ•œ€ôÍÑÈ¡Á…Ñ¡±¥ˆ¹A…Ñ ¡…ÍÍ}½Áå}Á…Ñ ¤¹…‰Í½±ÕÑ” ¤¤¹É•Á±…” qpœ°€œ¼œ¤¹É•Á±…” œèœ°€qpèœ¤(€€€€€€€€ŒQ…Í¬€Ìè#†îä‹†î<€È“†ê¤ƒE•¸¥¹•µ…Ñ¥Œ(€€€€€€€Ù™}™¥±Ñ•È€ô˜‰…ÍÌôí…ÍÍ}™™µÁ•ôœˆ(€€€€€€€€(€€€€€€€µ‘}™¥¹…°€ôl(€€€€€€€€€€€™™µÁ•}•á”°€ˆµäˆ°(€€€€€€€€€€€€ˆµ¤ˆ°É…Ý}Ù¥‘•½}Á…Ñ °(€€€€€€€€€€€€ˆµ¤ˆ°µ…ÍÑ•É}…Õ‘¥½}Á…Ñ °(€€€€€€€€€€€€ˆµÙ˜ˆ°Ù™}™¥±Ñ•È°(€€€€€€€€€€€€ˆµŒéØˆ°€‰ ÈØÑ}¹Ù•¹Œˆ°€ˆµÁÉ•Í•Ðˆ°€‰ÀØˆ°€ˆµÄˆ°€ˆÈÌˆ°(€€€€€€€€€€€€ˆµŒé„ˆ°€‰……Œˆ°(€€€€€€€€€€€€ˆµÍ¡½ÉÑ•ÍÐˆ°(€€€€€€€€€€€½ÕÑÁÕÑ}Á…Ñ (€€€€€€€t(€€€€€€€ÍÕ‰ÁÉ½•ÍÌ¹ÉÕ¸¡µ‘}™¥¹…°°ÍÑ‘½ÕÐõÍÕ‰ÁÉ½•ÍÌ¹Y9U10°ÍÑ‘•ÉÈõÍÕ‰ÁÉ½•ÍÌ¹Y9U10°¡•¬õQÉÕ”¤(€€€€€€€€(€€€€€€€ÑÉäè½Ì¹É•µ½Ù”¡…ÍÍ}½Áå}Á…Ñ ¤(€€€€€€€•á•ÁÐèÁ…ÍÌ(€€€€€€€€(€€€€€€€ÕÁ‘…Ñ•}ÍÑ…ÑÕÍ}™¸ ÄÀÀ°˜‰!¿¸Ñ£¹ á×†ê•Ð$Y¥‘•¼5@Ð„ˆ¤(€€€€€€€}±•…¹ÕÁ}½±‘}½ÕÑÁÕÑÌ¡½ÕÑÁÕÑ}‘¥È¤(€€€€€€€É•ÑÕÉ¸˜ˆ½ÍÑ…Ñ¥Œ½½ÕÑÁÕÐ½í½ÕÑÁÕÑ}™¥±•¹…µ•ôˆ(€€€€€€€€(€€€™¥¹…±±äè(€€€€€€€±•…¹ÕÁ}Ñ•µÁ}™¥±•Ì¡Ñ•µÁ}‘¥È¤