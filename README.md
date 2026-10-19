# ReClip Telegram Bot 🤖

A Telegram bot that downloads video/audio from **1000+ sites** (YouTube, TikTok, Instagram,
Facebook, Twitter/X, Reddit, Vimeo, Dailymotion and more) powered by `yt-dlp` + `ffmpeg`.

Runs **free on GitHub Actions** — no server needed, auto-restarts to stay online.

## Features

- Paste any link → get MP4 or MP3 back in chat
- Quality picker (up to 8 resolutions) + one-tap 🎵 MP3
- Thumbnail preview with inline buttons
- Animated progress bar while downloading
- Bulk links (up to 5 per message)
- 💎 **Full quality, any size** — heavy / 1-hour videos are served over a private
  Cloudflare quick-tunnel page you can **play or download** (no 50 MB compression)
- 🔒 Links are unlisted, single-file and **self-destruct after 10 min** (file wiped from disk)
- 🛡 Auto-detected **admin panel** (uptime, boots, downloads, tunnels, WARP egress)
- 📊 Stats persist across fresh VMs via a tiny `reclip-state` branch
- Auto-compress to fit Telegram's 50 MB bot limit (fallback if the tunnel is down)
- 🌐 **Cloudflare WARP relay** — fixes YouTube's datacenter IP block with **no cookies**
- 🧱 **Multi-layer fallback** — 9 WARP endpoints, account re-registration, player-client rotations
- 🔒 **Auto-delete** — downloaded files are wiped after 20 min
- 📊 Status panel (uptime)
- Self-restarting worker + watchdog cron

## How YouTube works without cookies

Datacenter IPs (including GitHub runners) are blocked by YouTube with
*"Sign in to confirm you're not a bot"*. Cookies used to be the only fix — but they rotate and
break constantly.

Instead the bot brings up a **Cloudflare WARP tunnel** in userspace:

1. `wgcf` registers a free WARP account
2. `wireproxy` creates a userspace WireGuard tunnel (no root, no TUN device)
3. yt-dlp runs through a local SOCKS5 relay with a clean egress IP

If a tunnel fails, the bot automatically tries **9 different WARP endpoints**, then
**re-registers a fresh account** and tries again. If WARP is entirely unavailable, downloads
fall back to alternate player clients and then to direct connections. A watchdog re-checks the
proxy every 45 seconds and restarts it on failure.

Verified working: **YouTube 1080p, 4K and MP3 — no cookies needed.**

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
| `WGCF_PROFILE` | ➖ | Optional pre-made WARP profile |

### 3. Enable Actions

Actions tab → allow workflows → **Run workflow** (or wait for the cron).

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `RUNTIME_MINUTES` | `340` | worker lifetime before self-rerun |
| `FILE_TTL_MIN` | `10` | file auto-delete window |
| `WARP_ENABLE` | `1` | bring up the Cloudflare WARP relay |
| `WARP_PROXY_PORT` | `25344` | local SOCKS5 port for the relay |
| `TUNNEL_ENABLE` | `1` | enable the quick tunnel for big files |
| `TUNNEL_TTL_MIN` | `10` | how long a full-quality link lives before auto-wipe |
| `ADMIN_ID` | — | fixed admin user id (optional; else auto-detected) |
| `WGCF_PROFILE` | — | optional pre-made WARP WireGuard profile |

Agent handoff instructions live in **[AGENT.md](AGENT.md)**.

## Local test

```bash
pip install -r requirements.txt
export BOT_TOKEN=...
python bot.py
```

## License

MIT — based on [ReClip](https://github.com/averygan/reclip).
