# AGENT.md — Handoff for any AI agent picking up this repo

> **Read this first.** This file lets a fresh agent operate this project knowing only the
> **repo URL** and a **GitHub token**. No other context is required.

---

## 1. What this project is

A **Telegram video/audio downloader bot** (yt-dlp + ffmpeg) that runs **free on GitHub Actions**.
It long-polls Telegram, exposes an inline-button UI, and restarts itself continuously.

| Item | Value |
|---|---|
| Repo | `sathxum/reclip-telegram-bot` (public) |
| Bot | `@Silencedown_bot` |
| Entry point | `bot.py` |
| Workflow | `.github/workflows/bot.yml` |
| Runtime per run | 340 min (~5h40m), then self-rerun |

---

## 2. Bootstrap: what an agent needs

### Required
- **GitHub token** (`repo` + `workflow` scopes) — for API calls and pushes
- **Repo URL** — `https://github.com/sathxum/reclip-telegram-bot.git`

### Verify the token immediately

```bash
TOKEN='<github_token>'
curl -s -H "Authorization: Bearer $TOKEN" https://api.github.com/user \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['login'])"
curl -sI -H "Authorization: Bearer $TOKEN" https://api.github.com/user \
  | grep -i x-oauth-scopes
```

Expected scopes include `repo` and `workflow`.

---

## 3. Repo layout

```
.
├── bot.py                      # the whole bot (WARP + quick tunnel + UI + engine + admin)
├── requirements.txt            # requests, yt-dlp
├── README.md                   # human setup guide
├── AGENT.md                    # this file
└── .github/workflows/bot.yml   # GitHub Actions runner
```

`bot.py` sections, in order:

| Section | Lines (approx) | Purpose |
|---|---|---|
| Telegram API helpers | top | `api()`, `send_message`, `edit_message`, callbacks |
| WARP relay | mid | `setup_warp`, `warp_watchdog`, `_write_wp_conf`, `_register_profile` |
| Quick tunnel + web server | mid | `setup_tunnel`, `tunnel_watchdog`, `_ShareHandler`, `create_share`, `expiry_loop`, `_tunnel_msg` |
| Stats + admin | mid | `stats_load/save`, `stats_bump`, `set_admin_if_needed`, `is_admin`, `handle_admin` |
| Download engine | mid | `yt_base`, `yt_info`, `run_download`, `fit_video`, `_split`, `send_media` |
| UI | mid | `handle_start/help/about/status/admin`, `main_menu` |
| Fetch + deliver | mid | `fetch_and_prompt`, `deliver` |
| Update loop | bottom | `handle_update`, `janitor_loop`, `main`, `trigger_rerun` |

---

## 4. Secrets used by the workflow

Set via `Settings → Secrets and variables → Actions`, or programmatically (see §6).

| Secret | Required | Purpose |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram bot token |
| `GH_PAT` | ✅ | Lets the bot dispatch its own rerun |
| `WGCF_PROFILE` | ➖ | Optional pre-made WARP WireGuard profile |

---

## 5. How to deploy / redeploy

```bash
git clone https://github.com/sathxum/reclip-telegram-bot.git
cd reclip-telegram-bot
# ...edit files...
git add -A && git commit -m "change"
git push https://<TOKEN>@github.com/sathxum/reclip-telegram-bot.git main
```

Then restart the live bot (a running worker still holds the old code):

```bash
REPO=sathxum/reclip-telegram-bot
# cancel running job
RUN=$(curl -s -H "Authorization: Bearer $TOKEN" \
  "https://api.github.com/repos/$REPO/actions/runs?status=in_progress" \
  | python3 -c "import sys,json;r=json.load(sys.stdin)['workflow_runs'];print(r[0]['id'] if r else '')")
[ -n "$RUN" ] && curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  "https://api.github.com/repos/$REPO/actions/runs/$RUN/cancel"
# dispatch fresh
curl -s -X POST -H "Authorization: Bearer $TOKEN" -H 'Accept: application/vnd.github+json' \
  "https://api.github.com/repos/$REPO/actions/workflows/bot.yml/dispatches" -d '{"ref":"main"}'
```

---

## 6. Setting secrets programmatically

GitHub secrets need **libsodium sealed-box** encryption:

```bash
pip install PyNaCl
```

```python
import base64, json, requests
from nacl import encoding, public

TOKEN, REPO = '<token>', 'sathxum/reclip-telegram-bot'
H = {'Authorization': f'Bearer {TOKEN}', 'Accept': 'application/vnd.github+json'}
key = requests.get(f'https://api.github.com/repos/{REPO}/actions/secrets/public-key', headers=H).json()
box = public.SealedBox(public.PublicKey(key['key'].encode(), encoding.Base64Encoder()))
for name, value in {'BOT_TOKEN': '...', 'GH_PAT': TOKEN}.items():
    enc = base64.b64encode(box.encrypt(value.encode())).decode()
    r = requests.put(f'https://api.github.com/repos/{REPO}/actions/secrets/{name}',
                     headers=H, data=json.dumps({'encrypted_value': enc, 'key_id': key['key_id']}))
    print(name, r.status_code)
```

---

## 7. Verify a deployment

