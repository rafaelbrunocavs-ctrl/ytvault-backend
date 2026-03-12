from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import yt_dlp
import uuid
import os
import time
import sqlite3                  # <- banco de dados, já vem no Python
from pathlib import Path
from contextlib import contextmanager

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
DB_FILE = Path("ytvault.db")    # <- arquivo do banco

jobs: dict = {}

# ─────────────────────────────────────────────
# BANCO DE DADOS
# ─────────────────────────────────────────────

def init_db():
    """Cria as tabelas se ainda não existirem.
    Roda uma vez quando o servidor inicia."""
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
                date      INTEGER
            )
        """)
        # Cada linha aqui é uma coluna da tabela
        # INTEGER = número inteiro, TEXT = texto, REAL = decimal

@contextmanager
def get_db():
    """Abre e fecha a conexão com o banco automaticamente.
    O 'with get_db() as db' garante que sempre fecha, mesmo com erro."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row   # faz as linhas se comportarem como dicionários
    try:
        yield conn
        conn.commit()                # salva as mudanças
    except Exception:
        conn.rollback()              # desfaz se der erro
        raise
    finally:
        conn.close()

def row_to_dict(row) -> dict:
    """Converte uma linha do banco para dicionário Python."""
    d = dict(row)
    d["favorite"] = bool(d["favorite"])   # converte 0/1 para True/False
    d["tags"] = []
    return d

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
        "extractor_args": {
            "youtube": {
                "player_client": ["web", "android"],
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
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

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
                "favorite": False,
                "date": int(time.time() * 1000),
            }

            # ── SALVA NO BANCO ──────────────────────────────
            # Antes era só: library.append(video)
            # Agora persiste para sempre no arquivo ytvault.db
            with get_db() as db:
                db.execute("""
                    INSERT OR REPLACE INTO videos
                    (id, title, channel, duration, quality, format, size, thumb, url, filename, category, favorite, date)
                    VALUES (:id, :title, :channel, :duration, :quality, :format, :size, :thumb, :url, :filename, :category, :favorite, :date)
                """, {**video, "favorite": 1 if video["favorite"] else 0})
            # ────────────────────────────────────────────────

            jobs[job_id]["status"] = "done"
            jobs[job_id]["progress"] = 100
            jobs[job_id]["info"] = {**video, "tags": []}

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)

# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

@app.on_event("startup")
def startup():
    init_db()   # cria tabelas ao iniciar o servidor

@app.get("/")
def root():
    return {"status": "YTVault API online ✅", "cookies": COOKIES_FILE.exists()}

@app.get("/storage")
def get_storage():
    total_bytes = sum(f.stat().st_size for f in DOWNLOAD_DIR.iterdir() if f.is_file()) if DOWNLOAD_DIR.exists() else 0
    return {
        "used_bytes": total_bytes,
        "used_gb": round(total_bytes / (1024**3), 2),
        "file_count": len(list(DOWNLOAD_DIR.iterdir())) if DOWNLOAD_DIR.exists() else 0
    }

@app.post("/cookies")
async def upload_cookies(body: dict):
    content = body.get("content", "")
    if not content:
        raise HTTPException(400, "Conteúdo vazio")
    COOKIES_FILE.write_text(content)
    return {"saved": True, "lines": len(content.splitlines())}

@app.get("/cookies/status")
def cookies_status():
    return {"exists": COOKIES_FILE.exists(), "lines": len(COOKIES_FILE.read_text().splitlines()) if COOKIES_FILE.exists() else 0}

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
    # ── LÊ DO BANCO ────────────────────────────────────
    # Antes era só: return library
    # Agora busca todos os vídeos salvos no banco
    with get_db() as db:
        rows = db.execute("SELECT * FROM videos ORDER BY date DESC").fetchall()
    return [row_to_dict(r) for r in rows]
    # ────────────────────────────────────────────────────

@app.delete("/library/{video_id}")
def delete_video(video_id: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Vídeo não encontrado")
        video = dict(row)
        filepath = DOWNLOAD_DIR / video["filename"]
        if filepath.exists():
            filepath.unlink()                                      # apaga o arquivo
        db.execute("DELETE FROM videos WHERE id = ?", (video_id,))  # apaga do banco
    return {"deleted": video_id}

@app.patch("/library/{video_id}/favorite")
def toggle_favorite(video_id: str):
    """Alterna favorito — exemplo de UPDATE no banco"""
    with get_db() as db:
        row = db.execute("SELECT favorite FROM videos WHERE id = ?", (video_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Vídeo não encontrado")
        new_val = 0 if row["favorite"] else 1
        db.execute("UPDATE videos SET favorite = ? WHERE id = ?", (new_val, video_id))
    return {"favorite": bool(new_val)}

@app.get("/download-file/{video_id}")
def download_file(video_id: str):
    with get_db() as db:
        row = db.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Vídeo não encontrado")
    video = dict(row)
    filepath = DOWNLOAD_DIR / video["filename"]
    if not filepath.exists():
        raise HTTPException(404, "Arquivo não encontrado no disco")
    return FileResponse(filepath, filename=video["filename"])

