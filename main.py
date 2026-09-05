import os
import uuid
import glob
import shutil
import asyncio
import time
import re
import difflib
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

# In-memory background jobs registry
active_jobs: dict[str, dict] = {}


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
    
    meta = None
    try:
        _, tracks, _, _ = await metadata_client.get_url_async(url)
        if tracks:
            meta = tracks[0]
    except Exception as me:
        err = f"Metadata error: {me}"

    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            await asyncio.to_thread(_download_worker, url, str(debug_dir), "LOSSLESS", False, meta)
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
    url: str = Query(..., description="Spotify URL (track, album, playlist, or artist)")
):
    """Fetches metadata for a track, album, playlist, or artist without downloading."""
    if "open.spotify.com" not in url:
        raise HTTPException(
            status_code=400,
            detail="Invalid URL. Please provide a valid Spotify link."
        )

    try:
        name, tracks, cover, _ = await metadata_client.get_url_async(url)
        if not tracks:
            raise HTTPException(status_code=404, detail="No tracks found for this link.")

        # Single track link
        if "open.spotify.com/track/" in url:
            meta = tracks[0]
            meta_dict = meta.model_dump()
            meta_dict["is_collection"] = False
            return meta_dict

        # Collection: Album, Playlist, or Artist
        t_type = "album" if "/album/" in url else ("/playlist/" in url and "playlist" or "artist")
        return {
            "is_collection": True,
            "type": t_type,
            "name": name,
            "cover_url": cover or (tracks[0].cover_url if tracks else ""),
            "total_tracks": len(tracks),
            "tracks": [
                {
                    "id": tr.id,
                    "title": tr.title,
                    "artists": tr.artists,
                    "album": tr.album,
                    "duration_ms": tr.duration_ms,
                    "cover_url": tr.cover_url,
                    "url": tr.external_url or f"https://open.spotify.com/track/{tr.id}"
                }
                for tr in tracks
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch metadata: {str(e)}")


def score_candidate(
    cand_title: str,
    cand_uploader: str,
    cand_duration: float,
    expected_title: str,
    expected_artist: str,
    expected_duration: float
) -> tuple[float, str]:
    """
    Rigorously scores an audio stream candidate against expected metadata.
    Returns (score, reason). Candidates with score <= 0 are rejected.
    Filters out remixes, mashups, slowed+reverb, sped up, covers, and previews.
    """
    title_lower = (cand_title or "").lower()
    uploader_lower = (cand_uploader or "").lower()
    exp_title_lower = (expected_title or "").lower()
    exp_artist_lower = (expected_artist or "").lower()

    # Clean core expected title (strip parenthetical information like "(Remastered 2013)")
    clean_exp_title = re.sub(r'\s*[\(\[].*?[\)\]]', '', exp_title_lower).strip()

    # Banned modifications unless explicitly present in the original track title
    banned_keywords = [
        'mashup', 'mash up', 'remix', 'slowed', 'reverb', 'sped up', 'speed up', 'speedup',
        'slower', 'faster', 'cover', 'tribute', 'karaoke', 'instrumental', 'acoustic',
        'edit audio', 'edit)', 'tik tok', 'tiktok', 'nightcore', 'daycore', 'chopped',
        'bass boosted', '8d audio', '8d', 'guitar cover', 'piano cover', 'drum cover',
        'orchestral cover', 'backing track'
    ]

    for kw in banned_keywords:
        if kw in title_lower and kw not in exp_title_lower:
            return -1000.0, f"Disqualified: contains banned keyword '{kw}'"

    # Reject artist mashups/versus unless in official metadata
    if any(delim in title_lower for delim in [" x ", " X ", " vs ", " vs. ", " / "]):
        if not any(delim in exp_title_lower or delim in exp_artist_lower for delim in [" x ", " X ", " vs ", " vs. "]):
            return -1000.0, "Disqualified: contains mashup / versus delimiter"

    # Title word match: ensure significant title words appear
    words = re.findall(r'\b[a-zA-Z0-9]+\b', clean_exp_title)
    sig_words = [w for w in words if len(w) >= 3 or w in ('be', 'me', 'we', 'go', 'do', 'no')]
    if sig_words:
        matched_words = [w for w in sig_words if w in title_lower]
        coverage = len(matched_words) / len(sig_words)
        if coverage < 0.65:
            return -500.0, f"Disqualified: low title word coverage ({coverage:.2f})"

    # Primary artist match (must appear in candidate title or uploader/channel)
    primary_artist = re.split(r'[,&/]', exp_artist_lower)[0].strip()
    artist_words = [w for w in re.findall(r'\b[a-zA-Z0-9]+\b', primary_artist) if len(w) > 2]
    artist_found = (primary_artist in title_lower or primary_artist in uploader_lower)
    if not artist_found and artist_words:
        artist_found = all(w in title_lower or w in uploader_lower for w in artist_words)

    if not artist_found:
        return -400.0, "Disqualified: primary artist missing from title and uploader"

    # Duration validation
    diff = 0.0
    if expected_duration > 0 and cand_duration > 0:
        if expected_duration > 60 and cand_duration < 55:
            return -800.0, f"Disqualified: preview snippet ({cand_duration:.1f}s < 55s)"
        diff = abs(cand_duration - expected_duration)
        if diff > 35.0:
            return -300.0, f"Disqualified: duration difference too large ({diff:.1f}s)"

    # Base score
    score = 100.0 - (diff * 2.5)

    # Bonuses for authentic uploads
    if "official" in title_lower or "official" in uploader_lower:
        score += 15.0
    if "topic" in uploader_lower or "vevo" in uploader_lower:
        score += 25.0
    if "lyrics" in title_lower and not ("edit" in title_lower or "mashup" in title_lower):
        score += 8.0
    if primary_artist in uploader_lower:
        score += 20.0

    sim = difflib.SequenceMatcher(None, f"{clean_exp_title} {primary_artist}", title_lower).ratio()
    score += (sim * 20.0)

    return score, f"Score: {score:.1f}"


def _download_worker(url: str, output_dir: str, quality: str, embed_lyrics: bool, meta=None):
    """Synchronous worker function executed in an asyncio thread pool."""
    import sys
    import urllib.request
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # Create and assign an isolated event loop for this background worker thread
    thread_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(thread_loop)

    qobuz_token = os.getenv("QOBUZ_TOKEN") or None
    tidal_api = os.getenv("TIDAL_CUSTOM_API") or None

    isrc_str = str(getattr(meta, "isrc", "") or "").strip().upper()
    western_prefixes = ("US", "GB", "UK", "FR", "DE", "CA", "AU", "SE", "NL", "IT", "ES")
    is_western = any(isrc_str.startswith(p) for p in western_prefixes)

    if is_western:
        # Western releases with European/US ISRC: Prioritize Amazon 24-bit Studio Master FLAC
        services = ["amazon", "qobuz", "deezer", "youtube"]
    else:
        # Regional / Bollywood / Asian tracks: Prioritize YouTube Music for instant <4s extraction
        services = ["youtube", "amazon", "deezer"]
    if tidal_api:
        services.insert(1, "tidal")

    # 1. Attempt SpotiFLAC multi-service extraction with direct native thread loop
    import builtins
    original_input = builtins.input
    builtins.input = lambda *a: (_ for _ in ()).throw(EOFError("Non-interactive server execution"))
    try:
        from SpotiFLAC.downloader import DownloadOptions, SpotiflacDownloader
        opts = DownloadOptions(
            output_dir=output_dir,
            services=services,
            quality=quality,
            embed_lyrics=False,
            enrich_metadata=False,
            track_max_retries=0,
            qobuz_token=qobuz_token,
            tidal_custom_api=tidal_api,
            allow_fallback=True,
            timeout_s=30,
        )
        dl = SpotiflacDownloader(opts)
        thread_loop.run_until_complete(dl.run_async(url))
    except Exception as e:
        print(f"[SpotiflacDownloader] Run notice: {e}")
    finally:
        builtins.input = original_input
        try:
            thread_loop.close()
        except Exception:
            pass

    # Clean up any leftover incomplete part files
    for p in glob.glob(str(Path(output_dir) / "**/*.part"), recursive=True):
        try:
            os.remove(p)
        except Exception:
            pass

    # Check if SpotiFLAC produced an audio file
    downloaded = glob.glob(str(Path(output_dir) / "**/*.*"), recursive=True)
    audio_files = [
        f for f in downloaded
        if f.lower().endswith(('.flac', '.mp3', '.m4a', '.wav', '.ogg', '.opus'))
    ]
    if audio_files:
        # If the file came from raw extraction (e.g. Amazon Antra B00...flac), ensure tags & filename are pristine
        raw_file = audio_files[0]
        raw_ext = os.path.splitext(raw_file)[1].lower()
        t_title = getattr(meta, "title", "Track") if meta else "Track"
        t_artists = getattr(meta, "artists", "Artist") if meta else "Artist"
        t_album = (getattr(meta, "album", "") if meta else "") or t_title
        t_cover = getattr(meta, "cover_url", "") if meta else ""
        t_year = str(getattr(meta, "release_date", ""))[:4] if meta and getattr(meta, "release_date", "") else ""

        safe_t = re.sub(r'[\\/*?:"<>|]', "", t_title)
        safe_a = re.sub(r'[\\/*?:"<>|]', "", t_artists)
        expected_name = f"{safe_t} - {safe_a}{raw_ext}"
        expected_path = os.path.join(output_dir, expected_name)

        try:
            if raw_ext == ".flac":
                from mutagen.flac import FLAC, Picture
                audio_tag = FLAC(raw_file)
                if not audio_tag.get("title"):
                    audio_tag["title"] = t_title
                if not audio_tag.get("artist"):
                    audio_tag["artist"] = t_artists
                if not audio_tag.get("album"):
                    audio_tag["album"] = t_album
                if t_year and not audio_tag.get("date"):
                    audio_tag["date"] = t_year
                if t_cover and not audio_tag.pictures:
                    try:
                        pic_data = httpx.get(t_cover, timeout=10.0).content
                        pic = Picture()
                        pic.type = 3
                        pic.mime = "image/jpeg"
                        pic.data = pic_data
                        audio_tag.add_picture(pic)
                    except Exception:
                        pass
                audio_tag.save()

            if os.path.abspath(raw_file) != os.path.abspath(expected_path):
                if os.path.exists(expected_path):
                    os.remove(expected_path)
                os.rename(raw_file, expected_path)
                print(f"[Worker] Cleanly normalized file: {expected_name}")
        except Exception as norm_err:
            print(f"[Worker] Tag normalization notice: {norm_err}")
        return

    # 2. Resilient Direct Stream Fallback via YouTube with Candidate Verification
    print("[Fallback] Hi-Res mirrors busy. Executing verified studio stream fallback...")
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

        flat_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "ios", "web_embedded", "tv_embedded", "mweb"]
                }
            },
        }

        ydl_dl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "outtmpl": out_tmpl,
            "postprocessors": [postprocessor],
            "extractor_args": {
                "youtube": {
                    "player_client": ["android", "ios", "web_embedded", "tv_embedded", "mweb"]
                }
            },
        }

        # Multi-query strategy for YouTube
        search_queries = [
            f"ytsearch5:{title} {artists} official audio",
            f"ytsearch5:{title} {artists} topic",
            f"ytsearch5:{artists} - {title}",
            f"ytsearch5:{title} {artists}",
        ]

        scored_candidates = []
        seen_ids = set()

        with yt_dlp.YoutubeDL(flat_opts) as ydl_flat:
            for q in search_queries:
                try:
                    res = ydl_flat.extract_info(q, download=False)
                    for entry in (res.get("entries") or []):
                        if not entry:
                            continue
                        vid = entry.get("id")
                        if not vid or vid in seen_ids:
                            continue
                        seen_ids.add(vid)

                        c_title = entry.get("title") or ""
                        c_uploader = entry.get("channel") or entry.get("uploader") or ""
                        c_dur = float(entry.get("duration") or 0)
                        c_url = entry.get("url") or entry.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}"

                        sc, reason = score_candidate(c_title, c_uploader, c_dur, title, artists, expected_duration)
                        print(f"[Fallback Candidate] Score {sc:5.1f}: '{c_title}' ({c_dur}s) - {reason}")
                        if sc > 0:
                            scored_candidates.append({
                                "score": sc,
                                "url": c_url,
                                "title": c_title,
                                "duration": c_dur,
                            })
                except Exception as qe:
                    print(f"[Fallback Search] Query error on '{q[:30]}': {qe}")

        # Sort candidates descending by match score
        scored_candidates.sort(key=lambda x: x["score"], reverse=True)

        for cand_info in scored_candidates[:3]:
            try:
                print(f"[Fallback Download] Attempting candidate (score {cand_info['score']:.1f}): {cand_info['title']}...")
                with yt_dlp.YoutubeDL(ydl_dl_opts) as ydl_dl:
                    ydl_dl.download([cand_info["url"]])

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
                    print(f"[Fallback] Downloaded: {cand} (duration: {cand_duration:.1f}s, size: {cand_size} bytes)")

                    min_allowed = min(expected_duration * 0.65, 45) if expected_duration > 45 else 20
                    if cand_duration >= min_allowed:
                        target_file = cand
                        download_success = True
                        break
                    else:
                        print(f"[Fallback] Rejected as preview snippet ({cand_duration:.1f}s < {min_allowed:.1f}s).")
                        try:
                            os.remove(cand)
                        except Exception:
                            pass
            except Exception as de:
                print(f"[Fallback Download] Failed on candidate: {de}")

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
            _, tracks, _, _ = await metadata_client.get_url_async(url)
            if tracks:
                meta = tracks[0]
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
    folder_name: str = "SpotiFLAC"


