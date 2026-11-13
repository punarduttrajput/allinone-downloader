# AllinOne Downloader

Paste a YouTube or Instagram link, pick a quality (Best / 1080p / 720p / 480p / 360p / MP3), and download.

Built with Flask + [yt-dlp](https://github.com/yt-dlp/yt-dlp). Needs **ffmpeg** on PATH (for merging HD video+audio and MP3 conversion).

## Run

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py
```

Open http://127.0.0.1:5000

## Instagram login-only posts

Private posts and many reels need a logged-in session. Export your Instagram cookies
(Netscape `cookies.txt` format, e.g. with a "Get cookies.txt" browser extension) and save the file as
`cookies.txt` next to `app.py`. It's picked up automatically. Keep that file private.

## When YouTube downloads start failing

Sites change often. Update yt-dlp:

```bash
.venv\Scripts\python -m pip install -U yt-dlp
```

Only download content you own or have permission to save.
