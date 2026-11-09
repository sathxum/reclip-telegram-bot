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
├── admin_server.py             # separate authenticated terminal/studio listener + tunnel
├── admin_ui.html               # self-contained mobile admin UI, no external assets
├── tests/test_admin.py         # local HTTP/security/ffmpeg/bot regression tests
├── requirements.txt            # requests, yt-dlp, PySocks
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
| `ADMIN_ID` | — | positive fixed Telegram user id; required for terminal/studio (workflow reads repository variable `ADMIN_ID`); legacy stats admin may still auto-detect |
| `ADMIN_PORT` | `8792` | separate loopback authenticated admin HTTP listener; never use `TUNNEL_PORT` |

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


---

## 11. Authenticated admin terminal and Video Studio (local implementation)

### Privilege and routing — do not weaken these boundaries

- Configure a **positive, explicit numeric `ADMIN_ID`** repository **variable** (not a
  changed secret). The Ubuntu workflow passes `${{ vars.ADMIN_ID }}`. Local operators
  can use the environment variable. Missing/invalid/non-positive IDs disable these tools;
  first-user auto-admin remains only for the legacy stats panel, **never shell/studio**.
- `/cmd` and video-delivery choice require both Telegram `from.id == ADMIN_ID` and
  `chat.id == ADMIN_ID` (private admin chat). Do not send shell capabilities into groups.
- General users retain small-file Telegram delivery, large-file anonymous unlisted
  sharing and existing fallback compression. Fixed admin video selection adds **Telegram
  video** versus **URL · Video Studio**, including small videos. Admin Telegram selection
  uses compression/splitting for large videos instead of silently switching to a URL.
  MP3 behavior remains unchanged.
- **Do not add execution/authenticated APIs to `_ShareHandler`.** It still exposes only
  the public single-file share page/media. `admin_server.py` starts a separate
  `127.0.0.1:8792` listener and separate HTTPS Cloudflare quick tunnel on demand.
  `ADMIN_PORT` must differ from `TUNNEL_PORT`; tunnel failure produces a retry message,
  not an anonymous shell or studio fallback. Public share/server code is unchanged.
- Launch links contain a 256-bit random token in the **URL fragment**, removed immediately
  by the self-contained page before exchange. Single-use, 90-second launch; scoped
  sessions last at most 10 minutes or the runner deadline. Capabilities remain memory-only.
  Never log launch/session/CSRF tokens or load third-party fonts/scripts/analytics.
- Each tab uses its own `__Host-reclip_admin_<sid>` Secure/HttpOnly/SameSite=Strict cookie.
  `sid` is only a **non-secret selector**, retained in same-origin URLs for media/reload;
  it cannot authenticate without its matching cookie. Every API/media/edit-download
  checks session, fixed admin, expiry and scope. All POSTs check exact Origin/Host;
  authenticated POSTs additionally require in-memory CSRF. CSP hashes the bundled
  script/style; no external assets, cross-origin calls, framing, indexing or caching.

### Console and media behavior

- Runner specs are read from actual `/etc/os-release`, `/proc` and filesystem data:
  never label a machine Ubuntu merely because the workflow requests `ubuntu-latest`.
- Real non-interactive `/bin/bash --noprofile --norc -c` as the **bot OS user**; multiline
  input, Ctrl/Cmd+Enter, history, stderr and exit status. Commands do not keep shell
  variables or interactive PTYs; use explicit directory controls for persistent cwd.
- Console is **trusted-admin full remote execution, not a sandbox**. It does not inherit
  BOT_TOKEN/GH_PAT/etc into subprocess environment, but a trusted admin can still read
  runner files/processes, use sudo where allowed, and deliberately escape process groups.
  Do not run untrusted commands or print credentials. Process-group kills handle normal
  background children, not deliberate `setsid` escapes; the fresh runner teardown is the
  final lifetime boundary. Hard SIGKILL cannot trigger Python cleanup.
- Limits: one command/render globally; 30-second commands, 64 KiB captured raw output,
  4096-character commands, 16 HTTP connection slots, 10-second absolute header/body reads,
  8 active launch/session entries, 3 renders per studio. Responses close connections.
  These are local bounds, **not DDoS protection** for a publicly discoverable tunnel.
- Studio previews the source, accepts 1–8 ordered/non-overlapping segments in seconds or
  hh:mm:ss, and optionally an even-pixel x/y/width/height crop. Max edited length 600s;
  ffmpeg transcodes accurate trim/concat/crop to H.264/AAC MP4 (lossy, not stream-copy),
  max 180 seconds render time, 256 MiB output, 2 encoding/filter threads.
- Browser-native playback depends on original source codec; an unsupported preview does
  not prevent editing. Crop is disabled for rotated/non-square-pixel sources to avoid
  misleading coordinates; trimming normalizes rotation/aspect. No visual drag-crop UI.
- Only edited clips have a download control/attachment endpoint. **Preview bytes cannot
  be made unsaveable** to the authenticated admin; native download controls are suppressed,
  not DRM. Each delivery uses a unique source filename. Sources/edited clips are deleted
  on launch expiry, session expiry/logout, tunnel replacement or shutdown; no persistence.
  Deletion is filesystem unlinking, not a forensic secure-erase guarantee.
- Admin server has a deadline reaper, atexit cleanup and graceful SIGTERM handling.
  Shell/session/cwd state never survives a worker restart. Cloudflare URL changes require
  fresh launch links. The workflow still runs `ubuntu-latest`, Python 3.11 with ffmpeg.

### Local verification (no bot, Telegram, GitHub APIs or tunnel needed)

```bash
python -m unittest discover -s tests -v
python -m py_compile bot.py admin_server.py tests/test_admin.py
```

Tests use stdlib unittest/HTTP, installed requests and local ffmpeg/ffprobe synthetic
fixtures; the dummy import token is never used for Telegram. Cover anonymous denial,
fixed admin/sender/job ownership, launch replay/expiry, cookie tab isolation, origin/CSRF,
scoped media routes, bounded real shell, cleanup, edit validation and real trim/concat/crop
output. Tests do **not** dispatch workflows, mutate secrets or deploy changes. Live
Cloudflare/Telegram and Ubuntu-runner smoke checks must be done separately by an operator.
