import os
import uuid
import glob
import shutil
import asyncio
from pathlib import Path
import contextlib
from contextlib import asynccontextmanager
import json
import httpx
from dotenv import load_dotenv
from pydantic import BaseModel
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
        if not em.list_installed():
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
        from mutagen.flac import FLAC, Picture
        from mutagen.id3 import ID3, APIC, TIT2, TPE1, TALB, TDRC
        from mutagen.mp4 import MP4, MP4Cover

        title = getattr(meta, "title", "Track") if meta else "Track"
        artists = getattr(meta, "artists", "Artist") if meta else "Artist"
        album = (getattr(meta, "album", "") if meta else "") or title
        cover_url = getattr(meta, "cover_url", "") if meta else ""
        year = str(getattr(meta, "release_date", ""))[:4] if meta and getattr(meta, "release_date", "") else ""
        expected_duration = (getattr(meta, "duration_ms", 0) or 0) / 1000.0

        safe_title = re.sub(r'[\\/*?:"<>|]', "", title)
        safe_artist = re.sub(r'[\\/*?:"<>|]', "", artists)
        base_filename = f"{safe_title} - {safe_artist}"

        quality_str = str(quality).upper()
        if "LOSSLESS" in quality_str or "FLAC" in quality_str:
            target_ext = "flac"
            postprocessor = {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "flac",
            }
        else:
            target_ext = "mp3"
            postprocessor = {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320" if "HIGH" in quality_str else "192",
            }

        out_tmpl = os.path.join(output_dir, f"{base_filename}.%(ext)s")
        target_file = None
        download_success = False

        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "outtmpl": out_tmpl,
            "postprocessors": [postprocessor],
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "tv_embedded"],
                }
            },
        }

        # Query candidates in order of quality & reliability
        candidate_queries = [
            ("youtube", f"ytsearch1:{title} {artists} official audio"),
            ("youtube", f"ytsearch1:{title} {artists} topic"),
            ("youtube", f"ytsearch1:{title} {artists}"),
        ]

        for source_name, query_str in candidate_queries:
            try:
                print(f"[Fallback] Querying {source_name} audio stream: {query_str[:40]}...")
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([query_str])

                found = [
                    f for f in glob.glob(os.path.join(output_dir, "**/*.*"), recursive=True)
                    if f.lower().endswith(('.flac', '.mp3', '.m4a', '.wav', '.ogg', '.opus'))
                ]
                if found:
                    cand = found[0]
                    cand_duration = 0
                    try:
                        import mutagen
                        mf = mutagen.File(cand)
                        if mf and hasattr(mf, "info") and hasattr(mf.info, "length"):
                            cand_duration = mf.info.length
                    except Exception:
                        pass

                    cand_size = os.path.getsize(cand)
                    print(f"[Fallback] Candidate downloaded: {cand} (duration: {cand_duration:.1f}s, size: {cand_size} bytes)")

                    # Require at least 65% of expected duration or 45s to reject 30s snippets
                    min_allowed = min(expected_duration * 0.65, 45) if expected_duration > 45 else 20
                    if cand_duration >= min_allowed:
                        target_file = cand
                        download_success = True
                        break
                    else:
                        print(f"[Fallback] Candidate rejected as preview snippet (duration {cand_duration:.1f}s < {min_allowed:.1f}s). Purging...")
                        try:
                            os.remove(cand)
                        except Exception:
                            pass
            except Exception as fe:
                print(f"[Fallback] {source_name} query error: {fe}")

        # If YouTube didn't yield a full track, try SoundCloud with duration search
        if not download_success:
            try:
                print(f"[Fallback] Attempting SoundCloud full-track resolution for: {title} {artists}...")
                sc_opts = {"quiet": True, "no_warnings": True, "extract_flat": False}
                with yt_dlp.YoutubeDL(sc_opts) as ydl_sc:
                    sc_res = ydl_sc.extract_info(f"scsearch10:{title} {artists} lyrics", download=False)
                    best_url = None
                    min_diff = 99999
                    for entry in (sc_res.get("entries") or []):
                        d = entry.get("duration") or 0
                        # Reject snippets under 60 seconds when expected duration is long
                        if expected_duration > 60 and d < 60:
                            continue
                        diff = abs(d - expected_duration) if expected_duration else 0
                        if diff < min_diff:
                            min_diff = diff
                            best_url = entry.get("webpage_url")

                    if best_url:
                        print(f"[Fallback] Downloading best SoundCloud track: {best_url} (diff: {min_diff:.1f}s)...")
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl_dl:
                            ydl_dl.download([best_url])

                        found = [
                            f for f in glob.glob(os.path.join(output_dir, "**/*.*"), recursive=True)
                            if f.lower().endswith(('.flac', '.mp3', '.m4a', '.wav', '.ogg', '.opus'))
                        ]
                        if found:
                            target_file = found[0]
                            download_success = True
            except Exception as sce:
                print(f"[Fallback] SoundCloud resolution error: {sce}")

        # Embed official metadata and album artwork
        if download_success and target_file and os.path.exists(target_file):
            cover_bytes = None
            if cover_url:
                try:
                    req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        cover_bytes = r.read()
                except Exception as ce:
                    print(f"[Fallback] Cover art download notice: {ce}")

            ext = os.path.splitext(target_file)[1].lower()
            if ext == ".flac":
                try:
                    audio = FLAC(target_file)
                    audio["title"] = [title]
                    audio["artist"] = [artists]
                    audio["album"] = [album]
                    if year:
                        audio["date"] = [year]
                    if cover_bytes:
                        pic = Picture()
                        pic.data = cover_bytes
                        pic.type = 3
                        pic.mime = "image/jpeg"
                        audio.add_picture(pic)
                    audio.save()
                    print(f"[Fallback] Tagged FLAC: {target_file}")
                except Exception as te:
                    print(f"[Fallback] FLAC tag notice: {te}")
            elif ext == ".mp3":
                try:
                    try:
                        audio = ID3(target_file)
                    except Exception:
                        audio = ID3()
                    audio.add(TIT2(encoding=3, text=title))
                    audio.add(TPE1(encoding=3, text=artists))
                    audio.add(TALB(encoding=3, text=album))
                    if year:
                        audio.add(TDRC(encoding=3, text=year))
                    if cover_bytes:
                        audio.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))
                    audio.save(target_file)
                    print(f"[Fallback] Tagged MP3: {target_file}")
                except Exception as te:
                    print(f"[Fallback] MP3 tag notice: {te}")
            elif ext == ".m4a":
                try:
                    audio = MP4(target_file)
                    audio["\xa9nam"] = [title]
                    audio["\xa9ART"] = [artists]
                    audio["\xa9alb"] = [album]
                    if year:
                        audio["\xa9day"] = [year]
                    if cover_bytes:
                        audio["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
                    audio.save()
                    print(f"[Fallback] Tagged M4A: {target_file}")
                except Exception as te:
                    print(f"[Fallback] M4A tag notice: {te}")
    except Exception as fallback_err:
        print(f"[Fallback] Fallback engine error: {fallback_err}")


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


class SaveToDriveRequest(BaseModel):
    url: str
    quality: str = "LOSSLESS"
    embed_lyrics: bool = True
    access_token: str


async def get_or_create_drive_folder(client: httpx.AsyncClient, access_token: str, folder_name: str = "SpotiFLAC") -> str:
    """Finds or creates a target folder in user's Google Drive."""
    headers = {"Authorization": f"Bearer {access_token}"}
    query = f"mimeType = 'application/vnd.google-apps.folder' and name = '{folder_name}' and trashed = false"
    
    try:
        res = await client.get(
            "https://www.googleapis.com/drive/v3/files",
            headers=headers,
            params={"q": query, "spaces": "drive", "fields": "files(id, name)"},
            timeout=15.0
        )
        if res.status_code == 200:
            files = res.json().get("files", [])
            if files:
                return files[0]["id"]
    except Exception as e:
        print(f"[GDrive] Search folder notice: {e}")

    # Create folder if not found
    create_res = await client.post(
        "https://www.googleapis.com/drive/v3/files",
        headers={**headers, "Content-Type": "application/json"},
        json={"name": folder_name, "mimeType": "application/vnd.google-apps.folder"},
        timeout=15.0
    )
    if create_res.status_code in (200, 201):
        return create_res.json()["id"]
    
    raise HTTPException(
        status_code=create_res.status_code,
        detail=f"Google Drive folder creation failed: {create_res.text}"
    )


async def upload_file_to_drive(client: httpx.AsyncClient, access_token: str, file_path: str, filename: str, mime_type: str, folder_id: str) -> dict:
    """Uploads a local audio file directly into Google Drive via multipart upload."""
    headers = {"Authorization": f"Bearer {access_token}"}
    boundary = f"boundary_{uuid.uuid4().hex}"

    metadata_bytes = json.dumps({"name": filename, "parents": [folder_id]}).encode("utf-8")
    with open(file_path, "rb") as f:
        file_bytes = f.read()

    body = (
        f"--{boundary}\r\n"
        f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
    ).encode("utf-8") + metadata_bytes + (
        f"\r\n--{boundary}\r\n"
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8") + file_bytes + (
        f"\r\n--{boundary}--\r\n"
    ).encode("utf-8")

    upload_headers = {
        **headers,
        "Content-Type": f"multipart/related; boundary={boundary}",
        "Content-Length": str(len(body)),
    }

    res = await client.post(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,webViewLink,size",
        headers=upload_headers,
        content=body,
        timeout=180.0
    )
    if res.status_code in (200, 201):
        return res.json()

    raise HTTPException(
        status_code=res.status_code,
        detail=f"Google Drive upload failed: {res.text}"
    )


@app.post("/api/save-to-drive")
async def save_to_drive(
    req: SaveToDriveRequest,
    background_tasks: BackgroundTasks
):
    """
    Downloads track on server and uploads directly to the user's Google Drive.
    The client device downloads 0 bytes of media.
    """
    if "open.spotify.com/track" not in req.url:
        raise HTTPException(
            status_code=400,
            detail="Invalid URL. Please provide a valid Spotify track link."
        )
    if not req.access_token:
        raise HTTPException(
            status_code=401,
            detail="Missing Google Drive access token."
        )

    job_id = str(uuid.uuid4())
    job_output_dir = TEMP_DIR / job_id
    job_output_dir.mkdir(parents=True, exist_ok=True)

    try:
        meta = None
        try:
            meta = await metadata_client.get_metadata_from_url_async(req.url)
        except Exception:
            pass

        # Execute download and transcoding on server
        await asyncio.to_thread(
            _download_worker,
            req.url,
            str(job_output_dir),
            req.quality,
            req.embed_lyrics,
            meta
        )

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
        ext = os.path.splitext(filename)[1].lower()
        media_types = {
            ".flac": "audio/flac",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".wav": "audio/wav",
            ".ogg": "audio/ogg",
            ".opus": "audio/opus",
        }
        mime_type = media_types.get(ext, "application/octet-stream")

        # Stream directly to user's Google Drive
        async with httpx.AsyncClient(timeout=180.0) as client:
            folder_id = await get_or_create_drive_folder(client, req.access_token, "SpotiFLAC")
            drive_file = await upload_file_to_drive(client, req.access_token, file_path, filename, mime_type, folder_id)

        # Purge temporary audio from server storage
        background_tasks.add_task(cleanup_directory, str(job_output_dir))

        file_id = drive_file.get("id")
        web_link = drive_file.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"

        return {
            "success": True,
            "file_id": file_id,
            "file_name": filename,
            "folder_name": "SpotiFLAC",
            "web_view_link": web_link,
            "size": drive_file.get("size")
        }
    except HTTPException:
        cleanup_directory(str(job_output_dir))
        raise
    except Exception as e:
        cleanup_directory(str(job_output_dir))
        raise HTTPException(
            status_code=500,
            detail=f"Google Drive cloud transfer error: {str(e)}"
        )

