"""AllinOne - paste an Instagram or YouTube link, download the video.

Backend: Flask + yt-dlp (+ ffmpeg for merging video/audio and MP3 extraction).
Flow:
  POST /api/info      {url}            -> title, thumbnail, quality options with sizes
  POST /api/download  {url, quality}   -> {job_id}  (download runs in background)
  GET  /api/progress/<job_id>          -> status, percent, speed, eta
  GET  /api/file/<job_id>              -> the finished file as an attachment
"""

import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_file, send_from_directory
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

BASE_DIR = Path(__file__).resolve().parent
WORK_ROOT = Path(tempfile.gettempdir()) / "allinone_downloads"
WORK_ROOT.mkdir(exist_ok=True)

# Optional cookies.txt for login-gated posts and YouTube's "not a bot" check.
# Local: next to app.py. Render: a Secret File named cookies.txt (mounted read-only
# at /etc/secrets), copied to a writable spot because yt-dlp updates the file.
COOKIES_FILE = WORK_ROOT / "cookies.txt"
for _src in (Path("/etc/secrets/cookies.txt"), BASE_DIR / "cookies.txt"):
    if _src.exists():
        shutil.copyfile(_src, COOKIES_FILE)
        break

JOB_TTL_SECONDS = 30 * 60
INFO_TTL_SECONDS = 20 * 60
MP3_KBPS = 192

ALLOWED_HOSTS = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "instagram.com": "instagram",
    "instagr.am": "instagram",
}
LINK_RE = re.compile(
    r"(?:https?://)?(?:[\w-]+\.)*(?:youtube\.com|youtu\.be|instagram\.com|instagr\.am)/\S*",
    re.IGNORECASE,
)

PLAYER_CLIENTS = [c.strip() for c in os.environ.get("YT_PLAYER_CLIENTS", "").split(",") if c.strip()]
# When YouTube answers "confirm you're not a bot", try again posing as other clients.
RETRY_CLIENT_SETS = [["tv"], ["web_safari", "mweb"]]

STANDARD_HEIGHTS = (1080, 720, 480, 360)
QUALITY_IDS = {"best", "audio", *map(str, STANDARD_HEIGHTS)}

app = Flask(__name__, static_folder="static")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
info_cache: dict[str, dict] = {}  # url -> {"at", "info", "clients"}


# ---------------------------------------------------------------- links

def normalize_url(raw: str) -> str | None:
    """Pull the first YouTube/Instagram link out of whatever was pasted and add https://."""
    m = LINK_RE.search((raw or "").strip())
    if not m:
        return None
    url = m.group(0).rstrip(").,;!?\"'")
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    return url


def detect_platform(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    for domain, platform in ALLOWED_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return platform
    return None


# ---------------------------------------------------------------- yt-dlp options

def base_opts(clients: list[str] | None = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "socket_timeout": 20,
    }
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    if shutil.which("node"):
        # Node solves YouTube's JS challenges (yt-dlp only looks for Deno by default).
        opts["js_runtimes"] = {"node": {}}
    clients = clients or PLAYER_CLIENTS
    if clients:
        opts["extractor_args"] = {"youtube": {"player_client": clients}}
    return opts


def video_selector(height: int | None) -> str:
    """Prefer H.264 + AAC in MP4: it plays on every phone, PC and TV."""
    h = f"[height<={height}]" if height else ""
    return (
        f"bv*{h}[vcodec^=avc]+ba[ext=m4a]/"
        f"bv*{h}[ext=mp4]+ba[ext=m4a]/"
        f"b{h}[ext=mp4]/bv*{h}+ba/b{h}/b"
    )


def format_for(quality: str) -> dict:
    if quality == "audio":
        return {
            "format": "bestaudio/best",
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": str(MP3_KBPS)}
            ],
        }
    height = None if quality == "best" else int(quality)
    return {"format": video_selector(height), "merge_output_format": "mp4"}


# ---------------------------------------------------------------- errors

def classify_error(exc: Exception) -> dict:
    raw = str(exc)
    low = raw.lower()
    if "not a bot" in low:
        return dict(code="blocked", retryable=True,
                    error="YouTube didn't let us through this time. This happens now and then. "
                          "Try again in a minute, or try a different video.")
    if "private" in low or "login" in low or "log in" in low or "sign in" in low or "cookies" in low:
        return dict(code="private", retryable=False,
                    error="This post is private or needs a login, so it can't be downloaded.")
    if "unsupported url" in low or "no video" in low:
        return dict(code="unsupported", retryable=False,
                    error="That link doesn't point to a video. Copy the link of a specific video, Short or reel.")
    if "unavailable" in low or "not available" in low or "removed" in low:
        return dict(code="unavailable", retryable=False,
                    error="This video is unavailable. It may have been removed, made private, or blocked in this region.")
    if "timed out" in low or "timeout" in low or "connection" in low or "network" in low:
        return dict(code="network", retryable=True,
                    error="Couldn't reach the site just now. Please try again.")
    print(f"unclassified error: {raw[:500]}", flush=True)
    return dict(code="failed", retryable=True,
                error="Something went wrong reading this video. Try again, or try a different link.")


