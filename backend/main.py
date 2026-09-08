import asyncio
import html
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
import yt_dlp

app = FastAPI(title="Playlist Downloader MVP")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],  # tighten for prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DOWNLOAD_DIR = os.path.join(
    os.environ.get("YTQ_DOWNLOAD_DIR") or str(Path.home() / ".ytquickie" / "downloads")
)
os.makedirs(BASE_DOWNLOAD_DIR, exist_ok=True)

CONFIG_PATH = os.path.join(str(Path.home() / ".ytquickie"), "config.json")
DEFAULT_DOWNLOAD_DIR = str(Path.home() / "Downloads")


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            cfg = {}
    except (FileNotFoundError, json.JSONDecodeError):
        cfg = {}
    cfg.setdefault("download_dir", "")
    return cfg


def save_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def get_configured_download_dir() -> str:
    cfg = load_config()
    return cfg.get("download_dir") or DEFAULT_DOWNLOAD_DIR

CONCURRENCY_LIMIT = 4
semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)

MAX_TRACKS = 50
JOB_TTL_SECONDS = 60 * 60  # delete job files/state 1 hour after completion
CLEANUP_INTERVAL_SECONDS = 15 * 60

# --- In-Memory State Store ---
jobs: Dict[str, dict] = {}
job_subscribers: Dict[str, List[asyncio.Queue]] = {}
job_cancel_flags: Dict[str, bool] = {}


# --- Models ---
class FetchRequest(BaseModel):
    url: str


class SettingsRequest(BaseModel):
    download_dir: str


class StartJobRequest(BaseModel):
    video_ids: List[str]
    titles: Optional[Dict[str, str]] = None  # video_id -> title, for nice filenames


# --- Helpers ---
def sanitize_filename(name: str, fallback: str) -> str:
    if not name:
        name = fallback
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = name.strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:150] or fallback


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def clean_ytdl_error(message: str) -> str:
    """Strip ANSI codes and reduce yt-dlp errors to a short friendly reason."""
    message = ANSI_ESCAPE_RE.sub("", message or "").strip()
    lower = message.lower()
    if "video is private" in lower:
        return "This video is private and cannot be downloaded."
    if "sign in to confirm your age" in lower or "age-restricted" in lower:
        return "Age-restricted — requires a logged-in account."
    if "video is unavailable" in lower or "error code 152" in lower:
        return "This video is unavailable (removed, region-blocked, or age-gated)."
    if "not available" in lower:
        return "This video is not available."
    collapsed = re.sub(r"\s+", " ", message)
    return collapsed[:200] or "Download failed."


async def publish_event(job_id: str, payload: dict):
    if job_id in job_subscribers:
        dead = []
        for q in job_subscribers[job_id]:
            try:
                await q.put(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            job_subscribers[job_id].remove(q)


# --- Core Worker Logic ---
def _ffmpeg_location():
    if getattr(sys, "frozen", False):
        bundled = os.path.join(getattr(sys, "_MEIPASS", ""), "ffmpeg")
        if os.path.isdir(bundled):
            return bundled
    return None


def run_ytdl_download(video_id: str, output_path: str, progress_cb):
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_path,
        "ffmpeg_location": _ffmpeg_location(),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [progress_cb],
        "retries": 3,
        "fragment_retries": 3,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web"],
                "player_skip": ["configs", "webpage"],
            }
        },
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([f"https://www.youtube.com/watch?v={video_id}"])


