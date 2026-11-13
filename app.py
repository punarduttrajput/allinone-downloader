"""AllinOne - paste an Instagram or YouTube link, download the video.

Backend: Flask + yt-dlp (+ ffmpeg for merging video/audio and MP3 extraction).
Flow:
  POST /api/info      {url}            -> title, thumbnail, available qualities
  POST /api/download  {url, quality}   -> {job_id}  (download runs in background)
  GET  /api/progress/<job_id>          -> status, percent, speed, eta
  GET  /api/file/<job_id>              -> the finished file as an attachment
"""

import os
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

ALLOWED_HOSTS = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "music.youtube.com": "youtube",
    "instagram.com": "instagram",
    "instagr.am": "instagram",
}

PLAYER_CLIENTS = [c.strip() for c in os.environ.get("YT_PLAYER_CLIENTS", "").split(",") if c.strip()]

QUALITIES = ["best", "1080", "720", "480", "360", "audio"]

app = Flask(__name__, static_folder="static")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


# ---------------------------------------------------------------- helpers

def detect_platform(url: str) -> str | None:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    for domain, platform in ALLOWED_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return platform
    return None


def base_opts() -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
    }
    if COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    if shutil.which("node"):
        # Node solves YouTube's JS challenges (yt-dlp only looks for Deno by default).
        opts["js_runtimes"] = {"node": {}}
    if PLAYER_CLIENTS:
        # e.g. "mweb,tv,web_safari" - clients that pair well with PO tokens
        opts["extractor_args"] = {"youtube": {"player_client": PLAYER_CLIENTS}}
    return opts


def format_for(quality: str) -> dict:
    if quality == "audio":
        return {
            "format": "bestaudio/best",
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ],
        }
    if quality == "best":
        fmt = "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b"
    else:
        h = int(quality)
        fmt = (
            f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/"
            f"bv*[height<={h}]+ba/b[height<={h}]/b"
        )
    return {"format": fmt, "merge_output_format": "mp4"}


def friendly_error(exc: Exception) -> str:
    msg = str(exc)
    low = msg.lower()
    if "not a bot" in low:
        return ("YouTube is blocking this server (\"confirm you're not a bot\"). "
                "The site owner needs to add a YouTube cookies.txt to the server.")
    if "login" in low or "cookies" in low or "private" in low:
        return ("This post needs a logged-in account (private or age/login-gated). "
                "Put a cookies.txt export next to app.py and try again.")
    if "unsupported url" in low:
        return "That link isn't a video page we can read."
    if "video unavailable" in low or "not available" in low:
        return "This video is unavailable (removed, private, or region-locked)."
    return msg.replace("ERROR: ", "").strip()[:300] or "Download failed."


def cleanup_old_jobs() -> None:
    now = time.time()
    with jobs_lock:
        stale = [jid for jid, j in jobs.items() if now - j["created"] > JOB_TTL_SECONDS]
        for jid in stale:
            shutil.rmtree(jobs[jid]["dir"], ignore_errors=True)
            del jobs[jid]
    # leftovers from earlier runs
    for d in WORK_ROOT.iterdir():  # cookies.txt is a file, so it's skipped below
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


@app.post("/api/info")
def info():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    platform = detect_platform(url)
    if not platform:
        return jsonify(error="Paste a valid YouTube or Instagram link."), 400

    try:
        with YoutubeDL({**base_opts(), "skip_download": True}) as ydl:
            data = ydl.extract_info(url, download=False)
    except DownloadError as e:
        return jsonify(error=friendly_error(e)), 422

    if data.get("_type") == "playlist" and data.get("entries"):
        # Instagram carousels: use the first video entry
        entries = [e for e in data["entries"] if e]
        data = entries[0] if entries else data

    heights = sorted(
        {f.get("height") for f in data.get("formats", []) if f.get("height") and f.get("vcodec") != "none"},
        reverse=True,
    )
    qualities = ["best"] + [q for q in ("1080", "720", "480", "360") if any(h >= int(q) for h in heights)]
    qualities.append("audio")

    return jsonify(
        platform=platform,
        title=data.get("title") or "Untitled",
        uploader=data.get("uploader") or data.get("channel") or "",
        duration=data.get("duration"),
        thumbnail=data.get("thumbnail"),
        qualities=qualities,
        max_height=heights[0] if heights else None,
    )


@app.post("/api/download")
def start_download():
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    quality = body.get("quality") or "best"
    if not detect_platform(url):
        return jsonify(error="Paste a valid YouTube or Instagram link."), 400
    if quality not in QUALITIES:
        return jsonify(error="Unknown quality."), 400

    cleanup_old_jobs()
    job_id = uuid.uuid4().hex
    job_dir = WORK_ROOT / job_id
    job_dir.mkdir()
    job = {
        "id": job_id, "dir": job_dir, "created": time.time(),
        "status": "queued", "percent": 0.0, "speed": None, "eta": None,
        "file": None, "error": None,
    }
    with jobs_lock:
        jobs[job_id] = job

    threading.Thread(target=run_job, args=(job, url, quality), daemon=True).start()
    return jsonify(job_id=job_id)


def run_job(job: dict, url: str, quality: str) -> None:
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

    opts = {
        **base_opts(),
        **format_for(quality),
        "outtmpl": str(job["dir"] / "%(title).80s [%(id)s].%(ext)s"),
        "progress_hooks": [hook],
    }
    try:
        with YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
        files = [p for p in job["dir"].iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if not files:
            raise DownloadError("No file was produced.")
        job.update(status="done", file=max(files, key=lambda p: p.stat().st_size))
    except Exception as e:  # noqa: BLE001 - report any failure to the UI
        job.update(status="error", error=friendly_error(e))


@app.get("/api/progress/<job_id>")
def progress(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="Unknown job."), 404
    return jsonify(
        status=job["status"], percent=job["percent"], speed=job["speed"],
        eta=job["eta"], error=job["error"],
        filename=job["file"].name if job["file"] else None,
    )


@app.get("/api/file/<job_id>")
def get_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify(error="File not ready."), 404
    return send_file(job["file"], as_attachment=True, download_name=job["file"].name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"AllinOne running on http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
