from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp
import uuid
import os
import json
import asyncio
from pathlib import Path

app = FastAPI(title="YTVault API")

# CORS — permite o app no GitHub Pages chamar esta API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Estado em memória (jobs e biblioteca)
jobs: dict = {}       # job_id → { status, progress, error, info }
library: list = []    # lista de vídeos baixados


# ── MODELOS ──────────────────────────────────────
class DownloadRequest(BaseModel):
    url: str
    quality: str = "1080p"   # 2160p, 1080p, 720p, 480p, audio
    format: str = "mp4"      # mp4, webm, mkv, mp3


# ── HELPERS ──────────────────────────────────────
def quality_to_format(quality: str, fmt: str) -> str:
    if quality == "audio":
        return "bestaudio/best"
    res = quality.replace("p", "")
    return f"bestvideo[height<={res}][ext={fmt}]+bestaudio/bestvideo[height<={res}]+bestaudio/best"


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

        ydl_opts = {
            "format": quality_to_format(quality, fmt),
            "outtmpl": output_template,
            "progress_hooks": [progress_hook(job_id)],
            "merge_output_format": fmt if fmt != "mp3" else None,
            "quiet": True,
            "no_warnings": True,
            # Anti-403: simula navegador real
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
            },
            "extractor_args": {"youtube": {"skip": ["dash", "hls"]}},
            "socket_timeout": 30,
            "retries": 5,
            "fragment_retries": 5,
            "concurrent_fragment_downloads": 4,
        }

        # Se for áudio, extrai MP3
        if quality == "audio" or fmt == "mp3":
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

            # Ajusta extensão se foi convertido
            if quality == "audio" or fmt == "mp3":
                filename = Path(filename).with_suffix(".mp3").__str__()

            # Salva na biblioteca
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
                "date": int(__import__("time").time() * 1000),
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


# ── ROTAS ────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "YTVault API online ✅"}


@app.post("/metadata")
def get_metadata(body: dict):
    """Busca título, thumb, duração sem baixar"""
    url = body.get("url")
    if not url:
        raise HTTPException(400, "URL obrigatória")
    try:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            },
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title"),
                "channel": info.get("uploader"),
                "duration": format_duration(info.get("duration", 0)),
                "thumbnail": info.get("thumbnail"),
                "view_count": info.get("view_count"),
                "upload_date": info.get("upload_date"),
            }
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/download")
def start_download(req: DownloadRequest, bg: BackgroundTasks):
    """Inicia download em background e retorna job_id"""
    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": "queue",
        "progress": 0,
        "speed": "",
        "eta": "",
        "error": None,
        "info": None,
    }
    bg.add_task(do_download, job_id, req.url, req.quality, req.format)
    return {"job_id": job_id}


@app.get("/progress/{job_id}")
def get_progress(job_id: str):
    """Retorna progresso de um download"""
    if job_id not in jobs:
        raise HTTPException(404, "Job não encontrado")
    return jobs[job_id]


@app.get("/library")
def get_library():
    """Lista todos os vídeos baixados"""
    return library


@app.delete("/library/{video_id}")
def delete_video(video_id: str):
    """Remove vídeo da biblioteca e do disco"""
    global library
    video = next((v for v in library if v["id"] == video_id), None)
    if not video:
        raise HTTPException(404, "Vídeo não encontrado")

    # Remove arquivo do disco
    filepath = DOWNLOAD_DIR / video["filename"]
    if filepath.exists():
        filepath.unlink()

    library = [v for v in library if v["id"] != video_id]
    return {"deleted": video_id}


@app.get("/download-file/{video_id}")
def download_file(video_id: str):
    """Faz download do arquivo para o cliente"""
    video = next((v for v in library if v["id"] == video_id), None)
    if not video:
        raise HTTPException(404, "Vídeo não encontrado")
    filepath = DOWNLOAD_DIR / video["filename"]
    if not filepath.exists():
        raise HTTPException(404, "Arquivo não encontrado no disco")
    return FileResponse(filepath, filename=video["filename"])