async def process_video(job_id: str, video_id: str, job_dir: str, title: Optional[str]):
    async with semaphore:
        if job_cancel_flags.get(job_id):
            return

        track = jobs[job_id]["tracks"][video_id]
        track["status"] = "downloading"
        track["progress"] = 0
        await publish_event(job_id, {"type": "track_update", "track": track})

        safe_title = sanitize_filename(title or video_id, video_id)
        output_tmpl = os.path.join(job_dir, f"{video_id}.%(ext)s")
        final_mp3_path = os.path.join(job_dir, f"{safe_title}.mp3")

        loop = asyncio.get_running_loop()

        def progress_cb(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)
                pct = int(downloaded / total * 100) if total else None
                track["progress"] = pct
                track["status"] = "downloading"
            elif d.get("status") == "finished":
                track["status"] = "converting"
                track["progress"] = 100
            asyncio.run_coroutine_threadsafe(
                publish_event(job_id, {"type": "track_update", "track": track}), loop
            )

        try:
            raw_path = os.path.join(job_dir, f"{video_id}.mp3")
            await loop.run_in_executor(
                None, run_ytdl_download, video_id, output_tmpl, progress_cb
            )

            if os.path.exists(raw_path) and raw_path != final_mp3_path:
                # Avoid collisions if two videos sanitize to the same title
                if os.path.exists(final_mp3_path):
                    final_mp3_path = os.path.join(
                        job_dir, f"{safe_title} ({video_id[:6]}).mp3"
                    )
                os.rename(raw_path, final_mp3_path)

            track["status"] = "done"
            track["progress"] = 100
            track["file_path"] = final_mp3_path
        except Exception as e:
            track["status"] = "failed"
            track["error"] = clean_ytdl_error(str(e))

        await publish_event(job_id, {"type": "track_update", "track": track})


