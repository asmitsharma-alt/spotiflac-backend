import os
import uuid
import glob
import shutil
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager
import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from SpotiFLAC import SpotiFLAC, SpotifyMetadataClient

# Load environment variables
load_dotenv()

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
    return {"status": "ok", "service": "spotiflac-backend"}


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


def _download_worker(url: str, output_dir: str, quality: str, embed_lyrics: bool):
    """Synchronous worker function executed in an asyncio thread pool."""
    qobuz_token = os.getenv("QOBUZ_TOKEN") or None
    tidal_api = os.getenv("TIDAL_CUSTOM_API") or None

    SpotiFLAC(
        url=url,
        output_dir=output_dir,
        quality=quality,
        embed_lyrics=embed_lyrics,
        qobuz_token=qobuz_token,
        tidal_custom_api=tidal_api,
        allow_fallback=True,
    )


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
        # Run the download in a non-blocking thread so other HTTP requests are served
        await asyncio.to_thread(
            _download_worker,
            url,
            str(job_output_dir),
            quality,
            embed_lyrics
        )

        # Locate downloaded audio file
        downloaded_files = glob.glob(str(job_output_dir / "**/*.*"), recursive=True)
        audio_files = [
            f for f in downloaded_files
            if f.lower().endswith(('.flac', '.mp3', '.m4a', '.wav', '.ogg'))
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

        media_type = "audio/flac" if filename.lower().endswith(".flac") else "audio/mpeg"

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
