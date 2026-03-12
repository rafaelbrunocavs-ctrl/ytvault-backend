from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp
import uuid
import os
import time
from pathlib import Path
from contextlib import contextmanager

# ─────────────────────────────────────────────
# BANCO: SQLite (local) ou PostgreSQL (Railway)
# ─────────────────────────────────────────────
# Railway injeta DATABASE_URL automaticamente quando você adiciona
# um PostgreSQL ao projeto. Sem ela, usa SQLite como fallback.

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    import psycopg2
    PLACEHOLDER = "%s"
    def get_connection():
        return psycopg2.connect(DATABASE_URL)
else:
    import sqlite3
    DB_FILE = Path("ytvault.db")
    PLACEHOLDER = "?"
    def get_connection():
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        return conn

@contextmanager
def get_db():
    """Abre e fecha a conexão automaticamente, funciona igual pros dois bancos."""
    conn = get_connection()
    if DATABASE_URL:
        # psycopg2 precisa de cursor explícito
        cur = conn.cursor()
        try:
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()
    else:
        # sqlite3 executa direto na conexão
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

def fetch_all(db, query, params=()):
    db.execute(query, params)
    cols = [desc[0] for desc in db.description]
    return [dict(zip(cols, row)) for row in db.fetchall()]

def fetch_one(db, query, params=()):
    db.execute(query, params)
    cols = [desc[0] for desc in db.description]
    row = db.fetchone()
    return dict(zip(cols, row)) if row else None

# ─────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────

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

class DownloadRequest(BaseModel):
    url: str
    quality: str = "1080p"
    format: str = "mp4"

# ─────────────────────────────────────────────
# INICIALIZAÇÃO DO BANCO
# ─────────────────────────────────────────────