async def run_job(job_id: str, selected_ids: List[str], titles: Dict[str, str]):
    job_dir = os.path.join(BASE_DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    jobs[job_id]["status"] = "processing"
    await publish_event(job_id, {"type": "job_status", "status": "processing"})

    tasks = [
        process_video(job_id, vid, job_dir, titles.get(vid))
        for vid in selected_ids
    ]
    await asyncio.gather(*tasks)

    if job_cancel_flags.get(job_id):
        jobs[job_id]["status"] = "cancelled"
        await publish_event(job_id, {"type": "job_status", "status": "cancelled"})
        shutil.rmtree(job_dir, ignore_errors=True)
        return

    if len(selected_ids) == 1:
        result_path = jobs[job_id]["tracks"][selected_ids[0]].get("file_path")
    else:
        zip_base = os.path.join(BASE_DOWNLOAD_DIR, f"{job_id}_archive")
        loop = asyncio.get_running_loop()
        result_path = await loop.run_in_executor(
            None, shutil.make_archive, zip_base, "zip", job_dir
        )

    jobs[job_id]["result_path"] = result_path
    jobs[job_id]["status"] = "completed"
    jobs[job_id]["completed_at"] = time.time()
    await publish_event(job_id, {"type": "job_status", "status": "completed"})


async def cleanup_loop():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        now = time.time()
        for job_id in list(jobs.keys()):
            job = jobs[job_id]
            completed_at = job.get("completed_at")
            if completed_at and now - completed_at > JOB_TTL_SECONDS:
                job_dir = os.path.join(BASE_DOWNLOAD_DIR, job_id)
                shutil.rmtree(job_dir, ignore_errors=True)
                result_path = job.get("result_path")
                if result_path and os.path.exists(result_path):
                    os.remove(result_path)
                jobs.pop(job_id, None)
                job_subscribers.pop(job_id, None)
                job_cancel_flags.pop(job_id, None)


@app.on_event("startup")
async def start_cleanup_task():
    asyncio.create_task(cleanup_loop())


# --- Spotify (no-credentials embed scraping) ---
SPOTIFY_EMBED_URL = "https://open.spotify.com/embed/{kind}/{sid}"
SPOTIFY_TRACK_SEARCH_LIMIT = 5
SPOTIFY_MATCH_DURATION_TOLERANCE_S = 3.0
SPOTIFY_MATCH_DURATION_TOLERANCE_PCT = 0.15
SPOTIFY_MIN_SIMILARITY = 0.42

_VARIANT_KEYWORD_RE = re.compile(
    r"\b(lyrics?|lyrical|karaoke|cover|live|remix|remaster|reverb|"
    r"slowed|sped ?up|8d|instrumental|juke ?box|megamix|unplugged)\b",
    re.I,
)


def _variant_penalty(title: str) -> float:
    return 0.10 if _VARIANT_KEYWORD_RE.search(title) else 0.0


def parse_spotify_url(url: str) -> Optional[tuple]:
    """Return (kind, spotify_id) or None for 'open.spotify.com/...' URLs and spotify: URIs."""
    url = url.strip()
    uri = re.match(r"^spotify:(track|playlist|album):([A-Za-z0-9]+)(?:$|\?)", url)
    if uri:
        return uri.group(1), uri.group(2)
    m = re.search(r"(?:open|play)\.spotify\.com/(track|playlist|album)/([A-Za-z0-9]+)", url)
    if m:
        return m.group(1), m.group(2)
    return None


def _parse_next_data(html_text: str) -> Optional[dict]:
    m = re.search(
        r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html_text, re.S
    )
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def _artists_from_subtitle(subtitle) -> str:
    subtitle = html.unescape(str(subtitle or ""))
    subtitle = urllib.parse.unquote_plus(subtitle.replace("%", ", "))
    return ", ".join(
        part.strip() for part in re.split(r"[,/|·•–]", subtitle) if part.strip()
    )


def _spotify_embed_entity(html_text: str) -> tuple:
    """Return (entity dict, list of track dicts) parsed from the embed page JSON."""
    data = _parse_next_data(html_text)
    if data is None:
        return None, []
    entity, tracks = None, []

    def walk(node):
        nonlocal entity
        if isinstance(node, dict):
            if (
                entity is None
                and isinstance(node.get("type"), str)
                and node.get("type") in ("playlist", "album", "track")
            ):
                entity = node
            tl = node.get("trackList")
            if isinstance(tl, list):
                tracks.extend(t for t in tl if isinstance(t, dict))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return entity, tracks


def _extract_spotify_tracks(html_text: str) -> List[dict]:
    entity, track_objs = _spotify_embed_entity(html_text)
    if not track_objs and entity and entity.get("type") == "track":
        track_objs = [entity]
    items = []
    for obj in track_objs:
        title = (obj.get("title") or obj.get("name") or "").strip()
        if not title:
            continue
        if isinstance(obj.get("artists"), list):
            artists = ", ".join(
                str(a.get("name", "")).strip()
                for a in obj["artists"]
                if isinstance(a, dict) and a.get("name")
            )
        else:
            artists = _artists_from_subtitle(obj.get("subtitle"))
        duration_ms = obj.get("duration")
        duration = (
            round(duration_ms / 1000) if isinstance(duration_ms, (int, float)) else None
        )
        items.append({"title": title, "artist": artists, "duration": duration})
    return items


def _spotify_playlist_title(html_text: str, kind: str, default: str) -> Optional[str]:
    if kind == "track":
        items = _extract_spotify_tracks(html_text)
        if items:
            t = items[0]
            return f"{t['artist']} - {t['title']}" if t["artist"] else t["title"]
        return default
    entity, _ = _spotify_embed_entity(html_text)
    if entity:
        name = entity.get("name") or entity.get("title")
        if name:
            return html.unescape(str(name)).strip() or default
    return default


def _get_spotify_embed(kind: str, sid: str) -> str:
    url = SPOTIFY_EMBED_URL.format(kind=kind, sid=sid)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _search_youtube(query: str) -> List[dict]:
    ydl_opts = {
        "extract_flat": True,
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{SPOTIFY_TRACK_SEARCH_LIMIT}:{query}", download=False)
    entries = info.get("entries") or []
    return [
        {
            "id": e.get("id"),
            "title": (e.get("title") or "").strip(),
            "duration": e.get("duration"),
        }
        for e in entries
        if e and e.get("id") and e.get("title")
    ]


def _title_score(a: str, b: str) -> float:
    a_tokens = set(re.findall(r"[a-z0-9]+", a.lower()))
    b_tokens = set(re.findall(r"[a-z0-9]+", b.lower()))
    if not a_tokens or not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / len(a_tokens)


def _pick_best_match(track: dict, query: str) -> Optional[dict]:
    best, best_score = None, None
    for hit in _search_youtube(query):
        sim = _title_score(query, hit["title"])
        hit_dur = hit.get("duration")
        if hit_dur is None:
            dur_diff, dur_ok = 0.0, True
        else:
            dur_diff = abs(hit_dur - (track["duration"] or 0))
            dur_ok = dur_diff <= SPOTIFY_MATCH_DURATION_TOLERANCE_S or (
                track["duration"]
                and dur_diff <= track["duration"] * SPOTIFY_MATCH_DURATION_TOLERANCE_PCT
            )
        if not dur_ok:
            continue
        score = sim - _variant_penalty(hit["title"]) - (dur_diff / 60.0) * 0.1
        if best_score is None or score > best_score:
            best, best_score = hit, score
    if best and best_score is not None and best_score >= SPOTIFY_MIN_SIMILARITY:
        return best
    return None


def _resolve_spotify_track(track: dict) -> Optional[dict]:
    title = track["title"]
    artists = track["artist"]
    primary = artists.split(",")[0].strip()
    display = f"{artists} - {title}"

    best = _pick_best_match(track, f"{artists} - {title} audio")
    if best is None and artists != primary:
        best = _pick_best_match(track, f"{primary} - {title} audio")

    if best is None:
        return None
    return {
        "id": best["id"],
        "title": display,
        "duration": track["duration"],
        "thumbnail": None,
        "match_title": best["title"],
    }


def fetch_spotify_metadata(url: str) -> dict:
    parsed = parse_spotify_url(url)
    if not parsed:
        raise HTTPException(status_code=400, detail="Invalid Spotify URL.")

    kind, sid = parsed
    try:
        html_text = _get_spotify_embed(kind, sid)
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"Could not fetch Spotify data from Spotify: {e}"
        )

    tracks = _extract_spotify_tracks(html_text)
    if not tracks:
        raise HTTPException(status_code=400, detail="No playable tracks found for that Spotify link.")

    playlist_title = _spotify_playlist_title(html_text, kind, "Spotify")
    tracks = tracks[:MAX_TRACKS]

    results, skipped = [], []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for track, resolved in zip(tracks, pool.map(_resolve_spotify_track, tracks)):
            if resolved:
                results.append(resolved)
            else:
                skipped.append(f"{track['artist']} - {track['title']}")

    return {
        "playlist_title": playlist_title,
        "total_tracks_in_playlist": len(tracks),
        "returned_tracks": len(results),
        "truncated": len(tracks) == MAX_TRACKS,
        "max_tracks": MAX_TRACKS,
        "skipped": skipped,
        "tracks": results,
    }


# --- Endpoints ---
@app.post("/api/playlist/fetch")
def fetch_playlist_metadata(req: FetchRequest):
    if parse_spotify_url(req.url):
        return fetch_spotify_metadata(req.url)

    ydl_opts = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)

            if "entries" not in info:
                title = info.get("title", "")
                video_id = info.get("id")
                if (
                    not video_id
                    or not title
                    or title in ("[Private video]", "[Deleted video]")
                    or info.get("availability")
                    in ("private", "subscriber_only", "needs_auth")
                ):
                    raise HTTPException(
                        status_code=400, detail="Could not retrieve video information."
                    )
                return {
                    "playlist_title": title,
                    "total_tracks_in_playlist": 1,
                    "returned_tracks": 1,
                    "truncated": False,
                    "max_tracks": MAX_TRACKS,
                    "tracks": [
                        {
                            "id": video_id,
                            "title": title or "Unknown Title",
                            "duration": info.get("duration"),
                            "thumbnail": info.get("thumbnails", [{}])[0].get("url")
                            if info.get("thumbnails")
                            else None,
                        }
                    ],
                }

            entries = [e for e in info["entries"] if e]
            items = []
            for entry in entries:
                if not entry:
                    continue

                title = entry.get("title", "")
                if (
                    not entry.get("id")
                    or not title
                    or title in ("[Private video]", "[Deleted video]")
                    or entry.get("availability")
                    in ("private", "subscriber_only", "needs_auth")
                ):
                    continue

                if len(items) >= MAX_TRACKS:
                    break

                items.append(
                    {
                        "id": entry.get("id"),
                        "title": title or "Unknown Title",
                        "duration": entry.get("duration"),
                        "thumbnail": entry.get("thumbnails", [{}])[0].get("url")
                        if entry.get("thumbnails")
                        else None,
                    }
                )

            truncated = len(entries) > MAX_TRACKS

            return {
                "playlist_title": info.get("title", "Untitled Playlist"),
                "total_tracks_in_playlist": len(entries),
                "returned_tracks": len(items),
                "truncated": truncated,
                "max_tracks": MAX_TRACKS,
                "tracks": items,
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch metadata: {str(e)}")


@app.post("/api/jobs")
def create_job(req: StartJobRequest, bg_tasks: BackgroundTasks):
    if not req.video_ids:
        raise HTTPException(status_code=400, detail="No video IDs selected.")
    if len(req.video_ids) > MAX_TRACKS:
        raise HTTPException(status_code=400, detail=f"Max {MAX_TRACKS} tracks per job.")

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "queued",
        "tracks": {
            vid: {"video_id": vid, "status": "pending", "progress": 0}
            for vid in req.video_ids
        },
        "result_path": None,
        "completed_at": None,
    }
    job_subscribers[job_id] = []
    job_cancel_flags[job_id] = False

    bg_tasks.add_task(run_job, job_id, req.video_ids, req.titles or {})
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, **job}