```bash
REPO=sathxum/reclip-telegram-bot
# latest run
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://api.github.com/repos/$REPO/actions/runs?per_page=1" | python3 -c "
import sys,json
r=json.load(sys.stdin)['workflow_runs'][0]
print(r['status'], r['conclusion'], r['event'])"
# bot identity
curl -s "https://api.telegram.org/bot<BOT_TOKEN>/getMe" | python3 -m json.tool | head -5
```

Success = run `in_progress` with step **"Run bot"** running, and `getMe` returns `Silencedown_bot`.

---

## 8. Environment variables (bot.py)

| Var | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — | required |
| `GH_PAT` | — | rerun dispatch |
| `GH_REPO` | — | set by workflow from `github.repository` |
| `WORKFLOW_FILE` | `bot.yml` | workflow to dispatch |
| `RUNTIME_MINUTES` | `340` | worker lifetime |
| `FILE_TTL_MIN` | `20` | downloaded-file auto-delete window (janitor) |
| `MAX_PARALLEL_JOBS` | `3` | concurrent download workers |
| `WARP_ENABLE` | `1` | bring up Cloudflare WARP relay (fixes YouTube) |
| `WARP_PROXY_PORT` | `25344` | local SOCKS5 port for the WARP relay |
| `WGCF_PROFILE` | — | optional pre-made WARP WireGuard profile |
| `TUNNEL_ENABLE` | `1` | bring up the Cloudflare quick tunnel for big files |
| `TUNNEL_TTL_MIN` | `10` | how long a full-quality link lives before auto-wipe |
| `TUNNEL_PORT` | `8791` | local port the private share server binds (127.0.0.1) |
| `ADMIN_ID` | — | fixed admin Telegram user id (optional; else auto-detected) |

---

## 9. Behaviour notes / gotchas

- **YouTube is fixed by Cloudflare WARP.** Datacenter IPs get "Sign in to confirm you're not a
  bot". The bot starts a userspace WireGuard (wireproxy + wgcf) SOCKS5 relay on boot and routes
  yt-dlp through it. Verified working: **YouTube 1080p, 4K and MP3 — no cookies needed.**
- **WARP fallback chain.** On failure the bot tries 9 WARP endpoints, then re-registers a
  fresh account and retries. If WARP is entirely unavailable it falls back to alternate
  player clients and direct connections. A watchdog re-checks every 45 s.
- **Full-quality delivery via Cloudflare quick tunnel.** Files under 50 MB are sent straight
  into the chat. Anything larger (long/1-hour videos, 4K, big MP3s) is served over a
  `*.trycloudflare.com` quick tunnel as a private play+download page — no compression, original
  quality, any size. `deliver()` only falls back to `fit_video`/`_split` compression if the
  tunnel is down.
- **The share server is locked down.** It binds `127.0.0.1` (only reachable through the tunnel),
  serves a single unlisted random URL per file, and all other paths return a generic private
  notice or 404. Response headers forbid indexing/caching; there is **no directory listing, no
  file browser, and no shell** — only an inline `<video>`/`<audio>` element plus a Download
  button. Do not expose anything else on it.
- **Links self-destruct (10 min).** `expiry_loop()` removes the share entry and securely wipes
  the file from disk after `TUNNEL_TTL_MIN`; the janitor does the same for leftovers. Nothing
  survives the run.
- **Admin auto-detection.** The first Telegram user to interact is recorded as admin and shown a
  `🛡 Admin` button + `/admin` panel (uptime, boots, downloads, tunnels created, active links,
  WARP egress, wipe window). Set a fixed `ADMIN_ID` env to lock this down.
- **Stats persist across fresh VMs** via a `reclip-state` git branch (`state.json`). Tiny text
  only; loaded on boot, saved every 3 min and at exit.
- **Only one worker may run at once** — Telegram allows a single `getUpdates` consumer.
  The workflow uses a `concurrency` group to enforce this.
- **Files are ephemeral by design.** A janitor thread wipes downloads after `FILE_TTL_MIN`;
  each Actions run also gets a fresh VM, so nothing survives a restart.
- **GitHub ToS:** long-running Telegram bots on Actions are a grey area. The self-rerun plus a
  watchdog cron (`17 */6 * * *`) keeps it resilient; if the workflow gets disabled, re-enable it
  with `PUT .../actions/workflows/bot.yml/enable`.
- **Never print or log `BOT_TOKEN` or cookies.** Keep them in secrets only.

---

## 10. Quick task recipes

| Task | Where |
|---|---|
| Change bot text / UI | `handle_*` functions in `bot.py` |
| Add a supported site | nothing — yt-dlp handles it |
| Change quality list | `quality_options()` |
| Change file lifetime | workflow `env:` or `FILE_TTL_MIN` in `bot.py` |
| Change run length | `RUNTIME_MINUTES` + workflow `timeout-minutes` |
| Change link expiry | `TUNNEL_TTL_MIN` (workflow `env:`) |
| Change who is admin | set `ADMIN_ID` env, or delete `state.json` on `reclip-state` and re-seed |
| Change big-file page look | `_share_html()` |
| Change admin panel text | `handle_admin()` |
| Add a new command | `handle_update()` message branch + a `handle_*` function |
