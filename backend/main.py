import asyncio
import json
import os
import re
import shutil
import time
import uuid
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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

BASE_DOWNLOAD_DIR = "/tmp/playlist_downloads"
os.makedirs(BASE_DOWNLOAD_DIR, exist_ok=True)

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
def run_ytdl_download(video_id: str, output_path: str, progress_cb):
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_path,
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

    zip_base = os.path.join(BASE_DOWNLOAD_DIR, f"{job_id}_archive")
    loop = asyncio.get_running_loop()
    zip_path = await loop.run_in_executor(
        None, shutil.make_archive, zip_base, "zip", job_dir
    )

    jobs[job_id]["zip_path"] = zip_path
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
                zip_path = job.get("zip_path")
                if zip_path and os.path.exists(zip_path):
                    os.remove(zip_path)
                jobs.pop(job_id, None)
                job_subscribers.pop(job_id, None)
                job_cancel_flags.pop(job_id, None)


@app.on_event("startup")
async def start_cleanup_task():
    asyncio.create_task(cleanup_loop())


# --- Endpoints ---
@app.post("/api/playlist/fetch")
def fetch_playlist_metadata(req: FetchRequest):
    ydl_opts = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
            if "entries" not in info:
                raise HTTPException(status_code=400, detail="Provided URL is not a playlist.")

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
        "zip_path": None,
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
    if not job or not job.get("zip_path"):
        raise HTTPException(status_code=404, detail="Archive not ready or job expired")

    return FileResponse(
        path=job["zip_path"],
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
        if job.get("zip_path") and os.path.exists(job["zip_path"]):
            os.remove(job["zip_path"])
        jobs.pop(job_id, None)
        job_subscribers.pop(job_id, None)
        job_cancel_flags.pop(job_id, None)
        return {"status": "deleted"}

    job["status"] = "cancelling"
    await publish_event(job_id, {"type": "job_status", "status": "cancelling"})
    return {"status": "cancelling"}
