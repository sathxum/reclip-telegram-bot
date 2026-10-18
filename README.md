# ReClip Telegram Bot 🤖

A self-hosted Telegram bot that downloads video/audio from **1000+ sites** (TikTok, Instagram, Facebook, Twitter/X, YouTube, Reddit, Vimeo, Dailymotion and more) powered by `yt-dlp` + `ffmpeg`.

Runs **free on GitHub Actions** — no server needed. Auto-reruns itself to stay online ~24/7.

## Features

- Paste any link → get MP4 or MP3 back in chat
- Quality picker (up to 8 resolutions) + one-tap MP3
- Thumbnail preview with inline buttons
- Bulk links (up to 5 per message)
- Auto-compress to fit Telegram's 50 MB bot limit
- Progress updates while downloading
- Self-restarting: each run lasts ~5h40m, then dispatches itself again
- Watchdog cron every 6h in case a self-restart fails

## Setup

The repo already ships a ready workflow at `.github/workflows/bot.yml`.

### 1. Create the public repo

```bash
git init -b main
git add .
git commit -m "ReClip Telegram bot"
git remote add origin https://github.com/<YOU>/reclip-telegram-bot.git
git push -u origin main
```

### 2. Add repository secrets

`Settings → Secrets and variables → Actions → New repository secret`

| Secret | Required | Purpose |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram bot token from @BotFather |
| `GH_PAT` | ✅ | Fine-grained PAT with `Actions: Read and write` so the bot can self-rerun |
| `YT_COOKIES` | ➖ | Netscape cookies.txt content — needed for YouTube/Instagram on datacenter IPs |

### 3. Enable Actions

`Actions` tab → allow workflows. The bot starts on the next cron tick, or press **Run workflow**.

## Important: datacenter IP limits

GitHub Actions runners use datacenter IPs. Sites like **YouTube, Instagram, Reddit, Vimeo, Pinterest** may ask for login / block the request.

Options:
1. **Add `YT_COOKIES`** — export cookies from a logged-in browser with a cookie extension, paste the file contents as the secret.
2. Accept that those sites need cookies while others (TikTok, FB, X, Dailymotion, Streamable…) work out of the box.

## Runtime & uptime

- `RUNTIME_MINUTES=340` (~5h40m), just under GitHub's 6h job cap
- On exit the bot POSTs a `workflow_dispatch` to restart itself
- `concurrency` group prevents two bots fighting over `getUpdates`
- The `schedule` cron is a safety net

## Local test

```bash
pip install -r requirements.txt
export BOT_TOKEN=...
python bot.py
```

## License

MIT — based on [ReClip](https://github.com/averygan/reclip).