async def get_or_create_drive_folder(
    client: httpx.AsyncClient,
    access_token: str,
    folder_name: str = "SpotiFLAC",
    parent_id: str | None = None
) -> str:
    """Finds or creates a target folder in user's Google Drive, optionally under a parent folder."""
    headers = {"Authorization": f"Bearer {access_token}"}
    query = f"mimeType = 'application/vnd.google-apps.folder' and name = '{folder_name}' and trashed = false"
    if parent_id:
        query += f" and '{parent_id}' in parents"

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

    create_payload = {"name": folder_name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        create_payload["parents"] = [parent_id]

    create_res = await client.post(
        "https://www.googleapis.com/drive/v3/files",
        headers={**headers, "Content-Type": "application/json"},
        json=create_payload,
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
    Synchronous direct upload endpoint for a single track.
    (Kept for backward compatibility).
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
            _, trs, _, _ = await metadata_client.get_url_async(req.url)
            if trs:
                meta = trs[0]
        except Exception:
            pass

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

        async with httpx.AsyncClient(timeout=180.0) as client:
            folder_id = await get_or_create_drive_folder(client, req.access_token, "SpotiFLAC")
            drive_file = await upload_file_to_drive(client, req.access_token, file_path, filename, mime_type, folder_id)

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


# ===========================================================================
# Autonomous Cloud Background Jobs (Detached from client browser tab)
# ===========================================================================

async def run_drive_job(
    job_id: str,
    tracks: list,
    quality: str,
    embed_lyrics: bool,
    access_token: str,
    base_folder_name: str = "SpotiFLAC",
    subfolder_name: str | None = None
):
    """
    Independent server-side worker that downloads and uploads songs to Google Drive.
    Runs detached in the background — continues running even if user closes tab.
    """
    job = active_jobs.get(job_id)
    if not job:
        return

    job["status"] = "in_progress"
    job["updated_at"] = time.time()
    print(f"[DriveJob {job_id[:8]}] Started processing {len(tracks)} track(s)...")

    async with httpx.AsyncClient(timeout=180.0) as client:
        try:
            base_id = await get_or_create_drive_folder(client, access_token, base_folder_name)
            target_folder_id = base_id
            if subfolder_name:
                # Clean invalid folder name characters
                safe_subfolder = re.sub(r'[\\/*?:"<>|]', "", subfolder_name).strip()
                if safe_subfolder:
                    target_folder_id = await get_or_create_drive_folder(
                        client, access_token, safe_subfolder, parent_id=base_id
                    )
        except Exception as fe:
            job["status"] = "failed"
            job["error"] = f"Failed to initialize Google Drive folder: {str(fe)}"
            job["updated_at"] = time.time()
            print(f"[DriveJob {job_id[:8]}] Folder init failed: {fe}")
            return

        for idx, tr in enumerate(tracks):
            track_title = tr.title
            track_artists = tr.artists
            track_url = tr.external_url or f"https://open.spotify.com/track/{tr.id}"

            job["current_track_title"] = f"{track_title} - {track_artists}"
            job["current_track_index"] = idx + 1
            job["updated_at"] = time.time()

            track_dir = TEMP_DIR / f"{job_id}_{idx}"
            track_dir.mkdir(parents=True, exist_ok=True)

            try:
                print(f"[DriveJob {job_id[:8]}] [{idx+1}/{len(tracks)}] Processing: {track_title}...")
                await asyncio.to_thread(
                    _download_worker,
                    track_url,
                    str(track_dir),
                    quality,
                    embed_lyrics,
                    meta=tr
                )

                downloaded_files = glob.glob(str(track_dir / "**/*.*"), recursive=True)
                audio_files = [
                    f for f in downloaded_files
                    if f.lower().endswith(('.flac', '.mp3', '.m4a', '.wav', '.ogg', '.opus'))
                ]

                if not audio_files:
                    raise Exception("Audio stream could not be extracted from providers.")

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

                drive_file = await upload_file_to_drive(
                    client,
                    access_token,
                    file_path,
                    filename,
                    mime_type,
                    target_folder_id
                )

                file_id = drive_file.get("id")
                web_link = drive_file.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"

                job["items"].append({
                    "title": track_title,
                    "artists": track_artists,
                    "status": "completed",
                    "file_name": filename,
                    "file_id": file_id,
                    "web_view_link": web_link,
                    "size": drive_file.get("size"),
                })
                job["completed_tracks"] += 1
                print(f"[DriveJob {job_id[:8]}] [{idx+1}/{len(tracks)}] Uploaded to Drive: {filename}")
            except Exception as item_err:
                print(f"[DriveJob {job_id[:8]}] [{idx+1}/{len(tracks)}] Error: {item_err}")
                job["items"].append({
                    "title": track_title,
                    "artists": track_artists,
                    "status": "failed",
                    "error": str(item_err),
                })
                job["failed_tracks"] += 1
            finally:
                cleanup_directory(str(track_dir))
                job["updated_at"] = time.time()

    if job["completed_tracks"] > 0:
        job["status"] = "completed"
    else:
        job["status"] = "failed"
        job["error"] = "No tracks could be downloaded and uploaded."
    job["updated_at"] = time.time()
    print(f"[DriveJob {job_id[:8]}] Finished: {job['completed_tracks']} succeeded, {job['failed_tracks']} failed.")


@app.post("/api/jobs/save-to-drive")
async def create_drive_job(req: SaveToDriveRequest):
    """
    Creates an asynchronous Google Drive background job.
    Returns immediately so the client can safely close the tab while the server processes.
    """
    if "open.spotify.com" not in req.url:
        raise HTTPException(
            status_code=400,
            detail="Invalid Spotify URL. Please provide a valid Spotify link."
        )
    if not req.access_token:
        raise HTTPException(
            status_code=401,
            detail="Missing Google Drive access token."
        )

    try:
        name, tracks, cover, _ = await metadata_client.get_url_async(req.url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch Spotify metadata: {e}")

    if not tracks:
        raise HTTPException(status_code=404, detail="No tracks found for this link.")

    job_id = str(uuid.uuid4())
    is_collection = len(tracks) > 1 or ("/album/" in req.url) or ("/playlist/" in req.url)
    subfolder = name if is_collection else None

    active_jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "url": req.url,
        "name": name,
        "cover_url": cover or (tracks[0].cover_url if tracks else ""),
        "total_tracks": len(tracks),
        "completed_tracks": 0,
        "failed_tracks": 0,
        "current_track_title": "",
        "current_track_index": 0,
        "created_at": time.time(),
        "updated_at": time.time(),
        "items": [],
        "error": None,
    }

    # Start detached background task on server
    asyncio.create_task(
        run_drive_job(
            job_id=job_id,
            tracks=tracks,
            quality=req.quality,
            embed_lyrics=req.embed_lyrics,
            access_token=req.access_token,
            base_folder_name=req.folder_name or "SpotiFLAC",
            subfolder_name=subfolder,
        )
    )

    return {
        "job_id": job_id,
        "status": "queued",
        "name": name,
        "total_tracks": len(tracks),
        "message": "Cloud transfer job queued. You can safely close this browser tab!"
    }


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str):
    """Fetches real-time status and progress for a background Google Drive job."""
    job = active_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired.")
    return job


@app.get("/api/jobs")
async def list_jobs():
    """Lists the 20 most recent background jobs."""
    sorted_jobs = sorted(active_jobs.values(), key=lambda j: j.get("created_at", 0), reverse=True)[:20]
    return sorted_jobs