# ---------------------------------------------------------------- extraction

def extract(url: str, platform: str, ydl_extra: dict | None = None, download: bool = False,
            first_clients: list[str] | None = None):
    """extract_info with automatic retries using other YouTube clients on bot checks.
    Returns (info, clients_used)."""
    attempts = [None] + (RETRY_CLIENT_SETS if platform == "youtube" else [])
    if first_clients in attempts:  # try the set that worked last time first
        attempts.remove(first_clients)
        attempts.insert(0, first_clients)
    last_exc = None
    for clients in attempts:
        try:
            with YoutubeDL({**base_opts(clients), **(ydl_extra or {})}) as ydl:
                info = ydl.extract_info(url, download=download)
                return ydl.sanitize_info(info), clients
        except DownloadError as e:
            last_exc = e
            if "not a bot" not in str(e).lower():
                break
            print(f"bot check with clients={clients or PLAYER_CLIENTS or 'default'}; retrying", flush=True)
    raise last_exc


def first_entry(info: dict) -> dict:
    """Instagram carousels come back as playlists: use the first video."""
    if info.get("_type") == "playlist" and info.get("entries"):
        entries = [e for e in info["entries"] if e]
        if entries:
            return entries[0]
    return info


def fmt_size(f: dict, duration: float | None) -> float | None:
    size = f.get("filesize") or f.get("filesize_approx")
    if not size and f.get("tbr") and duration:
        size = f["tbr"] * 1000 / 8 * duration
    return size


def estimate_options(info: dict) -> list[dict]:
    formats = info.get("formats") or []
    duration = info.get("duration")
    videos = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("height")]
    audios = [f for f in formats if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]

    def pick_audio():
        m4a = [a for a in audios if a.get("ext") == "m4a"] or audios
        return max(m4a, key=lambda a: a.get("abr") or a.get("tbr") or 0, default=None)

    def pick_video(limit: int | None):
        pool = [v for v in videos if limit is None or v["height"] <= limit]
        for pref in (lambda v: (v.get("vcodec") or "").startswith("avc"), lambda v: v.get("ext") == "mp4", lambda v: True):
            cands = [v for v in pool if pref(v)]
            if cands:
                return max(cands, key=lambda v: (v["height"], v.get("tbr") or 0))
        return None

    def total(video) -> float | None:
        if not video:
            return None
        size = fmt_size(video, duration)
        if video.get("acodec") in (None, "none"):  # video-only stream: audio is added
            audio = pick_audio()
            a = fmt_size(audio, duration) if audio else None
            size = size + a if size and a else size
        return size

    best = pick_video(None)
    best_h = best["height"] if best else None
    options = [{
        "id": "best",
        "label": f"Best · {best_h}p" if best_h else "Best",
        "note": "MP4",
        "size": total(best),
    }]
    for h in STANDARD_HEIGHTS:
        if best_h and h < best_h and any(v["height"] >= h for v in videos):
            options.append({"id": str(h), "label": f"{h}p", "note": "MP4", "size": total(pick_video(h))})
    options.append({
        "id": "audio",
        "label": "MP3 audio",
        "note": f"{MP3_KBPS} kbps",
        "size": duration * MP3_KBPS * 1000 / 8 if duration else None,
    })
    return options


def cached_info(url: str) -> dict | None:
    entry = info_cache.get(url)
    if entry and time.time() - entry["at"] < INFO_TTL_SECONDS:
        return entry
    return None


def cleanup_old() -> None:
    now = time.time()
    for u in [u for u, e in info_cache.items() if now - e["at"] > INFO_TTL_SECONDS]:
        info_cache.pop(u, None)
    with jobs_lock:
        stale = [jid for jid, j in jobs.items() if now - j["created"] > JOB_TTL_SECONDS]
        for jid in stale:
            shutil.rmtree(jobs[jid]["dir"], ignore_errors=True)
            del jobs[jid]
    # leftovers from earlier runs (cookies.txt is a file, so it's skipped)
    for d in WORK_ROOT.iterdir():
        if d.is_dir() and now - d.stat().st_mtime > JOB_TTL_SECONDS and d.name not in jobs:
            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- routes