@app.get("/api/jobs/{job_id}/stream")
async def stream_job_progress(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    queue: asyncio.Queue = asyncio.Queue()
    job_subscribers[job_id].append(queue)

    async def event_generator():
        yield {"event": "state", "data": json.dumps(jobs[job_id])}
        if jobs[job_id]["status"] in ("completed", "failed", "cancelled"):
            return
        try:
            while True:
                data = await queue.get()
                yield {"event": "update", "data": json.dumps(data)}
                if jobs[job_id]["status"] in ("completed", "failed", "cancelled"):
                    break
        finally:
            if queue in job_subscribers.get(job_id, []):
                job_subscribers[job_id].remove(queue)

    return EventSourceResponse(event_generator())


@app.get("/api/jobs/{job_id}/download")
def download_archive(job_id: str):
    job = jobs.get(job_id)
    if not job or not job.get("result_path"):
        raise HTTPException(status_code=404, detail="Result not ready or job expired")

    result_path = job["result_path"]
    if not os.path.exists(result_path):
        raise HTTPException(status_code=404, detail="Result file is missing")

    if os.path.splitext(result_path)[1].lower() == ".mp3":
        return FileResponse(
            path=result_path,
            filename=os.path.basename(result_path),
            media_type="audio/mpeg",
        )

    return FileResponse(
        path=result_path,
        filename=f"playlist_{job_id[:8]}.zip",
        media_type="application/zip",
    )


@app.delete("/api/jobs/{job_id}")
async def cancel_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job_cancel_flags[job_id] = True
    job_dir = os.path.join(BASE_DOWNLOAD_DIR, job_id)

    if job["status"] in ("completed",):
        # already done — just clean up files, keep record briefly
        shutil.rmtree(job_dir, ignore_errors=True)
        if job.get("result_path") and os.path.exists(job["result_path"]):
            os.remove(job["result_path"])
        jobs.pop(job_id, None)
        job_subscribers.pop(job_id, None)
        job_cancel_flags.pop(job_id, None)
        return {"status": "deleted"}

    job["status"] = "cancelling"
    await publish_event(job_id, {"type": "job_status", "status": "cancelling"})
    return {"status": "cancelling"}


@app.get("/api/settings")
def get_settings():
    return {"download_dir": get_configured_download_dir()}


@app.put("/api/settings")
def update_settings(req: SettingsRequest):
    path = req.download_dir.strip() or ""
    if path:
        path = os.path.abspath(os.path.expanduser(path))
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"Cannot use folder: {e}")
    cfg = load_config()
    cfg["download_dir"] = path
    save_config(cfg)
    return {"download_dir": get_configured_download_dir()}


# --- Static frontend (built React app) ---
# Mounted last so /api/* routes above are matched first.
_APP_ROOT = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(_APP_ROOT, "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
