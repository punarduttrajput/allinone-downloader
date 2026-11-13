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

## Deployed on Render

Live at https://allinone-downloader.onrender.com (Docker, free plan; it sleeps when idle, so the first
visit after a while takes ~1 minute). Pushes to `main` do **not** redeploy automatically (Render has no
GitHub connection); click **Manual Deploy → Deploy latest commit** in the Render dashboard.

The container also runs the [bgutil PO-token provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
on `127.0.0.1:4416` (see `start.sh`), and Node is used as yt-dlp's JS runtime. Render env var
`YT_PLAYER_CLIENTS=mweb,tv,web_safari` picks the YouTube clients. Tested 2026-11-13: this alone got
only ~2 of 7 videos past YouTube's bot check on Render's servers.

YouTube blocks cloud servers with "Sign in to confirm you're not a bot". To unblock it, export
YouTube cookies (Netscape format, ideally from a spare Google account) and add them in the Render
dashboard: **allinone-downloader → Environment → Secret Files → Add**, filename `cookies.txt`.
Save, then redeploy. The app picks it up from `/etc/secrets/cookies.txt`. Cookies expire, so repeat
this when the error comes back.

## When YouTube downloads start failing

Sites change often. Update yt-dlp:

```bash
.venv\Scripts\python -m pip install -U yt-dlp
```

Only download content you own or have permission to save.
