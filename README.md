# ReClip Telegram Bot 🤖

A Telegram bot that downloads video/audio from **1000+ sites** (TikTok, Instagram, Facebook,
Twitter/X, YouTube, Reddit, Vimeo, Dailymotion and more) powered by `yt-dlp` + `ffmpeg`.

Runs **free on GitHub Actions** — no server needed, auto-restarts to stay online.

## Features

- Paste any link → get MP4 or MP3 back in chat
- Quality picker (up to 8 resolutions) + one-tap 🎵 MP3
- Thumbnail preview with inline buttons
- Animated progress bar while downloading
- Bulk links (up to 5 per message)
- Auto-compress to fit Telegram's 50 MB bot limit
- 🍪 **Cookie support with auto-format detection** — Netscape, JSON, raw string, or base64
- 🔒 **Auto-delete everything** — cookies wiped after 30 min, files after 10 min
- 📊 Status panel (uptime, cookies, active jobs)
- Self-restarting worker + watchdog cron

## Setup

### 1. Push to a repo

```bash
git init -b main && git add . && git commit -m "ReClip bot"
git remote add origin https://github.com/<YOU>/reclip-telegram-bot.git
git push -u origin main
```

### 2. Add secrets

`Settings → Secrets and variables → Actions`

| Secret | Required | Purpose |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram bot token |
| `GH_PAT` | ✅ | PAT with `Actions: write` for self-rerun |
| `YT_COOKIES` | ➖ | Optional cookie seed (any format) |

### 3. Enable Actions

Actions tab → allow workflows → **Run workflow** (or wait for the cron).

## Sites & cookies

Runners use datacenter IPs. **TikTok, Facebook, X, Dailymotion, Streamable** work directly.
**YouTube, Instagram, Reddit, Vimeo, Pinterest** need cookies — the bot detects this and asks
in chat. Send cookies in **any** format and it converts them automatically.

🔒 Cookies are kept only for `COOKIE_TTL_MIN` (default 30) minutes, then wiped. Files are
deleted after `FILE_TTL_MIN` (default 10) minutes. Each Actions run also gets a fresh VM.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `RUNTIME_MINUTES` | `340` | worker lifetime before self-rerun |
| `COOKIE_TTL_MIN` | `30` | cookie auto-delete window |
| `FILE_TTL_MIN` | `10` | file auto-delete window |
| `YT_COOKIES` | — | optional cookie seed |

Agent handoff instructions live in **[AGENT.md](AGENT.md)**.

## Local test

```bash
pip install -r requirements.txt
export BOT_TOKEN=...
python bot.py
```

## License

MIT — based on [ReClip](https://github.com/averygan/reclip).
