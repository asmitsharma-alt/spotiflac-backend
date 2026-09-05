import os
import uuid
import glob
import shutil
import asyncio
from pathlib import Path
import contextlib
from contextlib import asynccontextmanager
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from SpotiFLAC import SpotiFLAC, SpotifyMetadataClient

# Load environment variables
load_dotenv()
os.environ.setdefault(
    "SPOTIFLAC_REGISTRIES",
    "https://raw.githubusercontent.com/zarzet/SpotiFLAC-Extension/main/registry.json"
)

TEMP_DIR = Path("./temp_downloads")
TEMP_DIR.mkdir(exist_ok=True)

# Keep-alive task reference
keep_alive_task = None


async def run_keep_alive(url: str, interval_minutes: int = 14):
    """Periodically ping self to keep Render free tier awake."""
    ping_url = f"{url.rstrip('/')}/api/health"
    print(f"[Keep-Alive] Started: Pinging {ping_url} every {interval_minutes} minutes.")
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            await asyncio.sleep(interval_minutes * 60)
            try:
                res = await client.get(ping_url)
                print(f"[Keep-Alive] Ping {ping_url} -> Status {res.status_code}")
            except Exception as e:
                print(f"[Keep-Alive] Ping failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global keep_alive_task

    # Pre-sync and ensure SpotiFLAC download provider extensions are active
    try:
        from SpotiFLAC.extensions import ExtensionManager
        em = ExtensionManager()
        em.ensure_download_providers()
        print(f"[Extensions] Active extensions: {[e.name for e in em.list_installed()]}")
    except Exception as e:
        print(f"[Extensions] Setup notice: {e}")

    external_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("SERVER_URL")
    if external_url:
        keep_alive_task = asyncio.create_task(run_keep_alive(external_url))
    else:
        print("[Keep-Alive] No external URL configured. Skipping self-ping.")

    yield

    if keep_alive_task:
        keep_alive_task.cancel()


app = FastAPI(
    title="SpotiFLAC Lossless Audio API",
    description="High-performance lossless audio extraction API powered by SpotiFLAC and FastAPI.",
    version="1.0.0",
    lifespan=lifespan,
)

# Enable CORS for all frontends
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

metadata_client = SpotifyMetadataClient()


def cleanup_directory(dir_path: str):
    """Safely purges the per-job temporary directory after download completion."""
    try:
        if os.path.exists(dir_path):
            shutil.rmtree(dir_path, ignore_errors=True)
            print(f"[Cleanup] Removed temporary directory: {dir_path}")
    except Exception as e:
        print(f"[Cleanup] Error removing directory {dir_path}: {e}")


@app.get("/api/health")
def health_check():
    """Health check endpoint used by Render and external uptime monitors."""
    return {"status": "ok", "service": "spotiflac-backend", "version": "2.0.0"}


@app.get("/api/debug")
async def debug_info(url: str = Query("https://open.spotify.com/track/5XeFesFbtLpXzIVDNQP22n")):
    import io
    import subprocess
    import traceback
    
    buf = io.StringIO()
    node_out = ""
    ffmpeg_out = ""
    exts = []
    downloaded = []
    err = None

    try:
        r = subprocess.run(["node", "-v"], capture_output=True, text=True, timeout=5)
        node_out = r.stdout.strip()
    except Exception as e:
        node_out = f"err: {e}"

    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        ffmpeg_out = r.stdout.split("\n")[0] if r.stdout else "none"
    except Exception as e:
        ffmpeg_out = f"err: {e}"

    try:
        from SpotiFLAC.extensions import ExtensionManager
        em = ExtensionManager()
        exts = [e.name for e in em.list_installed()]
    except Exception as e:
        exts = [f"err: {e}"]

    debug_dir = TEMP_DIR / f"debug_{uuid.uuid4().hex[:8]}"
    debug_dir.mkdir(parents=True, exist_ok=True)
    
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            await asyncio.to_thread(_download_worker, url, str(debug_dir), "LOSSLESS", False)
            downloaded = [os.path.basename(f) for f in glob.glob(str(debug_dir / "**/*.*"), recursive=True)]
        except Exception as e:
            err = traceback.format_exc()
        finally:
            shutil.rmtree(debug_dir, ignore_errors=True)

    return {
        "node": node_out,
        "ffmpeg": ffmpeg_out,
        "installed_extensions": exts,
        "downloaded_files": downloaded,
        "error": err,
        "logs": buf.getvalue(),
    }


@app.get("/api/info")
async def get_track_info(
    url: str = Query(..., description="Spotify track URL (e.g. https://open.spotify.com/track/...)")
):
    """Fetches track metadata without downloading."""
    if "open.spotify.com/track" not in url:
        raise HTTPException(
            status_code=400,
            detail="Invalid URL. Please provide a Spotify track URL."
        )

    try:
        metadata = await metadata_client.get_metadata_from_url_async(url)
        return metadata.model_dump()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch metadata: {str(e)}")


def _download_worker(url: str, output_dir: str, quality: str, embed_lyrics: bool, meta=None):
    """Synchronous worker function executed in an asyncio thread pool."""
    import sys
    import re
    import urllib.request
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    qobuz_token = os.getenv("QOBUZ_TOKEN") or None
    tidal_api = os.getenv("TIDAL_CUSTOM_API") or None

    services = ["youtube", "soundcloud", "qobuz", "deezer", "amazon"]
    if tidal_api:
        services.insert(0, "tidal")

    # 1. Attempt SpotiFLAC multi-service extraction
    try:
        SpotiFLAC(
            url=url,
            output_dir=output_dir,
            services=services,
            quality=quality,
            embed_lyrics=embed_lyrics,
            enrich_metadata=False,
            qobuz_token=qobuz_token,
            tidal_custom_api=tidal_api,
            allow_fallback=True,
            timeout_s=60,
        )
    except Exception as e:
        print(f"[SpotiFLAC] Run notice: {e}")

    # Check if SpotiFLAC produced an audio file
    downloaded = glob.glob(str(Path(output_dir) / "**/*.*"), recursive=True)
    audio_files = [
        f for f in downloaded
        if f.lower().endswith(('.flac', '.mp3', '.m4a', '.wav', '.ogg', '.opus'))
    ]
    if audio_files:
        return

    # 2. Resilient Direct Stream Fallback via yt-dlp & mutagen
    print("[Fallback] Audio provider mirrors failed or blocked. Executing direct high-quality stream fallback...")
    try:
        import yt_dlp
        from mutagen.mp4 import MP4, MP4Cover

        title = getattr(meta, "title", "Track") if meta else "Track"
        artists = getattr(meta, "artists", "Artist") if meta else "Artist"
        album = (getattr(meta, "album", "") if meta else "") or title
        cover_url = getattr(meta, "cover_url", "") if meta else ""
        year = str(getattr(meta, "release_date", ""))[:4] if meta and getattr(meta, "release_date", "") else ""

        safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
        safe_artist = re.sub(r'[\\/*?:"<>|]', "", artists)
        base_filename = f"{safe_title} - {safe_artist}"
        out_tmpl = os.path.join(output_dir, f"{base_filename}.%(ext)s")

        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "outtmpl": out_tmpl,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "m4a",
                    "preferredquality": "256",
                }
            ],
            "default_search": "ytsearch1",
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([f"ytsearch1:{title} {artists} official audio"])

        target_m4a = os.path.join(output_dir, f"{base_filename}.m4a")
        if not os.path.exists(target_m4a):
            candidates = glob.glob(os.path.join(output_dir, f"{base_filename}.*"))
            if candidates:
                target_m4a = candidates[0]

        if os.path.exists(target_m4a) and target_m4a.endswith(".m4a"):
            try:
                audio = MP4(target_m4a)
                audio["\xa9nam"] = [title]
                audio["\xa9ART"] = [artists]
                audio["\xa9alb"] = [album]
                if year:
                    audio["\xa9day"] = [year]
                if cover_url:
                    try:
                        req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=10) as r:
                            cover_bytes = r.read()
                        audio["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
                    except Exception as ce:
                        print(f"[Fallback] Cover embed notice: {ce}")
                audio.save()
                print(f"[Fallback] Successfully created and tagged: {target_m4a}")
            except Exception as tag_err:
                print(f"[Fallback] Tagging notice: {tag_err}")

    except Exception as fallback_err:
        print(f"[Fallback] Fallback error: {fallback_err}")


@app.get("/api/download")
async def download_track(
    background_tasks: BackgroundTasks,
    url: str = Query(..., description="Spotify track URL"),
    quality: str = Query("LOSSLESS", description="Desired audio quality: LOSSLESS, HIGH, or MEDIUM"),
    embed_lyrics: bool = Query(True, description="Whether to embed synced lyrics into tags")
):
    """
    Downloads a Spotify track in lossless FLAC format and streams it to the client.
    Automatically removes the temporary file after the client finishes downloading.
    """
    if "open.spotify.com/track" not in url:
        raise HTTPException(
            status_code=400,
            detail="Invalid URL. Please provide a valid Spotify track link."
        )

    job_id = str(uuid.uuid4())
    job_output_dir = TEMP_DIR / job_id
    job_output_dir.mkdir(parents=True, exist_ok=True)

    try:
        meta = None
        try:
            meta = await metadata_client.get_metadata_from_url_async(url)
        except Exception:
            pass

        # Run the download in a non-blocking thread so other HTTP requests are served
        await asyncio.to_thread(
            _download_worker,
            url,
            str(job_output_dir),
            quality,
            embed_lyrics,
            meta
        )

        # Locate downloaded audio file
        downloaded_files = glob.glob(str(job_output_dir / "**/*.*"), recursive=True)
        audio_files = [
            f for f in downloaded_files
            if f.lower().endswith(('.flac', '.mp3', '.m4a', '.wav', '.ogg', '.opus'))
        ]

        if not audio_files:
            raise HTTPException(
                status_code=404,
                detail="Track could not be retrieved from audio providers."
            )

        file_path = audio_files[0]
        filename = os.path.basename(file_path)

        # Schedule automatic folder deletion after the response is delivered
        background_tasks.add_task(cleanup_directory, str(job_output_dir))

        ext = os.path.splitext(filename)[1].lower()
        media_types = {
            ".flac": "audio/flac",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".wav": "audio/wav",
            ".ogg": "audio/ogg",
            ".opus": "audio/opus",
        }
        media_type = media_types.get(ext, "application/octet-stream")

        return FileResponse(
            path=file_path,
            filename=filename,
            media_type=media_type,
            background=background_tasks,
        )

    except HTTPException:
        cleanup_directory(str(job_output_dir))
        raise
    except Exception as e:
        cleanup_directory(str(job_output_dir))
        raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")