@app.get("/healthz")
def healthz():
    return jsonify(status="ok", uptime_seconds=int(time.time() - STARTED_AT), active_jobs=len(jobs))


def keep_alive() -> None:
    """Render's free plan sleeps after ~15 min without inbound traffic.
    Pinging our own public URL every 10 min counts as traffic and keeps it awake."""
    import urllib.request

    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/") + "/healthz"
    interval = int(os.environ.get("KEEPALIVE_SECONDS", "600"))
    while True:
        time.sleep(interval)
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                print(f"keep-alive ping {r.status}", flush=True)
        except Exception as e:  # noqa: BLE001 - never let the pinger die
            print(f"keep-alive ping failed: {e}", flush=True)


STARTED_AT = time.time()
if os.environ.get("RENDER_EXTERNAL_URL") and os.environ.get("KEEPALIVE", "1") != "0":
    threading.Thread(target=keep_alive, daemon=True).start()


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


INVALID_LINK = dict(code="invalid", retryable=False,
                    error="That doesn't look like a YouTube or Instagram link. "
                          "Copy the link from the Share button and paste it here.")


@app.post("/api/info")
def info():
    url = normalize_url((request.get_json(silent=True) or {}).get("url", ""))
    platform = detect_platform(url) if url else None
    if not platform:
        return jsonify(INVALID_LINK), 400

    cleanup_old()
    entry = cached_info(url)
    if not entry:
        try:
            data, clients = extract(url, platform, {"skip_download": True})
        except DownloadError as e:
            return jsonify(classify_error(e)), 422
        entry = {"at": time.time(), "info": first_entry(data), "clients": clients}
        info_cache[url] = entry

    data = entry["info"]
    return jsonify(
        url=url,
        platform=platform,
        id=data.get("id"),
        title=data.get("title") or "Untitled",
        uploader=data.get("uploader") or data.get("channel") or "",
        duration=data.get("duration"),
        thumbnail=data.get("thumbnail"),
        options=estimate_options(data),
    )


@app.post("/api/download")
def start_download():
    body = request.get_json(silent=True) or {}
    url = normalize_url(body.get("url") or "")
    quality = str(body.get("quality") or "best")
    platform = detect_platform(url) if url else None
    if not platform:
        return jsonify(INVALID_LINK), 400
    if quality not in QUALITY_IDS:
        return jsonify(error="Unknown quality.", code="invalid", retryable=False), 400

    cleanup_old()
    job_id = uuid.uuid4().hex
    job_dir = WORK_ROOT / job_id
    job_dir.mkdir()
    job = {
        "id": job_id, "dir": job_dir, "created": time.time(),
        "status": "queued", "percent": 0.0, "speed": None, "eta": None,
        "file": None, "error": None, "code": None, "retryable": False,
    }
    with jobs_lock:
        jobs[job_id] = job

    threading.Thread(target=run_job, args=(job, url, platform, quality), daemon=True).start()
    return jsonify(job_id=job_id)


def run_job(job: dict, url: str, platform: str, quality: str) -> None:
    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            job.update(
                status="downloading",
                percent=round(done / total * 100, 1) if total else job["percent"],
                speed=d.get("speed"),
                eta=d.get("eta"),
            )
        elif d["status"] == "finished":
            job.update(status="processing", percent=100.0)

    extra = {
        **format_for(quality),
        "outtmpl": str(job["dir"] / "%(title).80s [%(id)s].%(ext)s"),
        "progress_hooks": [hook],
    }
    try:
        # Start with the client set that worked for /api/info, if any.
        # (Reusing the fetched info itself doesn't work: YouTube's stream links are
        # tied to the session that fetched them and return 403 when reused.)
        entry = cached_info(url)
        extract(url, platform, extra, download=True, first_clients=entry["clients"] if entry else None)

        files = [p for p in job["dir"].iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if not files:
            raise DownloadError("No file was produced.")
        job.update(status="done", file=max(files, key=lambda p: p.stat().st_size))
    except Exception as e:  # noqa: BLE001 - report any failure to the UI
        job.update(status="error", **classify_error(e))


@app.get("/api/progress/<job_id>")
def progress(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="This download has expired. Please start it again.", code="expired", retryable=True), 404
    return jsonify(
        status=job["status"], percent=job["percent"], speed=job["speed"], eta=job["eta"],
        error=job["error"], code=job["code"], retryable=job["retryable"],
        filename=job["file"].name if job["file"] else None,
        size=job["file"].stat().st_size if job["file"] else None,
    )


@app.get("/api/file/<job_id>")
def get_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify(error="This file has expired. Please download it again."), 404
    return send_file(job["file"], as_attachment=True, download_name=job["file"].name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"AllinOne running on http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
