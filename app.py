import os
import uuid
import threading
import shutil
import subprocess
import json
import time
import re
import traceback
from flask import Flask, request, jsonify, render_template, send_from_directory
from google import genai
import asyncio
import edge_tts
from tts_engine import (
    extract_time_range,
    parse_sentences_and_time_ranges,
    remove_non_story_timeline_rows,
    split_text_into_sentences,
)
from video_renderer import render_review_video_ffmpeg, export_capcut_package, generate_all_images, render_ai_image_video_ffmpeg

# =====================================================================
# Cáº¥u hÃ¬nh MÃ´i trÆ°á»ng
# =====================================================================
WORKSPACE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(WORKSPACE_DIR, "app_settings.json")

# Táº¡o cÃ¡c thÆ° má»¥c cáº§n thiáº¿t
os.makedirs(os.path.join(WORKSPACE_DIR, "templates"), exist_ok=True)
os.makedirs(os.path.join(WORKSPACE_DIR, "static", "css"), exist_ok=True)
os.makedirs(os.path.join(WORKSPACE_DIR, "static", "js"), exist_ok=True)
os.makedirs(os.path.join(WORKSPACE_DIR, "static", "bgm"), exist_ok=True)
os.makedirs(os.path.join(WORKSPACE_DIR, "static", "output"), exist_ok=True)
os.makedirs(os.path.join(WORKSPACE_DIR, "static", "temp_preview"), exist_ok=True)

def _load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as settings_file:
            return json.load(settings_file)
    except (OSError, json.JSONDecodeError):
        return {}

def _save_settings(settings):
    temp_path = SETTINGS_PATH + ".tmp"
    with open(temp_path, "w", encoding="utf-8", newline="\n") as settings_file:
        json.dump(settings, settings_file, ensure_ascii=False, indent=2)
        settings_file.flush()
        os.fsync(settings_file.fileno())
    os.replace(temp_path, SETTINGS_PATH)

def get_configured_export_directory():
    export_dir = _load_settings().get("export_directory", "")
    if not isinstance(export_dir, str):
        return ""
    export_dir = export_dir.strip().strip('"')
    return os.path.abspath(os.path.normpath(export_dir)) if export_dir else ""

def get_export_directory():
    export_dir = get_configured_export_directory()
    return export_dir if export_dir and os.path.isdir(export_dir) else ""

export_copy_lock = threading.Lock()

def copy_result_to_export_directory(result_url):
    """Atomically copy a completed MP4/ZIP and verify its size."""
    configured_dir = get_configured_export_directory()
    if not configured_dir:
        return "", ""
    if not os.path.isdir(configured_dir):
        return "", f"Thu muc xuat khong ton tai: {configured_dir}. Vui long chon lai."

    filename = os.path.basename(result_url.split("?", 1)[0].rstrip("/\\"))
    source_path = os.path.join(WORKSPACE_DIR, "static", "output", filename)
    if not os.path.isfile(source_path):
        return "", "Khong tim thay file ket qua de sao chep."

    with export_copy_lock:
        target_path = os.path.join(configured_dir, filename)
        stem, extension = os.path.splitext(filename)
        counter = 1
        while os.path.exists(target_path):
            target_path = os.path.join(configured_dir, f"{stem}_{counter}{extension}")
            counter += 1

        partial_path = target_path + f".{uuid.uuid4().hex}.partial"
        last_error = None
        for attempt in range(3):
            try:
                shutil.copy2(source_path, partial_path)
                if os.path.getsize(partial_path) != os.path.getsize(source_path):
                    raise OSError("Kich thuoc file sau khi chep khong khop file goc.")
                os.replace(partial_path, target_path)
                if not os.path.isfile(target_path):
                    raise OSError("File dich khong ton tai sau khi chep.")
                return target_path, ""
            except OSError as exc:
                last_error = exc
                try:
                    if os.path.exists(partial_path):
                        os.remove(partial_path)
                except OSError:
                    pass
                if attempt < 2:
                    time.sleep(1)
        return "", f"Khong chep duoc video vao '{configured_dir}': {last_error}"

app = Flask(__name__, static_folder=os.path.join(WORKSPACE_DIR, "static"), template_folder=os.path.join(WORKSPACE_DIR, "templates"))
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024 * 1024  # 10GB cho video dai

# KhÃ³a báº£o máº­t máº·c Ä‘á»‹nh
DEFAULT_API_KEY = ""

# =====================================================================
# Quáº£n lÃ½ Tiáº¿n TrÃ¬nh Cháº¡y Ngáº§m (Thread-safe)
# =====================================================================
TASKS = {}
PROJECTS = {}
task_lock = threading.Lock()

# Whitelist dinh dang file duoc phep upload
ALLOWED_VIDEO_EXT = {'.mp4', '.mkv', '.avi', '.mov', '.webm'}
ALLOWED_AUDIO_EXT = {'.mp3', '.wav', '.m4a', '.ogg'}

def create_task(project_id="", video_name=""):
    task_id = str(uuid.uuid4())
    with task_lock:
        TASKS[task_id] = {
            "status": "running",
            "progress": 0,
            "message": "Dang khoi tao...",
            "result": None,
            "project_id": project_id,
            "video_name": video_name,
            "created_at": __import__('time').time(),
            "logs": []
        }
    return task_id

def append_task_log(task_id, level, message, details=""):
    """Store diagnostic events so the browser can show the real backend log."""
    entry = {
        "time": time.strftime("%H:%M:%S"),
        "level": str(level or "INFO").upper(),
        "message": str(message),
    }
    if details:
        entry["details"] = str(details)
    with task_lock:
        task = TASKS.get(task_id)
        if task is not None:
            task.setdefault("logs", []).append(entry)
            # Prevent an unusually long run from growing memory forever.
            if len(task["logs"]) > 2000:
                task["logs"] = task["logs"][-2000:]
    print(f"[{entry['time']}] [{entry['level']}] [task {task_id[:8]}] {entry['message']}", flush=True)
    if details:
        print(details, flush=True)

def _hard_limit_timestamp_script(script_text, max_words):
    """Last-resort duration guard which preserves coverage through the ending."""
    words = script_text.split()
    if len(words) <= max_words:
        return script_text
    blocks = [part.strip() for part in re.split(r'(?=\[TIME=[^\]]+\])', script_text) if part.strip()]
    if len(blocks) < 2:
        head_count = int(max_words * 0.7)
        return " ".join(words[:head_count] + words[-(max_words - head_count):])

    block_words = [block.split() for block in blocks]
    total = sum(len(items) for items in block_words)
    budgets = [max(6, int(max_words * len(items) / max(total, 1))) for items in block_words]
    while sum(budgets) > max_words:
        largest = max(range(len(budgets)), key=lambda i: budgets[i])
        if budgets[largest] <= 6:
            break
        budgets[largest] -= 1
    while sum(budgets) < max_words:
        largest = max(range(len(budgets)), key=lambda i: len(block_words[i]) - budgets[i])
        budgets[largest] += 1

    compacted = []
    for index, (items, budget) in enumerate(zip(block_words, budgets)):
        if len(items) <= budget:
            compacted.extend(items)
        elif index == len(block_words) - 1:
            # Keep the timestamp plus the actual conclusion at the end.
            compacted.append(items[0])
            compacted.extend(items[-max(0, budget - 1):])
        else:
            compacted.extend(items[:budget])
    return " ".join(compacted[:max_words])


def _bg_cleanup_loop():
    """Tu dong don dep: preview TTS cu + TASKS da hoan thanh."""
    import time
    preview_dir = os.path.join(WORKSPACE_DIR, "static", "temp_preview")
    while True:
        try:
            now = time.time()
            # Don dep preview TTS cu hon 5 phut
            for filename in os.listdir(preview_dir):
                filepath = os.path.join(preview_dir, filename)
                if os.path.isfile(filepath) and now - os.path.getmtime(filepath) > 300:
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
            # Don dep TASKS da hoan thanh/that bai hon 30 phut
            with task_lock:
                expired = [k for k, v in TASKS.items()
                          if v["status"] in ("success", "failed")
                          and now - v.get("created_at", 0) > 1800]
                for k in expired:
                    del TASKS[k]
        except Exception:
            pass
        time.sleep(600)

