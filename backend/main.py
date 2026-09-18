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
from yt_dlp.cookies import extract_cookies_from_browser

app = FastAPI(title="Playlist Downloader MVP")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],  # tighten for prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_no_cache_header(request, call_next):
    response = await call_next(request)
    if request.url.path in ("/", "/index.html") or request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

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


class RetryRequest(BaseModel):
    video_ids: Optional[List[str]] = None  # default: all failed tracks
    titles: Optional[Dict[str, str]] = None


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
    if "sign in to confirm" in lower or "you're not a bot" in lower:
        return (
            "YouTube blocked this download (bot check). "
            "Close your browser (it locks cookies), then use RETRY. "
            "If it persists, sign in to YouTube in Firefox, close Firefox, and retry."
        )
    if "video is private" in lower:
        return "This video is private and cannot be downloaded."
    if "sign in to confirm your age" in lower or "age-restricted" in lower:
        return "Age-restricted — requires a logged-in account."
    if "requested format is not available" in lower or "only images are available" in lower:
        return (
            "No downloadable audio found (YouTube returned images only — "
            "signature check failed). Close your browser and hit RETRY; "
            "the app retries automatically without cookies and with opus/m4a fallback."
        )
    if "signature" in lower and "fail" in lower:
        return (
            "YouTube signature check failed (missing JS solver). "
            "Close your browser and hit RETRY."
        )
    if "video is unavailable" in lower or "error code 152" in lower:
        return "This video is unavailable (removed, region-blocked, or age-gated)."
    if "not available" in lower:
        return "This video is not available."
    collapsed = re.sub(r"\s+", " ", message)
    return collapsed[:280] or "Download failed."


def _should_retry_without_cookies(message: str) -> bool:
    """Signature/bot failures are often caused by a locked or stale cookie jar."""
    lower = (message or "").lower()
    markers = (
        "requested format is not available",
        "only images are available",
        "signature",
        "n challenge",
        "sign in to confirm",
        "you're not a bot",
        "cookies",
        "dpapi",
    )
    return any(m in lower for m in markers)


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
# Order matters: Firefox first (no DPAPI issues on Windows), then Chromium-based.
COOKIE_BROWSERS = ("firefox", "chrome", "edge", "brave", "opera", "chromium")
_cookies_cache: dict = {"at": 0.0, "browser": None, "found": False}
_COOKIE_CACHE_TTL_SECONDS = 10 * 60


def resolve_cookies_browser() -> Optional[tuple]:
    """Return the first installed browser whose cookie store is readable, or None."""
    now = time.time()
    # Only cache successful lookups; retry on failure.
    if _cookies_cache["found"] and now - _cookies_cache["at"] < _COOKIE_CACHE_TTL_SECONDS:
        return _cookies_cache["browser"]

    for name in COOKIE_BROWSERS:
        try:
            jar = extract_cookies_from_browser(name)
        except Exception:
            continue
        if jar:
            count = sum(1 for _ in jar) if hasattr(jar, "__iter__") else None
            if count:
                _cookies_cache.update(at=now, browser=(name,), found=True)
                return (name,)

    return None


def _bundled_nodejs_dir() -> Optional[str]:
    """Directory holding a bundled node binary (PyInstaller bundle or repo checkout)."""
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(os.path.join(meipass, "nodejs"))
    if os.environ.get("YTQ_NODE_DIR"):
        candidates.append(os.environ["YTQ_NODE_DIR"])
    repo_node = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "release", "nodejs"
    )
    candidates.append(repo_node)
    for d in candidates:
        if d and os.path.isdir(d):
            return d
    return None


def _find_node_binary() -> Optional[str]:
    """Find node.js binary: explicit env override, bundled copy, PATH, common locations."""
    import shutil
    # Explicit override wins (file or directory).
    env_node = os.environ.get("YTQ_NODE")
    if env_node:
        if os.path.isfile(env_node):
            return env_node
        candidate = os.path.join(env_node, "node.exe" if os.name == "nt" else "node")
        if os.path.isfile(candidate):
            return candidate
    # Bundled copy shipped with the frozen app / release/nodejs.
    bundled_dir = _bundled_nodejs_dir()
    if bundled_dir:
        candidate = os.path.join(
            bundled_dir, "node.exe" if os.name == "nt" else "node"
        )
        if os.path.isfile(candidate):
            return candidate
    node = shutil.which("node")
    if node:
        return node
    # Common Windows install paths
    for candidate in [
        os.path.join(os.environ.get("ProgramFiles", ""), "nodejs", "node.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "nodejs", "node.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "nodejs", "node.exe"),
    ]:
        if candidate and os.path.isfile(candidate):
            return candidate
    return None


