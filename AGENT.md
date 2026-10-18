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
├── bot.py                      # the whole bot (bot logic + UI + cookies + janitor)
├── requirements.txt            # requests, yt-dlp
├── README.md                   # human setup guide
├── AGENT.md                    # this file
└── .github/workflows/bot.yml   # GitHub Actions runner
```

`bot.py` sections, in order:

| Section | Lines (approx) | Purpose |
|---|---|---|
| Telegram API helpers | top | `api()`, `send_message`, `edit_message`, callbacks |
| Cookie engine | mid | `to_netscape` (any-format converter), `looks_like_cookies`, `save_cookies`, `purge_cookies` |
| Download engine | mid | `base_cmd`, `yt_info`, `run_download`, `compress_to_fit`, `send_media` |
| UI | mid | `handle_start/help/about/cookies/status`, `main_menu` |
| Fetch + deliver | mid | `fetch_and_prompt`, `deliver`, `is_blocking` |
| Update loop | bottom | `handle_update`, `janitor_loop`, `main`, `trigger_rerun` |

---

## 4. Secrets used by the workflow

Set via `Settings → Secrets and variables → Actions`, or programmatically (see §6).

| Secret | Required | Purpose |
|---|---|---|
| `BOT_TOKEN` | ✅ | Telegram bot token |
| `GH_PAT` | ✅ | Lets the bot dispatch its own rerun |
| `YT_COOKIES` | ➖ | Optional seed cookies (any format) |

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
| `COOKIE_TTL_MIN` | `30` | cookie auto-delete window |
| `FILE_TTL_MIN` | `10` | downloaded-file auto-delete window |
| `YT_COOKIES` | — | optional cookie seed (any format) |

---

## 9. Behaviour notes / gotchas

- **YouTube & Instagram are IP-blocked on datacenter runners.** All player-client, PO-token,
  JS-runtime and IPv4 tricks were tried and failed. **Only cookies work.** The bot detects the
  block and asks the user for cookies automatically.
- **Only one worker may run at once** — Telegram allows a single `getUpdates` consumer.
  The workflow uses a `concurrency` group to enforce this.
- **Cookies & files are ephemeral by design.** A janitor thread wipes cookies after
  `COOKIE_TTL_MIN` and files after `FILE_TTL_MIN`; shutdown purges cookies too. Each
  Actions run gets a fresh VM, so nothing survives a restart anyway.
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
| Change cookie/file lifetime | workflow `env:` or defaults in `bot.py` |
| Change run length | `RUNTIME_MINUTES` + workflow `timeout-minutes` |
| Add a new command | `handle_update()` message branch + a `handle_*` function |