threading.Thread(target=_bg_cleanup_loop, daemon=True).start()

# =====================================================================
# BACKGROUND TASK RUNNERS
# =====================================================================
def analyze_video_bg(task_id, api_key, model_name, video_filename, style, custom_prompt, enable_multi_voice=False, review_minutes="auto"):
    def update_prog(prog, msg):
        with task_lock:
            TASKS[task_id]["progress"] = prog
            TASKS[task_id]["message"] = msg

    try:
        video_path = os.path.join(WORKSPACE_DIR, video_filename)
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Khong tim thay video: {video_filename}")

        update_prog(10, "Dang cau hinh ket noi API Gemini...")
        client = genai.Client(api_key=api_key)

        # === Nen video truoc khi upload ===
        update_prog(15, "Dang nen video de upload nhanh hon...")
        from video_renderer import get_ffmpeg_exe, get_video_info
        ffmpeg_exe = get_ffmpeg_exe()
        
        compressed_path = video_path.rsplit('.', 1)[0] + "_gemini_temp.mp4"
        try:
            orig_dur, orig_w, orig_h = get_video_info(video_path)
            orig_size_mb = os.path.getsize(video_path) / (1024 * 1024)
            need_compress = orig_size_mb > 50 or orig_h > 720
            
            if need_compress:
                update_prog(12, f"Dang nen video ({orig_size_mb:.0f}MB -> nho hon)... Co the mat vai phut.")
                
                # Tinh CRF phu hop theo do dai video de dam bao < 2GB sau nen
                # Target: ~500MB cho video 1h, ~200MB cho video 30p
                if orig_dur > 1800:  # > 30 phut
                    crf_val = "32"
                    audio_br = "48k"
                    target_fps = "15"
                    scale_h = 480
                elif orig_dur > 600:  # > 10 phut
                    crf_val = "30"
                    audio_br = "56k"
                    target_fps = "20"
                    scale_h = 540
                else:
                    crf_val = "28"
                    audio_br = "64k"
                    target_fps = "24"
                    scale_h = 720
                
                scale_filter = f"scale=-2:{scale_h}" if orig_h > scale_h else ""
                vf_parts = [f for f in [scale_filter] if f]
                vf_arg = ["-vf", ",".join(vf_parts)] if vf_parts else []
                
                cmd_compress = [
                    ffmpeg_exe, "-y",
                    "-i", video_path,
                    *vf_arg,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", crf_val,
                    "-c:a", "aac", "-b:a", audio_br,
                    "-r", target_fps,
                    compressed_path
                ]
                res = subprocess.run(cmd_compress, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     timeout=1800)  # Timeout 30 phut cho nen
                
                if res.returncode == 0 and os.path.exists(compressed_path):
                    compressed_mb = os.path.getsize(compressed_path) / (1024 * 1024)
                    update_prog(18, f"Da nen: {orig_size_mb:.0f}MB -> {compressed_mb:.0f}MB")
                    upload_path = compressed_path
                else:
                    upload_path = video_path
            else:
                upload_path = video_path
        except subprocess.TimeoutExpired:
            update_prog(18, "Nen video qua lau, dung file goc...")
            upload_path = video_path
        except Exception:
            upload_path = video_path

        update_prog(20, "Dang tai video len Gemini...")
        import time
        
        # Cau hinh timeout theo dung luong file
        upload_size_mb = os.path.getsize(upload_path) / (1024 * 1024)
        upload_timeout = max(120, int(upload_size_mb * 3))  # ~3s/MB, toi thieu 2 phut
        
        from google.genai import types as genai_types
        http_opts = genai_types.HttpOptions(timeout=upload_timeout * 1000)  # ms
        client_upload = genai.Client(api_key=api_key, http_options=http_opts)
        
        update_prog(20, f"Dang tai video ({upload_size_mb:.0f}MB) len Gemini...")
        
        # Upload voi retry cho 429 + connection errors
        video_file = None
        last_upload_error = None
        for upload_attempt in range(5):
            try:
                video_file = client_upload.files.upload(file=upload_path)
                break
            except Exception as upload_err:
                last_upload_error = upload_err
                err_str = str(upload_err)
                print(f"[!] Upload lan {upload_attempt+1}/5 that bai: {err_str[:200]}")
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower():
                    wait_sec = 30 * (upload_attempt + 1)
                    update_prog(20, f"Het quota tam thoi, cho {wait_sec}s roi thu lai...")
                    time.sleep(wait_sec)
                elif any(k in err_str for k in ["10053", "ReadError", "ConnectionError", "ConnectionReset"]) or "aborted" in err_str.lower() or "timeout" in err_str.lower():
                    wait_sec = 10 * (upload_attempt + 1)
                    update_prog(20, f"Ket noi bi ngat, thu lai lan {upload_attempt+2}/5... ({wait_sec}s)")
                    time.sleep(wait_sec)
                else:
                    raise
        if video_file is None:
            err_detail = str(last_upload_error)[:200] if last_upload_error else "Unknown"
            if "429" in err_detail or "quota" in err_detail.lower():
                raise Exception("Da het quota API mien phi! Quota tu reset sau 24h.")
            else:
                raise Exception(f"Upload that bai sau 5 lan. Loi: {err_detail}")
        
        # Xoa file nen tam
        if upload_path != video_path and os.path.exists(compressed_path):
            try:
                os.remove(compressed_path)
            except Exception:
                pass

        # Doi file ACTIVE - voi retry cho files.get 404
        update_prog(40, "Dang cho Gemini xu ly video...")
        max_wait = 900  # 15 phut cho video dai
        waited = 0
        file_ready = False
      ÷]y¶‰žËkºwµçA¡ÑÑÁÌè¼½…¥ÍÑÕ‘¥¼¹½½±”¹½´½…Á¥­•ä‰ô¤(€€€€€€€¥˜€ˆÐÈäˆ¥¸•ÉÉ}µÍœ½È€‰IM=UI}a!UMQˆ¥¸•ÉÉ}µÍœ½È€‰ÅÕ½Ñ„ˆ¥¸•ÉÉ}µÍœ¹±½Ý•È ¤è(€€€€€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰Ù…±¥ˆèQÉÕ”°€‰µ•ÍÍ…”ˆè€‰A$-•ä£†îÀ³†î„9£Á¹œƒGŒ£†êýÐÅÕ½Ñ„µ§†î¸Á£´£Ñ´¹…ä¸EÕ½Ñ„Ï†êôÉ•Í•ÐÍ…Ô€ÈÑ ¸ˆ°€‰µ½‘•±Ìˆèl‰•µ¥¹¤´È¸Àµ™±…Í ˆ°€‰•µ¥¹¤´È¸Ôµ™±…Í ˆ°€‰•µ¥¹¤´È¸Àµ™±…Í µ±¥Ñ”‰uô¤(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰Ù…±¥ˆè…±Í”°€‰µ•ÍÍ…”ˆè˜‰3†î]¤¯†êýÐ»†îE¤èí•ÉÉ}µÍlèÈÀÁuô‰ô¤()…ÁÀ¹É½ÕÑ” ˆ½…Á¤½±¥ÍÐµÙ¥‘•½Ìˆ°µ•Ñ¡½‘Ìõl‰P‰t¤)‘•˜±¥ÍÑ}Ù¥‘•½Ì ¤è(€€€™¥±•Ì€ô½Ì¹±¥ÍÑ‘¥È¡]=I-MA}%H¤(€€€Ù¥‘•½Ì€ôm˜™½È˜¥¸™¥±•Ì¥˜˜¹±½Ý•È ¤¹•¹‘ÍÝ¥Ñ   œ¹µÀÐœ°€œ¹µ­Øœ°€œ¹…Ù¤œ°€œ¹µ½Øœ¤¥t(€€€É•ÑÕÉ¸©Í½¹¥™ä¡Ù¥‘•½Ì¤()…ÁÀ¹É½ÕÑ” ˆ½…Á¤½‘•‰Õœµµ½‘•±Ìˆ°µ•Ñ¡½‘Ìõl‰A=MP‰t¤)‘•˜‘•‰Õ}µ½‘•±Ì ¤è(€€€‘…Ñ„€ôÉ•ÅÕ•ÍÐ¹©Í½¸½Èíô(€€€…¬€ô‘…Ñ„¹•Ð ‰…Á¥}­•äˆ°€ˆˆ¤¹ÍÑÉ¥À ¤(€€€¥˜¹½Ð…¬è(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰•ÉÉ½Èˆè€‰9¼A$­•ä‰ô¤(€€€ÑÉäè(€€€€€€€±¥•¹Ð€ô•¹…¤¹±¥•¹Ð¡…Á¥}­•äõ…¬¤(€€€€€€€É•ÍÕ±ÑÌ€ômt(€€€€€€€™½È´¥¸±¥•¹Ð¹µ½‘•±Ì¹±¥ÍÐ ¤è(€€€€€€€€€€€É•ÍÕ±ÑÌ¹…ÁÁ•¹¡´¹¹…µ”¥˜¡…Í…ÑÑÈ¡´°€¹…µ”œ¤•±Í”ÍÑÈ¡´¤¤(€€€€€€€€(€€€€€€€Ñ•ÍÑ}µ½‘•±Ì€ôl‰•µ¥¹¤´È¸Àµ™±…Í ˆ°€‰•µ¥¹¤´È¸Ôµ™±…Í ˆ°€‰•µ¥¹¤´È¸Àµ™±…Í µ±¥Ñ”‰t(€€€€€€€Ý½É­¥¹œ€ômt(€€€€€€€™½ÈÑ´¥¸Ñ•ÍÑ}µ½‘•±Ìè(€€€€€€€€€€€ÑÉäè(€€€€€€€€€€€€€€€É•ÍÀ€ô±¥•¹Ð¹µ½‘•±Ì¹•¹•É…Ñ•}½¹Ñ•¹Ð¡µ½‘•°õÑ´°½¹Ñ•¹ÑÌõl‰;Í¤€á¥¸£¼œ¸‰t¤(€€€€€€€€€€€€€€€¥˜É•ÍÀ…¹É•ÍÀ¹Ñ•áÐè(€€€€€€€€€€€€€€€€€€€Ý½É­¥¹œ¹…ÁÁ•¹¡ì‰µ½‘•°ˆèÑ´°€‰É•ÍÁ½¹Í”ˆèÉ•ÍÀ¹Ñ•áÑlèÔÁuô¤(€€€€€€€€€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€€€€€€€€€Ý½É­¥¹œ¹…ÁÁ•¹¡ì‰µ½‘•°ˆèÑ´°€‰•ÉÉ½ÈˆèÍÑÈ¡”¥lèÄÀÁuô¤(€€€€€€€€(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰…±±}µ½‘•±ÌˆèÉ•ÍÕ±ÑÍlèÌÁt°€‰Ñ•ÍÑ}É•ÍÕ±ÑÌˆèÝ½É­¥¹ô¤(€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰•ÉÉ½ÈˆèÍÑÈ¡”¥lèÌÀÁuô¤()…ÁÀ¹É½ÕÑ” œ½…Á¤½±¥ÍÐµ‰´œ°µ•Ñ¡½‘ÌõlPt¤)‘•˜±¥ÍÑ}‰´ ¤è(€€€‰µ}‘¥È€ô½Ì¹Á…Ñ ¹©½¥¸¡…ÁÀ¹ÍÑ…Ñ¥}™½±‘•È°€‰´œ¤(€€€½Ì¹µ…­•‘¥ÉÌ¡‰µ}‘¥È°•á¥ÍÑ}½¬õQÉÕ”¤(€€€™¥±•Ì€ôm˜™½È˜¥¸½Ì¹±¥ÍÑ‘¥È¡‰µ}‘¥È¤¥˜˜¹±½Ý•È ¤¹•¹‘ÍÝ¥Ñ   œ¹µÀÌœ°€œ¹Ý…Øœ°€œ¹´Ñ„œ°€œ¹……Œœ¤¥t(€€€É•ÑÕÉ¸©Í½¹¥™ä¡™¥±•Ì¤()…ÁÀ¹É½ÕÑ” œ½…Á¤½±¥ÍÐµÍ™àœ°µ•Ñ¡½‘ÌõlPt¤)‘•˜±¥ÍÑ}Í™à ¤è(€€€Í™á}‘¥È€ô½Ì¹Á…Ñ ¹©½¥¸¡…ÁÀ¹ÍÑ…Ñ¥}™½±‘•È°€Í™àœ¤(€€€½Ì¹µ…­•‘¥ÉÌ¡Í™á}‘¥È°•á¥ÍÑ}½¬õQÉÕ”¤(€€€™¥±•Ì€ôm˜™½È˜¥¸½Ì¹±¥ÍÑ‘¥È¡Í™á}‘¥È¤¥˜˜¹±½Ý•È ¤¹•¹‘ÍÝ¥Ñ   œ¹µÀÌœ°€œ¹Ý…Øœ°€œ¹´Ñ„œ°€œ¹……Œœ°€œ¹½œœ¤¥t(€€€É•ÑÕÉ¸©Í½¹¥™ä¡™¥±•Ì¤()…ÁÀ¹É½ÕÑ” ˆ½…Á¤½ÕÁ±½…µ™¥±”ˆ°µ•Ñ¡½‘Ìõl‰A=MP‰t¤)‘•˜ÕÁ±½…‘}™¥±” ¤è(€€€¥˜€™¥±”œ¹½Ð¥¸É•ÅÕ•ÍÐ¹™¥±•Ìè(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆè…±Í”°€‰µ•ÍÍ…”ˆè€‰-¡½¹œÑ¥´Ñ¡…äÑ•À¸‰ô¤(€€€€€€€€(€€€™¥±”€ôÉ•ÅÕ•ÍÐ¹™¥±•Íl™¥±”t(€€€™¥±•}ÑåÁ”€ôÉ•ÅÕ•ÍÐ¹™½É´¹•Ð ‰ÑåÁ”ˆ°€‰Ù¥‘•¼ˆ¤(€€€€(€€€¥˜™¥±”¹™¥±•¹…µ”€ôô€œœè(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆè…±Í”°€‰µ•ÍÍ…”ˆè€‰Q•¸Ñ•À­¡½¹œ¡½À±”¸‰ô¤(€€€€€€€€(€€€¥˜™¥±”è(€€€€€€€™¥±•¹…µ”€ô™¥±”¹™¥±•¹…µ”(€€€€€€€•áÐ€ô½Ì¹Á…Ñ ¹ÍÁ±¥Ñ•áÐ¡™¥±•¹…µ”¥lÅt¹±½Ý•È ¤(€€€€€€€€(€€€€€€€€Œ-¥•´ÑÉ„‘¥¹ ‘…¹œ™¥±”(€€€€€€€¥˜™¥±•}ÑåÁ”€ôô€‰Ù¥‘•¼ˆè(€€€€€€€€€€€¥˜•áÐ¹½Ð¥¸11=]}Y%=}aPè(€€€€€€€€€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆè…±Í”°€‰µ•ÍÍ…”ˆè˜‰¥¹ ‘…¹œí•áÑô­¡½¹œ¡¼ÑÉ¼¸¡¤¡…À¹¡…¸èìœ°€œ¹©½¥¸¡11=]}Y%=}aP¥ô‰ô¤(€€€€€€€€€€€Ñ…É•Ñ}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡]=I-MA}%H°™¥±•¹…µ”¤(€€€€€€€•±Í”è(€€€€€€€€€€€¥˜•áÐ¹½Ð¥¸11=]}U%=}aPè(€€€€€€€€€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆè…±Í”°€‰µ•ÍÍ…”ˆè˜‰¥¹ ‘…¹œí•áÑô­¡½¹œ¡¼ÑÉ¼¸¡¤¡…À¹¡…¸èìœ°€œ¹©½¥¸¡11=]}U%=}aP¥ô‰ô¤(€€€€€€€€€€€Ñ…É•Ñ}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡]=I-MA}%H°€‰ÍÑ…Ñ¥Œˆ°€‰‰´ˆ°™¥±•¹…µ”¤(€€€€€€€€€€€€(€€€€€€€™¥±”¹Í…Ù”¡Ñ…É•Ñ}Á…Ñ ¤(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆèQÉÕ”°€‰™¥±•¹…µ”ˆè™¥±•¹…µ•ô¤(€€€€€€€€(€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆè…±Í”°€‰µ•ÍÍ…”ˆè€‰1½¤­¡½¹œá…Œ‘¥¹ ­¡¤±ÕÔ™¥±”¸‰ô¤()…ÁÀ¹É½ÕÑ” ˆ½…Á¤½…¹…±åé”µÙ¥‘•¼ˆ°µ•Ñ¡½‘Ìõl‰A=MP‰t¤)‘•˜…¹…±åé•}Ù¥‘•¼ ¤è(€€€‘…Ñ„€ôÉ•ÅÕ•ÍÐ¹©Í½¸½Èíô(€€€…Á¥}­•ä€ô‘…Ñ„¹•Ð ‰…Á¥}­•äˆ°€ˆˆ¤¹ÍÑÉ¥À ¤½ÈU1Q}A%}-d(€€€µ½‘•±}¹…µ”€ô‘…Ñ„¹•Ð ‰µ½‘•±}¹…µ”ˆ°€‰•µ¥¹¤´Ä¸Ôµ™±…Í ˆ¤(€€€Ù¥‘•½}™¥±•¹…µ”€ô‘…Ñ„¹•Ð ‰Ù¥‘•½}¹…µ”ˆ°€ˆˆ¤(€€€ÍÑå±”€ô‘…Ñ„¹•Ð ‰ÍÑå±”ˆ°€‰‘É…µ…Ñ¥Œˆ¤(€€€ÕÍÑ½µ}ÁÉ½µÁÐ€ô‘…Ñ„¹•Ð ‰ÕÍÑ½µ}ÁÉ½µÁÐˆ°€ˆˆ¤¹ÍÑÉ¥À ¤(€€€•¹…‰±•}µÕ±Ñ¥}Ù½¥”€ô‘…Ñ„¹•Ð ‰•¹…‰±•}µÕ±Ñ¥}Ù½¥”ˆ°…±Í”¤(€€€ÁÉ½©•Ñ}¥€ôÍÑÈ¡‘…Ñ„¹•Ð ‰ÁÉ½©•Ñ}¥ˆ°€ˆˆ¤¤¹ÍÑÉ¥À ¤½ÈÍÑÈ¡ÕÕ¥¹ÕÕ¥Ð ¤¤(€€€É•Ù¥•Ý}µ¥¹ÕÑ•Í}É…Ü€ô‘…Ñ„¹•Ð ‰É•Ù¥•Ý}µ¥¹ÕÑ•Ìˆ°€‰…ÕÑ¼ˆ¤(€€€É•Ù¥•Ý}µ¥¹ÕÑ•Ì€ô€‰…ÕÑ¼ˆ¥˜ÍÑÈ¡É•Ù¥•Ý}µ¥¹ÕÑ•Í}É…Ü¤¹±½Ý•È ¤€ôô€‰…ÕÑ¼ˆ•±Í”µ…à Ì°µ¥¸ ÈÀ°¥¹Ð¡É•Ù¥•Ý}µ¥¹ÕÑ•Í}É…Ü¤¤¤(€€€€(€€€¥˜¹½ÐÙ¥‘•½}™¥±•¹…µ”è(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆè…±Í”°€‰µ•ÍÍ…”ˆè€‰£Á„£†î5¸Ù¥‘•¼Ÿ†îEŒ¸‰ô¤(€€€€€€€€(€€€Ñ…Í­}¥€ôÉ•…Ñ•}Ñ…Í¬¡ÁÉ½©•Ñ}¥°Ù¥‘•½}™¥±•¹…µ”¤(€€€Ý¥Ñ Ñ…Í­}±½¬è(€€€€€€€AI=)QMmÁÉ½©•Ñ}¥‘t€ôì(€€€€€€€€€€€€‰Ù¥‘•½}¹…µ”ˆèÙ¥‘•½}™¥±•¹…µ”°(€€€€€€€€€€€€‰…¹…±åÍ¥Í}Ñ…Í­}¥ˆèÑ…Í­}¥°(€€€€€€€€€€€€‰É•…Ñ•‘}…ÐˆèÑ¥µ”¹Ñ¥µ” ¤°(€€€€€€€ô(€€€€(€€€€Œ-£†î}¤£†ê…äÑ§†îÔÑË±¹ Á£‰¸Óµ »†î¸(€€€Ñ¡É•…€ôÑ¡É•…‘¥¹œ¹Q¡É•… (€€€€€€€Ñ…É•Ðõ…¹…±åé•}Ù¥‘•½}‰œ°(€€€€€€€…ÉÌô¡Ñ…Í­}¥°…Á¥}­•ä°µ½‘•±}¹…µ”°Ù¥‘•½}™¥±•¹…µ”°ÍÑå±”°ÕÍÑ½µ}ÁÉ½µÁÐ°•¹…‰±•}µÕ±Ñ¥}Ù½¥”°É•Ù¥•Ý}µ¥¹ÕÑ•Ì¤(€€€€¤(€€€Ñ¡É•…¹‘…•µ½¸€ôQÉÕ”(€€€Ñ¡É•…¹ÍÑ…ÉÐ ¤(€€€€(€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆèQÉÕ”°€‰Ñ…Í­}¥ˆèÑ…Í­}¥‘ô¤()…ÁÀ¹É½ÕÑ” ˆ½…Á¤½ÍÁ±¥ÐµÍ•¹Ñ•¹•Ìˆ°µ•Ñ¡½‘Ìõl‰A=MP‰t¤)‘•˜ÍÁ±¥Ñ}Í•¹Ñ•¹•Ì ¤è(€€€‘…Ñ„€ôÉ•ÅÕ•ÍÐ¹©Í½¸½Èíô(€€€Ñ•áÐ€ô‘…Ñ„¹•Ð ‰Ñ•áÐˆ°€ˆˆ¤¹ÍÑÉ¥À ¤(€€€¥˜¹½ÐÑ•áÐè(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡mt¤(€€€€€€€€(€€€Í•¹Ñ•¹•Ì€ôÍÁ±¥Ñ}Ñ•áÑ}¥¹Ñ½}Í•¹Ñ•¹•Ì¡Ñ•áÐ¤(€€€É•ÑÕÉ¸©Í½¹¥™ä¡Í•¹Ñ•¹•Ì¤()…ÁÀ¹É½ÕÑ” ˆ½…Á¤½ÁÉ•Ù¥•ÜµÑÑÌˆ°µ•Ñ¡½‘Ìõl‰A=MP‰t¤)‘•˜ÁÉ•Ù¥•Ý}ÑÑÌ ¤è(€€€‘…Ñ„€ôÉ•ÅÕ•ÍÐ¹©Í½¸½Èíô(€€€Ñ•áÐ€ô‘…Ñ„¹•Ð ‰Ñ•áÐˆ°€ˆˆ¤(€€€Ù½¥”€ô‘…Ñ„¹•Ð ‰Ù½¥”ˆ°€‰Ù¤µY8µ!½…¥5å9•ÕÉ…°ˆ¤(€€€É…Ñ”€ô‘…Ñ„¹•Ð ‰É…Ñ”ˆ°€ˆ¬ÄÀ”ˆ¤(€€€€(€€€¥˜¹½ÐÑ•áÐè(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆè…±Í”°€‰µ•ÍÍ…”ˆè€‰;†îe¤‘Õ¹œ‰ÔÑË†îE¹œ¸‰ô¤(€€€€€€€€(€€€™¥±•¹…µ”€ô˜‰ÁÉ•Ù¥•Ý}íÕÕ¥¹ÕÕ¥Ð ¥ô¹µÀÌˆ(€€€™¥±•Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡]=I-MA}%H°€‰ÍÑ…Ñ¥Œˆ°€‰Ñ•µÁ}ÁÉ•Ù¥•Üˆ°™¥±•¹…µ”¤(€€€€(€€€…Íå¹Œ‘•˜ÉÕ¹}ÑÑÌ ¤è(€€€€€€€½µµÕ¹¥…Ñ”€ô•‘•}ÑÑÌ¹½µµÕ¹¥…Ñ”¡Ñ•áÐ°Ù½¥”°É…Ñ”õÉ…Ñ”¤(€€€€€€€…Ý…¥Ð…Íå¹¥¼¹Ý…¥Ñ}™½È¡½µµÕ¹¥…Ñ”¹Í…Ù”¡™¥±•Á…Ñ ¤°Ñ¥µ•½ÕÐôÄÔ¸À¤(€€€€€€€€(€€€±½½À€ô…Íå¹¥¼¹¹•Ý}•Ù•¹Ñ}±½½À ¤(€€€…Íå¹¥¼¹Í•Ñ}•Ù•¹Ñ}±½½À¡±½½À¤(€€€ÑÉäè(€€€€€€€±½½À¹ÉÕ¹}Õ¹Ñ¥±}½µÁ±•Ñ”¡ÉÕ¹}ÑÑÌ ¤¤(€€€•á•ÁÐ…Íå¹¥¼¹Q¥µ•½ÕÑÉÉ½Èè(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆè…±Í”°€‰µ•ÍÍ…”ˆè€‰QQLÅÕ„Ñ¡½¤¥…¸¡¼€ ÄÕÌ¤¸Q¡Ô±…¤¸‰ô¤(€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆè…±Í”°€‰µ•ÍÍ…”ˆèÍÑÈ¡”¥ô¤(€€€™¥¹…±±äè(€€€€€€€±½½À¹±½Í” ¤(€€€€€€€€(€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆèQÉÕ”°€‰ÕÉ°ˆè˜ˆ½ÍÑ…Ñ¥Œ½Ñ•µÁ}ÁÉ•Ù¥•Ü½í™¥±•¹…µ•ô‰ô¤()…ÁÀ¹É½ÕÑ” ˆ½…Á¤½É•¹‘•ÈµÙ¥‘•¼ˆ°µ•Ñ¡½‘Ìõl‰A=MP‰t¤)‘•˜É•¹‘•É}Ù¥‘•¼ ¤è(€€€‘…Ñ„€ôÉ•ÅÕ•ÍÐ¹©Í½¸½Èíô(€€€Ù¥‘•½}¹…µ”€ô‘…Ñ„¹•Ð ‰Ù¥‘•½}¹…µ”ˆ°€ˆˆ¤(€€€‰µ}¹…µ”€ô‘…Ñ„¹•Ð ‰‰µ}¹…µ”ˆ°€ˆˆ¤(€€€‰µ}Ù½±Õµ”€ô™±½…Ð¡‘…Ñ„¹•Ð ‰‰µ}Ù½±Õµ”ˆ°€ÄÈ¤¤€¼€ÄÀÀ¸À(€€€Ù½¥•}¹…µ”€ô‘…Ñ„¹•Ð ‰Ù½¥•}¹…µ”ˆ°€‰Ù¤µY8µ!½…¥5å9•ÕÉ…°ˆ¤(€€€Ù½¥•}É…Ñ”€ô‘…Ñ„¹•Ð ‰Ù½¥•}É…Ñ”ˆ°€ˆ´ÄÀ”ˆ¤(€€€Í•¹Ñ•¹•Ì€ô‘…Ñ„¹•Ð ‰Í•¹Ñ•¹•Ìˆ°mt¤(€€€ÁÉ½©•Ñ}¥€ôÍÑÈ¡‘…Ñ„¹•Ð ‰ÁÉ½©•Ñ}¥ˆ°€ˆˆ¤¤¹ÍÑÉ¥À ¤(€€€É•Ù¥•Ý}µ¥¹ÕÑ•Í}É…Ü€ô‘…Ñ„¹•Ð ‰É•Ù¥•Ý}µ¥¹ÕÑ•Ìˆ°€‰…ÕÑ¼ˆ¤(€€€€(€€€€Œ†ê•Ô£±¹ Ù¥‘•¼(€€€½ÁÑ¥½¹Ì€ôì(€€€€€€€€‰µ¥ÉÉ½Èˆè‘…Ñ„¹•Ð ‰µ¥ÉÉ½Èˆ°QÉÕ”¤°(€€€€€€€€‰é½½µ}™…Ñ½Èˆè™±½…Ð¡‘…Ñ„¹•Ð ‰é½½µ}™…Ñ½Èˆ°€Ä¸Àà¤¤°(€€€€€€€€‰ÍÁ••‘}™…Ñ½Èˆè™±½…Ð¡‘…Ñ„¹•Ð ‰ÍÁ••‘}™…Ñ½Èˆ°€Ä¸À¤¤°(€€€€€€€€‰½¹ÑÉ…ÍÐˆè™±½…Ð¡‘…Ñ„¹•Ð ‰½¹ÑÉ…ÍÐˆ°€Ä¸ÀÔ¤¤°(€€€€€€€€‰‰É¥¡Ñ¹•ÍÌˆè™±½…Ð¡‘…Ñ„¹•Ð ‰‰É¥¡Ñ¹•ÍÌˆ°€Ä¸À¤¤°(€€€€€€€€‰™½¹Ñ}Í¥é”ˆè¥¹Ð¡‘…Ñ„¹•Ð ‰™½¹Ñ}Í¥é”ˆ°€ÐÈ¤¤°(€€€€€€€€‰™½¹Ñ}½±½Èˆè‘…Ñ„¹•Ð ‰™½¹Ñ}½±½Èˆ°€ˆˆ¤°(€€€€€€€€‰‰}½Á…¥Ñäˆè¥¹Ð¡‘…Ñ„¹•Ð ‰‰}½Á…¥Ñäˆ°€ÄÔÀ¤¤°(€€€€€€€€‰™½¹Ñ}Á…Ñ ˆè€‰éqq]¥¹‘½ÝÍqq½¹ÑÍqq…É¥…±‰¹ÑÑ˜ˆ°(€€€€€€€€‰…ÍÁ•Ñ}É…Ñ¥¼ˆè‘…Ñ„¹•Ð ‰…ÍÁ•Ñ}É…Ñ¥¼ˆ°€ˆäèÄØˆ¤°(€€€€€€€€‰•¹…‰±•}ÍÕ‰Ñ¥Ñ±•Ìˆè‘…Ñ„¹•Ð ‰•¹…‰±•}ÍÕ‰Ñ¥Ñ±•Ìˆ°QÉÕ”¤°(€€€€€€€€‰•¹…‰±•}é½½µ}É½Àˆè‘…Ñ„¹•Ð ‰•¹…‰±•}é½½µ}É½Àˆ°QÉÕ”¤°(€€€€€€€€‰•¹…‰±•}­•¹}‰ÕÉ¹Ìˆè‘…Ñ„¹•Ð ‰•¹…‰±•}­•¹}‰ÕÉ¹Ìˆ°…±Í”¤(€€€ô(€€€¥˜ÍÑÈ¡É•Ù¥•Ý}µ¥¹ÕÑ•Í}É…Ü¤¹±½Ý•È ¤€„ô€‰…ÕÑ¼ˆè(€€€€€€€É•Ù¥•Ý}µ¥¹ÕÑ•Ì€ôµ…à Ì°µ¥¸ ÈÀ°¥¹Ð¡É•Ù¥•Ý}µ¥¹ÕÑ•Í}É…Ü¤¤¤(€€€€€€€½ÁÑ¥½¹Íl‰É•Ù¥•Ý}µ¥¹ÕÑ•Ì‰t€ôÉ•Ù¥•Ý}µ¥¹ÕÑ•Ì(€€€€€€€ÍÁ½­•¹}Ñ•áÐ€ôÉ”¹ÍÕˆ¡Èql üéQ%5ñY=%ñM`¤õmyqut­quðð üéY=%ñM`¤õmxùt¬øœ°€œ€œ°€œ€œ¹©½¥¸¡Í•¹Ñ•¹•Ì¤°™±…ÌõÉ”¹%9=IM¤(€€€€€€€ÍÁ½­•¹}Ý½É‘Ì€ô±•¸¡ÍÁ½­•¹}Ñ•áÐ¹ÍÁ±¥Ð ¤¤(€€€€€€€µ…á}É•¹‘•É}Ý½É‘Ì€ô¥¹Ð¡É•Ù¥•Ý}µ¥¹ÕÑ•Ì€¨€ÄÔÀ€¨€Ä¸ÈÀ¤(€€€€€€€¥˜ÍÁ½­•¹}Ý½É‘Ì€øµ…á}É•¹‘•É}Ý½É‘Ìè(€€€€€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì(€€€€€€€€€€€€€€€€‰ÍÕ•ÍÌˆè…±Í”°(€€€€€€€€€€€€€€€€‰µ•ÍÍ…”ˆè€ (€€€€€€€€€€€€€€€€€€€˜‰-¥ ‰…¸¼íÍÁ½­•¹}Ý½É‘ÍôÑÔ°ÙÕ½ÐµÕŒíÉ•Ù¥•Ý}µ¥¹ÕÑ•ÍôÁ¡ÕÐ€ˆ(€€€€€€€€€€€€€€€€€€€˜ˆ¡Ñ½¤‘„íµ…á}É•¹‘•É}Ý½É‘ÍôÑÔ¤¸!…äÉÕÐ½¸­¥ ‰…¸ÑÉÕ½Œ­¡¤áÕ…Ð¸ˆ(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€ô¤°€ÐÀÀ(€€€€(€€€¥˜¹½ÐÙ¥‘•½}¹…µ”è(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆè…±Í”°€‰µ•ÍÍ…”ˆè€‰£Á„£†î5¸Ù¥‘•¼Ÿ†îEŒ¸‰ô¤(€€€¥˜¹½ÐÍ•¹Ñ•¹•Ìè(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆè…±Í”°€‰µ•ÍÍ…”ˆè€‰/†î- ‹†ê¸ÑË†îE¹œ¸‰ô¤((€€€±•…¹}Ñ¥µ•±¥¹”°É…¹•}ÍÑ…ÉÑÌ°É…¹•}•¹‘Ì€ôÁ…ÉÍ•}Í•¹Ñ•¹•Í}…¹‘}Ñ¥µ•}É…¹•Ì¡Í•¹Ñ•¹•Ì¤(€€€ÁÉ•Ù¥½ÕÍ}•¹€ô€´Ä¸À(€€€™½È¥¹‘•à°€¡Ñ•áÐ°ÍÑ…ÉÐ°•¹¤¥¸•¹Õµ•É…Ñ”¡é¥À¡±•…¹}Ñ¥µ•±¥¹”°É…¹•}ÍÑ…ÉÑÌ°É…¹•}•¹‘Ì¤°ÍÑ…ÉÐôÄ¤è(€€€€€€€¥˜Ñ•áÐ¹ÍÑ…ÉÑÍÝ¥Ñ   ˆñY=%ôˆ°€ˆñM`ôˆ¤¤è(€€€€€€€€€€€½¹Ñ¥¹Õ”(€€€€€€€¥˜ÍÑ…ÉÐ¥Ì9½¹”½È•¹¥Ì9½¹”è(€€€€€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì(€€€€€€€€€€€€€€€€‰ÍÕ•ÍÌˆè…±Í”°(€€€€€€€€€€€€€€€€‰µ•ÍÍ…”ˆè€ (€€€€€€€€€€€€€€€€€€€˜‰É¹œí¥¹‘•áô£Á„ÌQ%5}9¸#äÓ†ê…¼³†ê…¤¯†î- ‹†ê¸•µ¥¹¤Ñ¡•¼“†ê…¹œ€ˆ(€€€€€€€€€€€€€€€€€€€€‰mQ%5õµ´éÍÌµµ´éÍÍtÑËÃ†îmŒ­¡¤á×†ê•Ð¸ˆ(€€€€€€€€€€€€€€€€¤(€€€€€€€€€€€ô¤°€ÐÀÀ(€€€€€€€¥˜•¹€ðôÍÑ…ÉÐè(€€€€€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì(€€€€€€€€€€€€€€€€‰ÍÕ•ÍÌˆè…±Í”°(€€€€€€€€€€€€€€€€‰µ•ÍÍ…”ˆè˜‰É¹œí¥¹‘•áôÌQ%5}9­£Ñ¹œ³†îm¸£…¸Q%5}MQIP¸ˆ(€€€€€€€€€€€ô¤°€ÐÀÀ(€€€€€€€¥˜ÍÑ…ÉÐ€ðÁÉ•Ù¥½ÕÍ}•¹è(€€€€€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì(€€€€€€€€€€€€€€€€‰ÍÕ•ÍÌˆè…±Í”°(€€€€€€€€€€€€€€€€‰µ•ÍÍ…”ˆè˜‰É¹œí¥¹‘•áô‹†î,ƒE¤³å¤¡¿†êÝŒ£†îM¹œ³©¸­¡¿†ê¹œÑ£†îu¤¥…¸ÑËÃ†îmŒ¸ˆ(€€€€€€€€€€€ô¤°€ÐÀÀ(€€€€€€€ÁÉ•Ù¥½ÕÍ}•¹€ô•¹(€€€€€€€€(€€€¥˜¹½ÐÁÉ½©•Ñ}¥è(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì(€€€€€€€€€€€€‰ÍÕ•ÍÌˆè…±Í”°(€€€€€€€€€€€€‰µ•ÍÍ…”ˆè€‰A¡¥•¸‘Ô…¸­¡½¹œ¡½À±”¸!…äÑ…¤±…¤ÑÉ…¹œÙ„Ñ…¼±…¤­¥ ‰…¸•µ¥¹¤¸ˆ(€€€€€€€ô¤°€ÐÀä(€€€Ý¥Ñ Ñ…Í­}±½¬è(€€€€€€€ÁÉ½©•Ð€ôAI=)QL¹•Ð¡ÁÉ½©•Ñ}¥¤(€€€¥˜¹½ÐÁÉ½©•Ð½ÈÁÉ½©•Ð¹•Ð ‰Ù¥‘•½}¹…µ”ˆ¤€„ôÙ¥‘•½}¹…µ”è(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì(€€€€€€€€€€€€‰ÍÕ•ÍÌˆè…±Í”°(€€€€€€€€€€€€‰µ•ÍÍ…”ˆè€‰Y¥‘•¼Ù„­¥ ‰…¸­¡½¹œÕ¹œµ½ÐÁ¡¥•¸¸!…äÑ…¼±…¤­¥ ‰…¸¡¼Ù¥‘•¼‘…¹œ¡½¸¸ˆ(€€€€€€€ô¤°€ÐÀä((€€€Ñ…Í­}¥€ôÉ•…Ñ•}Ñ…Í¬¡ÁÉ½©•Ñ}¥°Ù¥‘•½}¹…µ”¤((€€€•áÁ½ÉÑ}µ½‘”€ô‘…Ñ„¹•Ð ‰•áÁ½ÉÑ}µ½‘”ˆ°€‰™Õ±±}µÀÐˆ¤(€€€€(€€€€Œ£†ê…äÑ§†îÔÑË±¹ É•¹‘•È»†î¸(€€€Ñ¡É•…€ôÑ¡É•…‘¥¹œ¹Q¡É•… (€€€€€€€Ñ…É•ÐõÉ•¹‘•É}Ù¥‘•½}‰œ°(€€€€€€€…ÉÌô¡Ñ…Í­}¥°Ù¥‘•½}¹…µ”°‰µ}¹…µ”°‰µ}Ù½±Õµ”°Ù½¥•}¹…µ”°Ù½¥•}É…Ñ”°Í•¹Ñ•¹•Ì°½ÁÑ¥½¹Ì°•áÁ½ÉÑ}µ½‘”¤(€€€€¤(€€€Ñ¡É•…¹‘…•µ½¸€ôQÉÕ”(€€€Ñ¡É•…¹ÍÑ…ÉÐ ¤(€€€€(€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆèQÉÕ”°€‰Ñ…Í­}¥ˆèÑ…Í­}¥‘ô¤()…ÁÀ¹É½ÕÑ” ˆ½…Á¤½Ñ…Í¬µÍÑ…ÑÕÌ¼ñÑ…Í­}¥øˆ°µ•Ñ¡½‘Ìõl‰P‰t¤)‘•˜Ñ…Í­}ÍÑ…ÑÕÌ¡Ñ…Í­}¥¤è(€€€Ý¥Ñ Ñ…Í­}±½¬è(€€€€€€€Ñ…Í¬€ôQM-L¹•Ð¡Ñ…Í­}¥¤(€€€€€€€€(€€€¥˜¹½ÐÑ…Í¬è(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÑ…ÑÕÌˆè€‰¹½Ñ}™½Õ¹ˆ°€‰µ•ÍÍ…”ˆè€‰-£Ñ¹œÓ±´Ñ£†ê•äÓ…ŒÛ†î”¸‰ô¤(€€€€€€€€(€€€É•ÑÕÉ¸©Í½¹¥™ä¡Ñ…Í¬¤((((Œ€ôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôô(Œ	-I=U9QM,è$Y%<9IQ=H(Œ€ôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôôô)‘•˜•¹•É…Ñ•}…¥}Ù¥‘•½}‰œ¡Ñ…Í­}¥°…Á¥}­•ä°ÍÉ¥ÁÑ}Ñ•áÐ°ÍÑå±•}ÁÉ½µÁÐ°‰µ}¹…µ”°‰µ}Ù½±Õµ”°Ù½¥•}¹…µ”°Ù½¥•}É…Ñ”°½ÁÑ¥½¹Ì¤è(€€€‘•˜ÕÁ‘…Ñ•}ÁÉ½œ¡ÁÉ½œ°µÍœ¤è(€€€€€€€Ý¥Ñ Ñ…Í­}±½¬è(€€€€€€€€€€€QM-MmÑ…Í­}¥‘ul‰ÁÉ½É•ÍÌ‰t€ôÁÉ½œ(€€€€€€€€€€€QM-MmÑ…Í­}¥‘ul‰µ•ÍÍ…”‰t€ôµÍœ((€€€ÑÉäè(€€€€€€€™É½´ÑÑÍ}•¹¥¹”¥µÁ½ÉÐÍÁ±¥Ñ}Ñ•áÑ}¥¹Ñ½}Í•¹Ñ•¹•Ì°•¹•É…Ñ•}…±±}ÑÑÌ(€€€€€€€€(€€€€€€€ÕÁ‘…Ñ•}ÁÉ½œ Ô°€‹A…¹œÁ£‰¸Óµ ¯†î- ‹†ê¸¸¸¸ˆ¤(€€€€€€€Í•¹Ñ•¹•Ì€ôÍÁ±¥Ñ}Ñ•áÑ}¥¹Ñ½}Í•¹Ñ•¹•Ì¡ÍÉ¥ÁÑ}Ñ•áÐ¤(€€€€€€€¥˜¹½ÐÍ•¹Ñ•¹•Ìè(€€€€€€€€€€€É…¥Í”á•ÁÑ¥½¸ ‰/†î- ‹†ê¸ÑË†îE¹œ¡¿†êÝŒÅ×„¹Ÿ†ê½¸¸ˆ¤(€€€€€€€€€€€€(€€€€€€€Ñ•µÁ}‘¥È€ô½Ì¹Á…Ñ ¹©½¥¸¡]=I-MA}%H°˜‰Ñ•µÁ}íÑ…Í­}¥‘ôˆ¤(€€€€€€€½Ì¹µ…­•‘¥ÉÌ¡Ñ•µÁ}‘¥È°•á¥ÍÑ}½¬õQÉÕ”¤(€€€€€€€€(€€€€€€€€Œ€Ä¸•¹•É…Ñ”%µ…•Ì(€€€€€€€¥µ…•}Á…Ñ¡Ì€ô•¹•É…Ñ•}…±±}¥µ…•Ì¡…Á¥}­•ä°Í•¹Ñ•¹•Ì°ÍÑå±•}ÁÉ½µÁÐ°Ñ•µÁ}‘¥È°½ÁÑ¥½¹Ì¹•Ð ‰…ÍÁ•Ñ}É…Ñ¥¼ˆ°€ˆÄØèäˆ¤°ÕÁ‘…Ñ•}ÁÉ½œ¤(€€€€€€€€(€€€€€€€€Œ€È¸•¹•É…Ñ”Õ‘¥¼QQL(€€€€€€€ÕÁ‘…Ñ•}ÁÉ½œ ÐÀ°€‹A…¹œÓ†îU¹œ£†îÀ§†î5¹œ»Í¤$€¡QQL¤¸¸¸ˆ¤(€€€€€€€…Õ‘¥½}Á…Ñ¡Ì°‘ÕÉ…Ñ¥½¹Ì€ô•¹•É…Ñ•}…±±}ÑÑÌ¡Í•¹Ñ•¹•Ì°Ù½¥•}¹…µ”°Ù½¥•}É…Ñ”°Ñ•µÁ}‘¥È¤(€€€€€€€€(€€€€€€€€Œ€Ì¸I•Í½±Ù”	4A…Ñ (€€€€€€€‰µ}Á…Ñ €ô€ˆˆ(€€€€€€€¥˜‰µ}¹…µ”è(€€€€€€€€€€€‰µ}Á…Ñ €ô½Ì¹Á…Ñ ¹©½¥¸¡]=I-MA}%H°€‰ÍÑ…Ñ¥Œˆ°€‰‰´ˆ°‰µ}¹…µ”¤(€€€€€€€€€€€€(€€€€€€€€Œ€Ð¸I•¹‘•È¥¹…°Y¥‘•¼(€€€€€€€É•Í}ÕÉ°€ôÉ•¹‘•É}…¥}¥µ…•}Ù¥‘•½}™™µÁ•œ (€€€€€€€€€€€Ñ…Í­}¥°(€€€€€€€€€€€Í•¹Ñ•¹•Ì°(€€€€€€€€€€€‘ÕÉ…Ñ¥½¹Ì°(€€€€€€€€€€€…Õ‘¥½}Á…Ñ¡Ì°(€€€€€€€€€€€¥µ…•}Á…Ñ¡Ì°(€€€€€€€€€€€‰µ}Á…Ñ °(€€€€€€€€€€€‰µ}Ù½±Õµ”°(€€€€€€€€€€€½ÁÑ¥½¹Ì°(€€€€€€€€€€€ÕÁ‘…Ñ•}ÁÉ½œ(€€€€€€€€¤(€€€€€€€•áÁ½ÉÑ}Á…Ñ °•áÁ½ÉÑ}•ÉÉ½È€ô½Áå}É•ÍÕ±Ñ}Ñ½}•áÁ½ÉÑ}‘¥É•Ñ½Éä¡É•Í}ÕÉ°¤(€€€€€€€Ý¥Ñ Ñ…Í­}±½¬è(€€€€€€€€€€€QM-MmÑ…Í­}¥‘ul‰ÍÑ…ÑÕÌ‰t€ô€‰ÍÕ•ÍÌˆ(€€€€€€€€€€€QM-MmÑ…Í­}¥‘ul‰ÁÉ½É•ÍÌ‰t€ô€ÄÀÀ(€€€€€€€€€€€QM-MmÑ…Í­}¥‘ul‰µ•ÍÍ…”‰t€ô€‰S†ê…¼Y¥‘•¼!¿†ê…Ð#±¹ $Ñ£¹ Ñ¹œ„ˆ(€€€€€€€€€€€QM-MmÑ…Í­}¥‘ul‰É•ÍÕ±Ð‰t€ôÉ•Í}ÕÉ°(€€€€€€€€€€€QM-MmÑ…Í­}¥‘ul‰•áÁ½ÉÑ}Á…Ñ ‰t€ô•áÁ½ÉÑ}Á…Ñ (€€€€€€€€€€€QM-MmÑ…Í­}¥‘ul‰•áÁ½ÉÑ}•ÉÉ½È‰t€ô•áÁ½ÉÑ}•ÉÉ½È((€€€•á•ÁÐá•ÁÑ¥½¸…Ì”è(€€€€€€€¥µÁ½ÉÐÑÉ…•‰…¬(€€€€€€€ÑÉ…•‰…¬¹ÁÉ¥¹Ñ}•áŒ ¤(€€€€€€€Ý¥Ñ Ñ…Í­}±½¬è(€€€€€€€€€€€QM-MmÑ…Í­}¥‘ul‰ÍÑ…ÑÕÌ‰t€ô€‰•ÉÉ½Èˆ(€€€€€€€€€€€QM-MmÑ…Í­}¥‘ul‰µ•ÍÍ…”‰t€ô˜‰3†î]¤èíÍÑÈ¡”¥ôˆ(()…ÁÀ¹É½ÕÑ” ˆ½…¤µÙ¥‘•¼ˆ¤)‘•˜…¥}Ù¥‘•¼ ¤è(€€€É•ÑÕÉ¸É•¹‘•É}Ñ•µÁ±…Ñ” ‰…¥}Ù¥‘•¼¹¡Ñµ°ˆ°‘•™…Õ±Ñ}­•äõU1Q}A%}-d¤()…ÁÀ¹É½ÕÑ” ˆ½…Á¤½•¹•É…Ñ”µ…¤µÙ¥‘•¼ˆ°µ•Ñ¡½‘Ìõl‰A=MP‰t¤)‘•˜…Á¥}•¹•É…Ñ•}…¥}Ù¥‘•¼ ¤è(€€€‘…Ñ„€ôÉ•ÅÕ•ÍÐ¹©Í½¸½Èíô(€€€…Á¥}­•ä€ô‘…Ñ„¹•Ð ‰…Á¥}­•äˆ°€ˆˆ¤¹ÍÑÉ¥À ¤½ÈU1Q}A%}-d(€€€ÍÉ¥ÁÑ}Ñ•áÐ€ô‘…Ñ„¹•Ð ‰ÍÉ¥ÁÑ}Ñ•áÐˆ°€ˆˆ¤¹ÍÑÉ¥À ¤(€€€ÍÑå±•}ÁÉ½µÁÐ€ô‘…Ñ„¹•Ð ‰ÍÑå±•}ÁÉ½µÁÐˆ°€‰[†êô»¥ÐƒG…¸§†ê¸°·ÔÏ†ê½ŒÓÃ…¤Ï…¹œˆ¤¹ÍÑÉ¥À ¤(€€€‰µ}¹…µ”€ô‘…Ñ„¹•Ð ‰‰µ}¹…µ”ˆ°€ˆˆ¤(€€€‰µ}Ù½±Õµ”€ô™±½…Ð¡‘…Ñ„¹•Ð ‰‰µ}Ù½±Õµ”ˆ°€ÄÈ¤¤€¼€ÄÀÀ¸À(€€€Ù½¥•}¹…µ”€ô‘…Ñ„¹•Ð ‰Ù½¥•}¹…µ”ˆ°€‰Ù¤µY8µ!½…¥5å9•ÕÉ…°ˆ¤(€€€Ù½¥•}É…Ñ”€ô‘…Ñ„¹•Ð ‰Ù½¥•}É…Ñ”ˆ°€ˆ´ÄÀ”ˆ¤(€€€€(€€€½ÁÑ¥½¹Ì€ôì(€€€€€€€€‰…ÍÁ•Ñ}É…Ñ¥¼ˆè‘…Ñ„¹•Ð ‰…ÍÁ•Ñ}É…Ñ¥¼ˆ°€ˆÄØèäˆ¤°(€€€€€€€€‰™½¹Ñ}Í¥é”ˆè¥¹Ð¡‘…Ñ„¹•Ð ‰™½¹Ñ}Í¥é”ˆ°€ÐÈ¤¤°(€€€€€€€€‰™½¹Ñ}½±½Èˆè‘…Ñ„¹•Ð ‰™½¹Ñ}½±½Èˆ°€ˆˆ¤°(€€€€€€€€‰‰}½Á…¥Ñäˆè¥¹Ð¡‘…Ñ„¹•Ð ‰‰}½Á…¥Ñäˆ°€ÄÔÀ¤¤(€€€ô(€€€€(€€€¥˜¹½ÐÍÉ¥ÁÑ}Ñ•áÐè(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆè…±Í”°€‰µ•ÍÍ…”ˆè€‰£Á„¹£†êµÀ¯†î- ‹†ê¸¸‰ô¤(€€€¥˜¹½Ð…Á¥}­•äè(€€€€€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆè…±Í”°€‰µ•ÍÍ…”ˆè€‰g©Ô†êÔÁ£†ê¤ÌA$-•ä•µ¥¹¤¸‰ô¤(€€€€€€€€(€€€Ñ…Í­}¥€ôÉ•…Ñ•}Ñ…Í¬ ¤(€€€€(€€€Ñ¡É•…€ôÑ¡É•…‘¥¹œ¹Q¡É•… (€€€€€€€Ñ…É•Ðõ•¹•É…Ñ•}…¥}Ù¥‘•½}‰œ°(€€€€€€€…ÉÌô¡Ñ…Í­}¥°…Á¥}­•ä°ÍÉ¥ÁÑ}Ñ•áÐ°ÍÑå±•}ÁÉ½µÁÐ°‰µ}¹…µ”°‰µ}Ù½±Õµ”°Ù½¥•}¹…µ”°Ù½¥•}É…Ñ”°½ÁÑ¥½¹Ì¤(€€€€¤(€€€Ñ¡É•…¹‘…•µ½¸€ôQÉÕ”(€€€Ñ¡É•…¹ÍÑ…ÉÐ ¤(€€€€(€€€É•ÑÕÉ¸©Í½¹¥™ä¡ì‰ÍÕ•ÍÌˆèQÉÕ”°€‰Ñ…Í­}¥ˆèÑ…Í­}¥‘ô¤()¥˜}}¹…µ•}|€ôô€‰}}µ…¥¹}|ˆè(€€€ÁÉ¥¹Ð ‰l©tM•ÉÙ•È¥ÌÉÕ¹¹¥¹œ…Ð¡ÑÑÀè¼¼ÄÈÜ¸À¸À¸ÄèÔÀÀÀˆ¤(€€€…ÁÀ¹ÉÕ¸¡¡½ÍÐôˆÀ¸À¸À¸Àˆ°Á½ÉÐôÔÀÀÀ°‘•‰ÕœõQÉÕ”°ÕÍ•}É•±½…‘•Èõ…±Í”¤(