def init_db():
    """Cria a tabela se não existir. Roda uma vez ao iniciar."""
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS videos (
                id        TEXT PRIMARY KEY,
                title     TEXT,
                channel   TEXT,
                duration  TEXT,
                quality   TEXT,
                format    TEXT,
                size      REAL,
                thumb     TEXT,
                url       TEXT,
                filename  TEXT,
                category  TEXT DEFAULT 'Sem categoria',
                favorite  INTEGER DEFAULT 0,
                date      BIGINT
            )
        """)

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def get_ydl_opts(extra: dict = {}) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        },
        "extractor_args": {"youtube": {"player_client": ["web", "android"]}},
        "socket_timeout": 60,
        "retries": 10,
        "fragment_retries": 10,
    }
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    opts.update(extra)
    return opts

def quality_to_format(quality: str) -> str:
    if quality == "audio":
        return "bestaudio/best"
    res = quality.replace("p", "")
    return f"bestvideo[height<={res}]+bestaudio/best[height<={res}]/best"

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

def format_duration(seconds: int) -> str:
    if not seconds:
        return "0:00"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

# ─────────────────────────────────────────────
# DOWNLOAD (roda em background)
# ─────────────────────────────────────────────

def do_download(job_id: str, url: str, quality: str, fmt: str):
    try:
        output_template = str(DOWNLOAD_DIR / f"{job_id}_%(title)s.%(ext)s")
        extra = {
            "format": quality_to_format(quality),
            "outtmpl": output_template,
            "progress_hooks": [progress_hook(job_id)],
            "merge_output_format": "mp4",
        }
        if quality == "audio":
            extra["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]

        with yt_dlp.YoutubeDL(get_ydl_opts(extra)) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if not os.path.exists(filename):
                for f in DOWNLOAD_DIR.iterdir():
                    if f.name.startswith(job_id) and f.suffix in [".mp4", ".mkv", ".webm", ".mp3"]:
                        filename = str(f)
                        break

            video = {
                "id": job_id,
                "title": info.get("title", "Sem título"),
                "channel": info.get("uploader", "Desconhecido"),
                "duration": format_duration(info.get("duration", 0)),
                "quality": quality,
                "format": "mp3" if quality == "audio" else "mp4",
                "size": round(os.path.getsize(filename) / (1024**3), 2) if os.path.exists(filename) else 0,
                "thumb": info.get("thumbnail", ""),
                "url": url,
                "filename": os.path.basename(filename),
                "category": "Sem categoria",
                "favorite": 0,
                "date": int(time.time() * 1000),
            }

            p = PLACEHOLDER
            with get_db() as db:
                db.execute(
                    f"INSERT INTO videos (id,title,channel,duration,quality,format,size,thumb,url,filename,category,favorite,date) "
                    f"VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})",
                    list(video.values())
                )

            jobs[job_id]["status"] = "done"
            jobs[job_id]["progress"] = 100
            jobs[job_id]["info"] = {**video, "favorite": False, "tags": []}

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)

# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

@app.on_event("startup")
def startup():
    init_db()

@app.get("/")
def root():
    return {
        "status": "YTVault API online ✅",
        "db": "postgresql" if DATABASE_URL else "sqlite",
        "cookies": COOKIES_FILE.exists()
    }

@app.get("/storage")
def get_storage():
    total_bytes = sum(f.stat().st_size for f in DOWNLOAD_DIR.iterdir() if f.is_file()) if DOWNLOAD_DIR.exists() else 0
    return {"used_bytes": total_bytes, "used_gb": round(total_bytes / (1024**3), 2)}

@app.post("/cookies")
async def upload_cookies(body: dict):
    content = body.get("content", "")
    if not content:
        raise HTTPException(400, "Conteúdo vazio")
    COOKIES_FILE.write_text(content)
    return {"saved": True}

@app.get("/cookies/status")
def cookies_status():
    return {"exists": COOKIES_FILE.exists()}

@app.post("/metadata")
def get_metadata(body: dict):
    url = body.get("url")
    if not url:
        raise HTTPException(400, "URL obrigatória")
    try:
        with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title"),
                "channel": info.get("uploader"),
                "duration": format_duration(info.get("duration", 0)),
                "thumbnail": info.get("thumbnail"),
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
    with get_db() as db:
        videos = fetch_all(db, "SELECT * FROM videos ORDER BY date DESC")
    for v in videos:
        v["favorite"] = bool(v.get("favorite", False))
        v["tags"] = []
    return videos

@app.delete("/library/{video_id}")
def delete_video(video_id: str):
    p = PLACEHOLDER
    with get_db() as db:
        video = fetch_one(db, f"SELECT * FROM videos WHERE id = {p}", (video_id,))
        if not video:
            raise HTTPException(404, "Vídeo não encontrado")
        filepath = DOWNLOAD_DIR / video["filename"]
        if filepath.exists():
            filepath.unlink()
        db.execute(f"DELETE FROM videos WHERE id = {p}", (video_id,))
    return {"deleted": video_id}

@app.patch("/library/{video_id}/favorite")
def toggle_favorite(video_id: str):
    p = PLACEHOLDER
    with get_db() as db:
        video = fetch_one(db, f"SELECT favorite FROM videos WHERE id = {p}", (video_id,))
        if not video:
            raise HTTPException(404, "Vídeo não encontrado")
        new_val = 0 if video["favorite"] else 1
        db.execute(f"UPDATE videos SET favorite = {p} WHERE id = {p}", (new_val, video_id))
    return {"favorite": bool(new_val)}

@app.get("/download-file/{video_id}")
def download_file(video_id: str):
    p = PLACEHOLDER
    with get_db() as db:
        video = fetch_one(db, f"SELECT * FROM videos WHERE id = {p}", (video_id,))
    if not video:
        raise HTTPException(404, "Vídeo não encontrado")
    filepath = DOWNLOAD_DIR / video["filename"]
    if not filepath.exists():
        raise HTTPException(404, "Arquivo não encontrado no disco")
    return FileResponse(filepath, filename=video["filename"])

