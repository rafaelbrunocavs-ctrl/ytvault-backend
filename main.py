from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp
import uuid
import os
import time
from pathlib import Path

app = FastAPI(title="YTVault API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
COOKIES_FILE = Path("cookies.txt")

jobs: dict = {}
library: list = []

class DownloadRequest(BaseModel):
    url: str
    quality: str = "1080p"
    format: str = "mp4"

def get_ydl_opts(extra: dict = {}) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["web"],
                "skip": ["translated_subs"],
            }
        },
        "socket_timeout": 60,
        "retries": 10,
        "fragment_retries": 10,
    }
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    opts.update(extra)
    return opts

def quality_to_format(quality: str, fmt: str) -> str:
    if quality == "audio":
        return "bestaudio/best"
    res = quality.replace("p", "")
    return f"bestvideo[height<={res}]+bestaudio/best[height<={res}]/best"

def get_available_qualities(info: dict) -> list:
    """Extrai qualidades únicas disponíveis do vídeo"""
    formats = info.get("formats", [])
    heights = set()
    for f in formats:
        h = f.get("height")
        if h and f.get("vcodec") != "none":
            heights.add(h)
    # Ordena do maior pro menor e mapeia para labels
    labels_map = {2160: "4K · 2160p", 1440: "2K · 1440p", 1080: "Full HD · 1080p",
                  720: "HD · 720p", 480: "SD · 480p", 360: "360p", 240: "240p", 144: "144p"}
    result = []
    for h in sorted(heights, reverse=True):
        label = labels_map.get(h, f"{h}p")
        result.append({"height": h, "label": label, "value": f"{h}p"})
    return result

def progress_hook(job_id: str):
    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                jobs[job_id]["progress"] = round(downloaded / total * 100, 1)
            jobs[job_id]["status"] = "downloading"
            jobs[job_id]["speed"] = d.get("_speed_str", "")
            jobs[job_id]["eta"] = d.get("_eta_str", "")
        elif d["status"] == "finished":
            jobs[job_id]["progress"] = 100
            jobs[job_id]["status"] = "processing"
    return hook

def do_download(job_id: str, url: str, quality: str, fmt: str):
    try:
        output_template = str(DOWNLOAD_DIR / f"{job_id}_%(title)s.%(ext)s")
        extra = {
            "format": quality_to_format(quality, fmt),
            "outtmpl": output_template,
            "progress_hooks": [progress_hook(job_id)],
            "merge_output_format": fmt if quality != "audio" else None,
        }
        if quality == "audio" or fmt == "mp3":
            extra["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]

        with yt_dlp.YoutubeDL(get_ydl_opts(extra)) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if quality == "audio" or fmt == "mp3":
                filename = Path(filename).with_suffix(".mp3").__str__()

            video_entry = {
                "id": job_id,
                "title": info.get("title", "Sem título"),
                "channel": info.get("uploader", "Desconhecido"),
                "duration": format_duration(info.get("duration", 0)),
                "quality": quality,
                "format": "mp3" if quality == "audio" else fmt,
                "size": round(os.path.getsize(filename) / (1024**3), 2) if os.path.exists(filename) else 0,
                "thumb": info.get("thumbnail", ""),
                "url": url,
                "filename": os.path.basename(filename),
                "tags": info.get("tags", [])[:3] if info.get("tags") else [],
                "category": "Sem categoria",
                "favorite": False,
                "date": int(time.time() * 1000),
            }
            library.append(video_entry)
            jobs[job_id]["status"] = "done"
            jobs[job_id]["progress"] = 100
            jobs[job_id]["info"] = video_entry

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)

def format_duration(seconds: int) -> str:
    if not seconds:
        return "0:00"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

@app.get("/")
def root():
    return {"status": "YTVault API online ✅", "cookies": COOKIES_FILE.exists()}

@app.get("/storage")
def get_storage():
    """Retorna espaço usado pelos downloads"""
    total_bytes = sum(
        f.stat().st_size for f in DOWNLOAD_DIR.iterdir() if f.is_file()
    ) if DOWNLOAD_DIR.exists() else 0
    return {
        "used_bytes": total_bytes,
        "used_gb": round(total_bytes / (1024**3), 2),
        "file_count": len(list(DOWNLOAD_DIR.iterdir())) if DOWNLOAD_DIR.exists() else 0
    }

@app.post("/cookies")
async def upload_cookies(body: dict):
    """Recebe conteúdo do arquivo cookies.txt e salva no servidor"""
    content = body.get("content", "")
    if not content:
        raise HTTPException(400, "Conteúdo vazio")
    COOKIES_FILE.write_text(content)
    return {"saved": True, "lines": len(content.splitlines())}

@app.get("/cookies/status")
def cookies_status():
    return {"exists": COOKIES_FILE.exists(), "lines": len(COOKIES_FILE.read_text().splitlines()) if COOKIES_FILE.exists() else 0}

@app.post("/debug-formats")
def debug_formats(body: dict):
    """Debug: lista todos os formatos disponíveis para um vídeo"""
    url = body.get("url")
    if not url:
        raise HTTPException(400, "URL obrigatória")
    try:
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get("formats", [])
            return {
                "total_formats": len(formats),
                "formats": [
                    {
                        "format_id": f.get("format_id"),
                        "ext": f.get("ext"),
                        "height": f.get("height"),
                        "width": f.get("width"),
                        "vcodec": f.get("vcodec"),
                        "acodec": f.get("acodec"),
                        "filesize": f.get("filesize"),
                        "format_note": f.get("format_note"),
                    }
                    for f in formats
                ]
            }
    except Exception as e:
        raise HTTPException(400, str(e))


def get_metadata(body: dict):
    url = body.get("url")
    if not url:
        raise HTTPException(400, "URL obrigatória")
    try:
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
            qualities = get_available_qualities(info)
            return {
                "title": info.get("title"),
                "channel": info.get("uploader"),
                "duration": format_duration(info.get("duration", 0)),
                "thumbnail": info.get("thumbnail"),
                "qualities": qualities,
            }
    except Exception as e:
        raise HTTPException(400, str(e))

@app.post("/download")
def start_download(req: DownloadRequest, bg: BackgroundTasks):
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {"status": "queue", "progress": 0, "speed": "", "eta": "", "error": None, "info": None}
    bg.add_task(do_download, job_id, req.url, req.quality, req.format)
    return {"job_id": job_id}

@app.get("/progress/{job_id}")
def get_progress(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job não encontrado")
    return jobs[job_id]

@app.get("/library")
def get_library():
    return library

@app.delete("/library/{video_id}")
def delete_video(video_id: str):
    global library
    video = next((v for v in library if v["id"] == video_id), None)
    if not video:
        raise HTTPException(404, "Vídeo não encontrado")
    filepath = DOWNLOAD_DIR / video["filename"]
    if filepath.exists():
        filepath.unlink()
    library = [v for v in library if v["id"] != video_id]
    return {"deleted": video_id}

@app.get("/download-file/{video_id}")
def download_file(video_id: str):
    video = next((v for v in library if v["id"] == video_id), None)
    if not video:
        raise HTTPException(404, "Vídeo não encontrado")
    filepath = DOWNLOAD_DIR / video["filename"]
    if not filepath.exists():
        raise HTTPException(404, "Arquivo não encontrado no disco")
    return FileResponse(filepath, filename=video["filename"])