def _js_runtime_opts() -> dict:
    """yt-dlp options enabling YouTube's JS-challenge solver via Node.

    Without this, signature/n-challenge solving fails and YouTube returns
    only storyboards/images -> 'Requested format is not available'.
    """
    node = _find_node_binary()
    if node:
        js_runtimes = {"node": {"path": node}}
    else:
        # Let yt-dlp auto-detect (deno/node on PATH); better than disabling EJS.
        js_runtimes = {"node": {}, "deno": {}}
    return {
        "js_runtimes": js_runtimes,
        # Fetch the challenge-solver script from GitHub when required.
        "remote_components": ["ejs:github"],
    }


def base_ydl_opts(use_cookies: bool = True) -> dict:
    """Shared yt-dlp options: quiet logging + autodetected browser cookies + JS runtime."""
    opts: dict = {"quiet": True, "no_warnings": True, **_js_runtime_opts()}
    if use_cookies:
        browser = resolve_cookies_browser()
        if browser:
            opts["cookiesfrombrowser"] = browser
    return opts


def _ffmpeg_location():
    candidates = []
    if getattr(sys, "frozen", False):
        bundled = os.path.join(getattr(sys, "_MEIPASS", ""), "ffmpeg")
        candidates.append(bundled)
    if os.environ.get("YTQ_FFMPEG_DIR"):
        candidates.append(os.environ["YTQ_FFMPEG_DIR"])
    repo_ffmpeg = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "release", "ffmpeg"
    )
    candidates.append(repo_ffmpeg)
    for path in candidates:
        ffmpeg = os.path.join(path, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if os.path.isfile(ffmpeg):
            return path
    return None


def _build_download_opts(
    output_path: str, progress_cb, use_cookies: bool, fmt: str
) -> dict:
    """Build download opts. Tolerant format (any audio incl. opus) -> MP3/192.

    NOTE: no `extractor_args.player_client` override — yt-dlp's upstream
    defaults track YouTube's SABR experiments. Pinning `android` breaks
    downloads when YouTube serves SABR-only streams.
    """
    return {
        **base_ydl_opts(use_cookies=use_cookies),
        "format": fmt,
        "format_sort": ["acodec:opus", "acodec:aac", "acodec:mp3"],
        "prefer_free_formats": False,
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
        "socket_timeout": 30,
    }


def run_ytdl_download(video_id: str, output_path: str, progress_cb):
    """Download+transcode with automatic fallbacks.

    Attempt order (first success wins):
      1. bestaudio/best WITH cookies (authenticated, best quality)
      2. bestaudio/best WITHOUT cookies (cookie jar locked/stale/bot-flagged)
      3. best (any audio+video) WITHOUT cookies (last resort, still -> MP3)
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    attempts = [
        {"use_cookies": True, "fmt": "bestaudio/best"},
        {"use_cookies": False, "fmt": "bestaudio/best"},
        {"use_cookies": False, "fmt": "best"},
    ]
    last_exc: Optional[Exception] = None
    for i, attempt in enumerate(attempts):
        ydl_opts = _build_download_opts(
            output_path, progress_cb, attempt["use_cookies"], attempt["fmt"]
        )
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            return
        except Exception as e:
            last_exc = e
            msg = str(e)
            # Only fall through to the no-cookie fallbacks for cookie/signature/
            # format-availability failures; re-raise hard failures immediately,
            # except allow format fallback (attempt 3) for format errors.
            is_last = i == len(attempts) - 1
            if is_last:
                break
            next_uses_cookies = attempts[i + 1]["use_cookies"]
            if next_uses_cookies != attempt["use_cookies"]:
                # Transitioning cookies -> no cookies: only worth it for
                # signature/bot/format failures.
                if not _should_retry_without_cookies(msg):
                    break
            continue
    assert last_exc is not None
    raise last_exc


async def process_video(job_id: str, video_id: str, job_dir: str, title: Optional[str]):
    async with semaphore:
        if job_cancel_flags.get(job_id):
            return

        track = jobs[job_id]["tracks"][video_id]
        track["status"] = "downloading"
        track["progress"] = 0
        track.pop("error", None)
        await publish_event(job_id, {"type": "track_update", "track": track})

        safe_title = sanitize_filename(title or video_id, video_id)
        output_tmpl = os.path.join(job_dir, f"{video_id}.%(ext)s")
        final_mp3_path = os.path.join(job_dir, f"{safe_title}.mp3")

        loop = asyncio.get_running_loop()

        def progress_cb(d):
            if job_cancel_flags.get(job_id):
                raise yt_dlp.utils.DownloadCancelled("Cancelled by user")
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

            if job_cancel_flags.get(job_id):
                track["status"] = "cancelled"
                return

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
            if job_cancel_flags.get(job_id):
                track["status"] = "cancelled"
                return
            track["status"] = "failed"
            track["error"] = clean_ytdl_error(str(e))

        await publish_event(job_id, {"type": "track_update", "track": track})


async def _finalize_job(job_id: str, all_ids: List[str]):
    """Recompute result_path (single MP3 or ZIP) and mark completed."""
    if len(all_ids) == 1:
        result_path = jobs[job_id]["tracks"][all_ids[0]].get("file_path")
    else:
        job_dir = os.path.join(BASE_DOWNLOAD_DIR, job_id)
        zip_base = os.path.join(BASE_DOWNLOAD_DIR, f"{job_id}_archive")
        # Drop a stale archive so make_archive rebuilds from current job_dir.
        stale = zip_base + ".zip"
        if os.path.exists(stale):
            try:
                os.remove(stale)
            except OSError:
                pass
        loop = asyncio.get_running_loop()
        result_path = await loop.run_in_executor(
            None, shutil.make_archive, zip_base, "zip", job_dir
        )

    jobs[job_id]["result_path"] = result_path
    jobs[job_id]["status"] = "completed"
    jobs[job_id]["completed_at"] = time.time()
    await publish_event(job_id, {"type": "job_status", "status": "completed"})


async def run_job(job_id: str, selected_ids: List[str], titles: Dict[str, str]):
    job_dir = os.path.join(BASE_DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    jobs[job_id]["status"] = "processing"
    jobs[job_id]["completed_at"] = None
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

    await _finalize_job(job_id, selected_ids)


async def run_retry_job(job_id: str, retry_ids: List[str]):
    """Re-process only failed tracks, then rebuild the archive."""
    job = jobs.get(job_id)
    if not job:
        return
    job_dir = os.path.join(BASE_DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    job["status"] = "processing"
    job["completed_at"] = None
    job_cancel_flags[job_id] = False
    await publish_event(job_id, {"type": "job_status", "status": "processing"})

    titles = job.get("titles") or {}
    tasks = [process_video(job_id, vid, job_dir, titles.get(vid)) for vid in retry_ids]
    await asyncio.gather(*tasks)

    if job_cancel_flags.get(job_id):
        job["status"] = "cancelled"
        await publish_event(job_id, {"type": "job_status", "status": "cancelled"})
        return

    await _finalize_job(job_id, job.get("selected_ids") or list(job["tracks"].keys()))


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
        **base_ydl_opts(),
        "extract_flat": True,
        "skip_download": True,
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
        "truncated": False,
        "skipped": skipped,
        "tracks": results,
    }


# --- Endpoints ---
@app.post("/api/playlist/fetch")
def fetch_playlist_metadata(req: FetchRequest):
    if parse_spotify_url(req.url):
        return fetch_spotify_metadata(req.url)

    ydl_opts = {
        **base_ydl_opts(),
        "extract_flat": "in_playlist",
        "skip_download": True,
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

            return {
                "playlist_title": info.get("title", "Untitled Playlist"),
                "total_tracks_in_playlist": len(entries),
                "returned_tracks": len(items),
                "truncated": False,
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

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "queued",
        "tracks": {
            vid: {"video_id": vid, "status": "pending", "progress": 0}
            for vid in req.video_ids
        },
        "titles": dict(req.titles or {}),
        "selected_ids": list(req.video_ids),
        "result_path": None,
        "completed_at": None,
    }
    job_subscribers[job_id] = []
    job_cancel_flags[job_id] = False

    bg_tasks.add_task(run_job, job_id, req.video_ids, req.titles or {})
    return {"job_id": job_id}


@app.post("/api/jobs/{job_id}/retry")
def retry_failed_tracks(job_id: str, req: RetryRequest, bg_tasks: BackgroundTasks):
    """Re-queue failed tracks within the same job (per-track retry)."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") == "processing":
        raise HTTPException(status_code=409, detail="Job is still processing.")

    if req.titles:
        job.setdefault("titles", {}).update(req.titles)

    if req.video_ids:
        retry_ids = [vid for vid in req.video_ids if vid in job["tracks"]]
    else:
        retry_ids = [
            vid for vid, t in job["tracks"].items() if t.get("status") == "failed"
        ]
    if not retry_ids:
        raise HTTPException(status_code=400, detail="No failed tracks to retry.")

    # Reset and re-run only those tracks; archive is rebuilt on completion.
    for vid in retry_ids:
        job["tracks"][vid].update({"status": "pending", "progress": 0})
        job["tracks"][vid].pop("error", None)

    bg_tasks.add_task(run_retry_job, job_id, retry_ids)
    return {"job_id": job_id, "retry_ids": retry_ids}


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

    job["status"] = "cancelled"
    shutil.rmtree(job_dir, ignore_errors=True)
    await publish_event(job_id, {"type": "job_status", "status": "cancelled"})
    return {"status": "cancelled"}


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
