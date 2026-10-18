#!/usr/bin/env python3
"""ReClip Telegram Bot - a fast, always-on video/audio downloader bot.

- Handles 1000+ sites via yt-dlp + ffmpeg
- Cloudflare WARP relay (userspace WireGuard) fixes YouTube's datacenter IP block
- Multiple endpoints, automatic account re-registration, continuous watchdog
- Downloaded files are auto-deleted after a short TTL (fresh VM each run)
- Animated progress and polished inline-button UI
"""
import os
import re
import json
import time
import glob
import random
import string
import subprocess
import sys
import tarfile
import threading
from datetime import datetime

import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
GH_PAT = os.environ.get("GH_PAT", "").strip()
GH_REPO = os.environ.get("GH_REPO", "").strip()
WORKFLOW_FILE = os.environ.get("WORKFLOW_FILE", "bot.yml")
RUNTIME_MINUTES = int(os.environ.get("RUNTIME_MINUTES", "340"))
FILE_TTL = int(os.environ.get("FILE_TTL_MIN", "10")) * 60

if not BOT_TOKEN:
    print("FATAL: BOT_TOKEN is not set")
    sys.exit(1)

API = "https://api.telegram.org/bot" + BOT_TOKEN
WORKDIR = "/tmp/reclip"
DL = os.path.join(WORKDIR, "downloads")
os.makedirs(DL, exist_ok=True)

TG_LIMIT = 50 * 1024 * 1024
START = time.time()
DEADLINE = START + RUNTIME_MINUTES * 60
URL_RE = re.compile(r"https?://\S+")
JOBS = {}

BLOCK_PAT = re.compile(
    "not a bot|sign in to confirm|login required|private video|rate.?limit|"
    "http error 403|403 forbidden|only available|account is|age.?restricted|"
    "this video is unavailable|blocked|consent|confirm your age|payment required",
    re.I,
)


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def api(method, data=None, files=None, timeout=90):
    url = API + "/" + method
    for _ in range(3):
        try:
            if files:
                r = requests.post(url, data=data, files=files, timeout=timeout)
            else:
                r = requests.post(url, json=data, timeout=timeout)
            j = r.json()
            if not j.get("ok"):
                log("api!", method, str(j.get("description"))[:140])
            return j
        except Exception as exc:
            log("api-exc", method, exc)
            time.sleep(2)
    return {"ok": False}


def send_message(chat_id, text, markup=None, disable_preview=True):
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview}
    if markup:
        data["reply_markup"] = json.dumps(markup)
    return api("sendMessage", data)


def edit_message(chat_id, message_id, text, markup=None):
    if not message_id:
        return {"ok": False}
    data = {"chat_id": chat_id, "message_id": message_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True}
    if markup is not None:
        data["reply_markup"] = json.dumps(markup)
    return api("editMessageText", data)


def chat_action(chat_id, action="upload_video"):
    api("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=15)


def answer_cb(cb_id, text=""):
    api("answerCallbackQuery", {"callback_query_id": cb_id, "text": text}, timeout=15)


def esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{int(n)}B" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def fmt_dur(s):
    try:
        s = int(s or 0)
    except Exception:
        return "?"
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return (f"{h}h " if h else "") + f"{m}m {sec}s"


def bar(pct, width=12):
    pct = max(0.0, min(100.0, float(pct or 0)))
    filled = int(round(width * pct / 100))
    return "▰" * filled + "▱" * (width - filled) + f"  {pct:.0f}%"


# ------------------------------------------------------------------- warp ---
# Cloudflare WARP gives a clean egress IP that fixes YouTube's datacenter
# block ("Sign in to confirm you're not a bot") without any cookies.
# Userspace WireGuard (wireproxy) => no root, no TUN device needed.
#
# Fallback chain for maximum reliability:
#   1. profile from env seed (WGCF_PROFILE) if provided
#   2. existing generated profile
#   3. freshly registered WARP account
#   Each profile is tried against every known WARP endpoint until one tunnel
#   comes up. A watchdog keeps re-checking and restarts on failure.

WARP_ENABLED = os.environ.get("WARP_ENABLE", "1").strip().lower() not in ("0", "false", "no")
WARP_DIR = "/tmp/warp"
PROXY_PORT = int(os.environ.get("WARP_PROXY_PORT", "25344"))
PROXY_URL = "socks5h://127.0.0.1:" + str(PROXY_PORT)
WGCF_PROFILE_ENV = os.environ.get("WGCF_PROFILE", "").strip()
PROXY_STATE = {"up": False, "ip": "", "endpoint": "", "proc": None, "fair": 0}

WGCF_URL = ("https://github.com/ViRb3/wgcf/releases/download/v2.2.29/"
            "wgcf_2.2.29_linux_amd64")
WIREPROXY_URL = ("https://github.com/pufferffish/wireproxy/releases/download/"
                 "v1.0.9/wireproxy_linux_amd64.tar.gz")

ENDPOINTS = [
    "engage.cloudflareclient.com:2408",
    "162.159.192.1:2408",
    "162.159.195.1:2408",
    "engage.cloudflareclient.com:500",
    "162.159.193.10:2408",
    "188.114.96.1:2408",
    "188.114.97.1:2408",
    "engage.cloudflareclient.com:1701",
    "engage.cloudflareclient.com:4500",
]


def _http_get(url, timeout=120):
    for _ in range(2):
        try:
            r = requests.get(url, timeout=timeout, stream=True)
            if r.status_code == 200:
                return r
        except Exception as exc:
            log("get err", url[:60], exc)
            time.sleep(2)
    return None


def _download(url, dest):
    r = _http_get(url)
    if not r:
        return False
    try:
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(65536):
                if chunk:
                    fh.write(chunk)
        return os.path.getsize(dest) > 0
    except Exception as exc:
        log("save err", dest, exc)
        return False


def _parse_wg_conf(text):
    sect = None
    out = {"Interface": {}, "Peer": {}}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            sect = line[1:-1]
            continue
        if sect in out and "=" in line:
            k, v = line.split("=", 1)
            out[sect][k.strip()] = v.strip()
    return out


def _proxy_check():
    try:
        r = requests.get("https://www.cloudflare.com/cdn-cgi/trace",
                         proxies={"https": PROXY_URL, "http": PROXY_URL}, timeout=12)
        if r.status_code == 200:
            for line in r.text.splitlines():
                if line.startswith("ip="):
                    PROXY_STATE["ip"] = line[3:].strip()
            return True
    except Exception:
        pass
    return False


def _ensure_binaries():
    os.makedirs(WARP_DIR, exist_ok=True)
    wgcf = os.path.join(WARP_DIR, "wgcf")
    wp = os.path.join(WARP_DIR, "wireproxy")
    if not os.path.isfile(wgcf):
        if not _download(WGCF_URL, wgcf):
            return None, None
        os.chmod(wgcf, 0o755)
    if not os.path.isfile(wp):
        tgz = os.path.join(WARP_DIR, "wp.tgz")
        if not _download(WIREPROXY_URL, tgz):
            return None, None
        try:
            with tarfile.open(tgz) as tf:
                for m in tf.getmembers():
                    if m.name.endswith("wireproxy") and m.isfile():
                        m.name = "wireproxy"
                        tf.extract(m, WARP_DIR)
        except Exception as exc:
            log("untar err", exc)
            return None, None
        if not os.path.isfile(wp):
            return None, None
        os.chmod(wp, 0o755)
    return wgcf, wp


def _register_profile(wgcf):
    conf_path = os.path.join(WARP_DIR, "wgcf-profile.conf")
    try:
        subprocess.run([wgcf, "register", "--accept-tos"], cwd=WARP_DIR,
                       capture_output=True, text=True, timeout=150)
        subprocess.run([wgcf, "generate"], cwd=WARP_DIR,
                       capture_output=True, text=True, timeout=150)
    except Exception as exc:
        log("wgcf err", exc)
    return conf_path if os.path.isfile(conf_path) else None


def _profile_path(wgcf):
    conf_path = os.path.join(WARP_DIR, "wgcf-profile.conf")
    if WGCF_PROFILE_ENV:
        try:
            with open(conf_path, "w") as fh:
                fh.write(WGCF_PROFILE_ENV)
            return conf_path
        except Exception as exc:
            log("profile write err", exc)
    if os.path.isfile(conf_path):
        return conf_path
    return _register_profile(wgcf)


def _write_wp_conf(profile_path, endpoint):
    try:
        raw = open(profile_path).read()
    except Exception:
        return None
    d = _parse_wg_conf(raw)
    iface = d.get("Interface") or {}
    peer = d.get("Peer") or {}
    if not iface.get("PrivateKey") or not peer.get("PublicKey"):
        return None
    addr = "172.16.0.2/32"
    for part in (iface.get("Address") or "").split(","):
        part = part.strip()
        if "/" in part and ":" not in part:
            addr = part
            break
    lines = [
        "[Interface]",
        "PrivateKey = " + iface["PrivateKey"],
        "Address = " + addr,
        "DNS = 1.1.1.1",
        "MTU = " + (iface.get("MTU") or "1280"),
        "",
        "[Peer]",
        "PublicKey = " + peer["PublicKey"],
    ]
    if peer.get("PresharedKey"):
        lines.append("PresharedKey = " + peer["PresharedKey"])
    lines += [
        "AllowedIPs = 0.0.0.0/0",
        "Endpoint = " + endpoint,
        "PersistentKeepalive = 25",
        "",
        "[Socks5]",
        "BindAddress = 127.0.0.1:" + str(PROXY_PORT),
        "",
    ]
    path = os.path.join(WARP_DIR, "wireproxy.conf")
    with open(path, "w") as fh:
        fh.write("\n".join(lines))
    return path


def _kill_proxy():
    try:
        old = PROXY_STATE.get("proc")
        if old and old.poll() is None:
            old.terminate()
            time.sleep(1)
            if old.poll() is None:
                old.kill()
    except Exception:
        pass


def _start_wireproxy(wp, conf_path):
    _kill_proxy()
    try:
        logf = open(os.path.join(WARP_DIR, "wireproxy.log"), "ab")
        proc = subprocess.Popen([wp, "-c", conf_path], stdout=logf,
                                stderr=subprocess.STDOUT)
        PROXY_STATE["proc"] = proc
    except Exception as exc:
        log("wireproxy start err", exc)
        return False
    for _ in range(9):
        time.sleep(2)
        if _proxy_check():
            return True
    return False


def setup_warp():
    """Bring up the WARP SOCKS proxy with endpoint + account fallbacks."""
    if not WARP_ENABLED:
        log("warp: disabled")
        return False
    wgcf, wp = _ensure_binaries()
    if not wgcf or not wp:
        log("warp: binaries unavailable")
        return False

    profiles = []
    p = _profile_path(wgcf)
    if p:
        profiles.append(p)

    for attempt in range(2):
        for profile in list(profiles):
            for endpoint in ENDPOINTS:
                conf = _write_wp_conf(profile, endpoint)
                if not conf:
                    continue
                if _start_wireproxy(wp, conf):
                    PROXY_STATE.update({"up": True, "endpoint": endpoint})
                    log("warp: up via", endpoint, "| egress", PROXY_STATE["ip"])
                    return True
                log("warp: endpoint failed:", endpoint)
        if attempt == 0:
            log("warp: re-registering a fresh account")
            fresh = _register_profile(wgcf)
            if fresh and fresh not in profiles:
                profiles.append(fresh)

    PROXY_STATE["up"] = False
    log("warp: all attempts failed")
    return False


def warp_watchdog():
    if not WARP_ENABLED:
        return
    if not PROXY_STATE["up"]:
        setup_warp()
    stall = 0
    while time.time() < DEADLINE + 120:
        time.sleep(45)
        if PROXY_STATE["up"] and _proxy_check():
            stall = 0
            continue
        PROXY_STATE["up"] = False
        stall += 1
        log("warp: proxy down (strike", stall, ") - recovering")
        if setup_warp():
            stall = 0


# ---------------------------------------------------------------- engine ----

def base_cmd(extra=None):
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--socket-timeout", "25",
           "--retries", "5", "--fragment-retries", "10", "--no-cache-dir"]
    if PROXY_STATE.get("up"):
        cmd += ["--proxy", PROXY_URL]
    if extra:
        cmd += extra
    return cmd


def yt_info(url):
    tries = []
    if PROXY_STATE.get("up"):
        tries.append(["--proxy", PROXY_URL])
    tries.append([])
    for extra in tries:
        try:
            res = subprocess.run(
                ["yt-dlp", "--no-playlist", "--no-warnings", "--socket-timeout", "25",
                 "--no-cache-dir"] + extra + ["-j", url],
                capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            continue
        for line in res.stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line), None
                except Exception:
                    continue
    return None, "unavailable"


def quality_options(info):
    heights = set()
    for f in info.get("formats") or []:
        h = f.get("height")
        if f.get("vcodec") and f.get("vcodec") != "none" and h:
            heights.add(int(h))
    if not heights:
        return []
    labels = []
    for h in sorted(heights, reverse=True):
        name = {2160: "4K", 1440: "2K", 1080: "Full HD", 720: "HD",
                480: "SD", 360: "Low"}.get(h, f"{h}p")
        labels.append((h, f"{name} ({h}p)"))
    return labels


def _run_once(cmd, out_base, on_progress):
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
    except Exception as exc:
        return None, str(exc)
    last = ""
    t0 = time.time()
    if proc.stdout:
        for raw in proc.stdout:
            raw = raw.rstrip()
            m = re.search(r"\[download\]\s+([\d.]+)%", raw)
            if m and on_progress and time.time() - t0 > 3:
                t0 = time.time()
                try:
                    on_progress(float(m.group(1)))
                except Exception:
                    pass
            if raw.strip():
                last = raw
    proc.wait()
    if proc.returncode != 0:
        return None, last or "download failed"
    files = [f for f in glob.glob(out_base + ".*") if os.path.isfile(f)]
    if not files:
        return None, "no output file"
    files.sort(key=lambda p: os.path.getsize(p), reverse=True)
    chosen = files[0]
    for f in files:
        if f != chosen:
            try:
                os.remove(f)
            except OSError:
                pass
    return chosen, None


def run_download(url, opts, out_base, on_progress=None):
    """Download with a fallback chain: WARP -> alternate client -> direct."""
    attempts = []
    if PROXY_STATE.get("up"):
        attempts.append({"proxy": True, "client": None})
        attempts.append({"proxy": True, "client": "default,tv,web"})
    attempts.append({"proxy": False, "client": None})
    attempts.append({"proxy": False, "client": "default,tv,web"})

    last_err = ""
    for at in attempts:
        cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--socket-timeout", "25",
               "--retries", "5", "--fragment-retries", "10", "--no-cache-dir"]
        if at["proxy"] and PROXY_STATE.get("up"):
            cmd += ["--proxy", PROXY_URL]
        if at["client"]:
            cmd += ["--extractor-args", "youtube:player_client=" + at["client"]]
        cmd += opts + ["-o", out_base + ".%(ext)s", url]
        path, err = _run_once(cmd, out_base, on_progress)
        if path:
            return path, None
        last_err = err
        for f in glob.glob(out_base + ".*"):
            try:
                os.remove(f)
            except OSError:
                pass
    return None, last_err


def compress_to_fit(path, duration):
    out = path.rsplit(".", 1)[0] + "_c.mp4"
    dur = max(float(duration or 0), 1)
    target_bits = (44 * 1024 * 1024) * 8
    vbit = max(int(target_bits / dur) - 96000, 100000)
    cmd = ["ffmpeg", "-y", "-i", path, "-c:v", "libx264", "-preset", "veryfast",
           "-b:v", str(vbit), "-maxrate", str(int(vbit * 1.3)),
           "-bufsize", str(vbit * 2), "-c:a", "aac", "-b:a", "96k", out]
    try:
        subprocess.run(cmd, capture_output=True, timeout=1800)
    except Exception:
        return None
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        return out
    return None


def send_media(chat_id, path, kind, caption):
    size = os.path.getsize(path)
    if size > TG_LIMIT:
        return False, size
    if kind == "audio":
        method, field = "sendAudio", "audio"
    else:
        method, field = "sendVideo", "video"
    with open(path, "rb") as fh:
        files = {field: (os.path.basename(path), fh, "application/octet-stream")}
        data = {"chat_id": chat_id, "caption": caption[:1000], "parse_mode": "HTML"}
        if kind == "video":
            data["supports_streaming"] = "true"
        r = api(method, data, files=files, timeout=1800)
    return bool(r.get("ok")), size


# -------------------------------------------------------------------- UI ----

def main_menu():
    return {"inline_keyboard": [
        [{"text": "🎞 How to use", "callback_data": "help"},
         {"text": "ℹ️ About", "callback_data": "about"}],
        [{"text": "📊 Status", "callback_data": "status"}],
    ]}


def handle_start(chat_id, name):
    text = (
        f"👋 <b>Hello {esc(name)}!</b> ✨\n"
        "━━━━━━━━━━━━━━━\n"
        "I'm <b>ReClip</b> — your personal downloader 🎬🎵\n\n"
        "🔗 Paste <b>any video link</b> and I will–\n"
        "• 📥 download it for you\n"
        "• 🎚 let you choose the quality\n"
        "• 🚀 send it right here in this chat\n\n"
        "<i>Works with YouTube, TikTok, Instagram, Facebook, X,\n"
        "Reddit, Vimeo and 1000+ more sites.</i>"
    )
    send_message(chat_id, text, main_menu())


def handle_help(chat_id, message_id=None):
    text = (
        "📖 <b>How to use ReClip</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        "1️⃣  Send me one or more links 🔗\n"
        "2️⃣  I show the video info + thumbnail\n"
        "3️⃣  Pick a <b>quality</b> — or tap 🎵 <b>MP3</b>\n"
        "4️⃣  Your file arrives in a few seconds 🚀\n\n"
        "✨ <b>Nice to know</b>\n"
        "• Up to <b>5 links</b> per message\n"
        "• Big files are auto-shrunk to fit Telegram\n"
        "• Tap ❌ <b>Cancel</b> anytime\n"
        "• 🔒 Downloaded files are <b>auto-deleted</b>, nothing is kept"
    )
    markup = {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "back"}]]}
    if message_id:
        edit_message(chat_id, message_id, text, markup)
    else:
        send_message(chat_id, text, main_menu())


def handle_about(chat_id, message_id=None):
    text = (
        "ℹ️ <b>About ReClip</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        "A fast, always-on downloader powered by <b>yt-dlp</b> + <b>ffmpeg</b>.\n"
        "Supports <b>1000+</b> sites for video and audio.\n\n"
        "🔒 <b>Privacy first</b>\n"
        "• Downloaded files are auto-deleted\n"
        "• Nothing is stored permanently\n\n"
        "<i>Please respect copyright — only download content you have the right to.</i>"
    )
    markup = {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "back"}]]}
    if message_id:
        edit_message(chat_id, message_id, text, markup)
    else:
        send_message(chat_id, text, main_menu())


def handle_status(chat_id, message_id=None):
    text = (
        "📊 <b>Uptime</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"🟢 <b>{fmt_dur(time.time() - START)}</b>"
    )
    markup = {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "back"}]]}
    if message_id:
        edit_message(chat_id, message_id, text, markup)
    else:
        send_message(chat_id, text, main_menu())


# ---------------------------------------------------------------- fetching --

def animate_fetch(chat_id, mid, stop):
    frames = ["🔍", "🧩", "⚙️", "🧠", "🛰"]
    i = 0
    while not stop.is_set() and time.time() < DEADLINE:
        try:
            edit_message(chat_id, mid, frames[i % len(frames)] + " <b>Fetching video info…</b>")
        except Exception:
            pass
        i += 1
        if stop.wait(1.4):
            break


def fetch_and_prompt(chat_id, url):
    msg = send_message(chat_id, "🔍 <b>Fetching video info…</b>")
    mid = (msg.get("result") or {}).get("message_id")
    stop = threading.Event()
    if mid:
        threading.Thread(target=animate_fetch, args=(chat_id, mid, stop), daemon=True).start()
    info, err = yt_info(url)
    stop.set()

    if not info:
        edit_message(chat_id, mid,
                     "❌ <b>Couldn't read that link.</b>\n"
                     "The site may be blocked, private or unsupported.\n\n"
                     "<code>" + esc(url[:180]) + "</code>",
                     {"inline_keyboard": [[{"text": "🔁 Try another", "callback_data": "back"}]]})
        return

    title = info.get("title") or "Untitled"
    dur = fmt_dur(info.get("duration"))
    uploader = info.get("uploader") or info.get("channel") or info.get("uploader_id") or ""
    jid = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    JOBS[jid] = {"chat_id": chat_id, "url": url, "info": info, "msg_id": mid}

    rows = []
    for h, label in quality_options(info)[:8]:
        rows.append([{"text": f"🎬 {label}", "callback_data": f"v:{jid}:{h}"}])
    rows.append([{"text": "🎵 MP3 (Audio)", "callback_data": f"a:{jid}"}])
    rows.append([{"text": "❌ Cancel", "callback_data": f"x:{jid}"}])

    cap = f"🎞 <b>{esc(title)[:200]}</b>\n"
    if uploader:
        cap += f"👤 {esc(uploader)[:80]}\n"
    cap += f"⏱ {dur}\n\n✨ <b>Choose a format below</b> 👇"
    markup = {"inline_keyboard": rows}

    thumb = info.get("thumbnail")
    if thumb:
        try:
            r = requests.get(thumb, timeout=20)
            if r.status_code == 200 and r.content:
                data = {"chat_id": chat_id, "caption": cap, "parse_mode": "HTML",
                        "reply_markup": json.dumps(markup)}
                res = api("sendPhoto", data,
                          files={"photo": ("thumb.jpg", r.content, "image/jpeg")}, timeout=60)
                if res.get("ok"):
                    if mid:
                        api("deleteMessage", {"chat_id": chat_id, "message_id": mid})
                    return
        except Exception:
            pass
    edit_message(chat_id, mid, cap, markup)


def deliver(chat_id, jid, mode, height=None, cb_id=None):
    job = JOBS.get(jid)
    if not job:
        if cb_id:
            answer_cb(cb_id, "This request expired, send the link again.")
        return
    url = job["url"]
    info = job["info"]
    title = info.get("title") or "video"
    dur = info.get("duration")
    mid = job.get("msg_id")
    safe = "".join(c for c in title if c not in '\\/:*?"<>|').strip()[:80] or "video"
    out_base = os.path.join(DL, f"{jid}_{safe}")

    if mode == "audio":
        opts = ["-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "0"]
        kind = "audio"
    else:
        sel = (f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
               if height else "bestvideo+bestaudio/best")
        opts = ["-f", sel, "--merge-output-format", "mp4"]
        kind = "video"

    if cb_id:
        answer_cb(cb_id, "🚀 Download started…")
    if mid:
        edit_message(chat_id, mid,
                     f"🚀 <b>Starting download…</b>\n🎞 {esc(title)[:120]}\n\n▱▱▱▱▱▱▱▱▱▱▱▱  0%",
                     {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": f"x:{jid}"}]]})
    chat_action(chat_id, "upload_video" if kind == "video" else "upload_voice")

    counter = {"n": 0}

    def on_prog(pct):
        counter["n"] += 1
        if counter["n"] % 2 == 0 and mid:
            edit_message(chat_id, mid,
                         f"⬇️ <b>Downloading…</b>\n{bar(pct)}\n🎞 {esc(title)[:100]}",
                         {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": f"x:{jid}"}]]})

    path, err = run_download(url, opts, out_base, on_prog)
    if not path:
        if mid:
            edit_message(chat_id, mid,
                         "❌ <b>Download failed.</b>\n<code>" + esc(str(err)[:180]) + "</code>",
                         {"inline_keyboard": [[{"text": "🔁 Retry", "callback_data": f"v:{jid}:0"}]]})
        return

    size = os.path.getsize(path)
    if size > TG_LIMIT and kind == "video":
        if mid:
            edit_message(chat_id, mid, "🛠 <b>File is large — compressing to fit Telegram…</b> ⏳", None)
        comp = compress_to_fit(path, dur)
        if comp:
            try:
                os.remove(path)
            except OSError:
                pass
            path = comp
            size = os.path.getsize(path)

    if size > TG_LIMIT:
        if mid:
            edit_message(chat_id, mid,
                         f"⚠️ <b>File is {fmt_size(size)}</b> — over Telegram's 50 MB bot limit "
                         "and couldn't be shrunk.\n\nTry a lower quality or MP3 🎵",
                         {"inline_keyboard": [[{"text": "🎵 Get MP3", "callback_data": f"a:{jid}"}]]})
        try:
            os.remove(path)
        except OSError:
            pass
        return

    if mid:
        edit_message(chat_id, mid, f"⬆️ <b>Uploading {fmt_size(size)}…</b>\n🎞 {esc(title)[:120]}", None)
    cap = f"🎞 <b>{esc(title)[:200]}</b>\n⏱ {fmt_dur(dur)} • {fmt_size(size)}"
    ok, size = send_media(chat_id, path, kind, cap)
    if ok:
        if mid:
            try:
                api("deleteMessage", {"chat_id": chat_id, "message_id": mid})
            except Exception:
                pass
    else:
        if mid:
            edit_message(chat_id, mid, "❌ <b>Upload failed</b> — file may be too large.", None)
    try:
        os.remove(path)
    except OSError:
        pass


# ------------------------------------------------------------------- loop ---

def handle_update(upd):
    if "message" in upd:
        m = upd["message"]
        chat = m.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        name = chat.get("first_name") or chat.get("title") or "there"
        text = m.get("text") or m.get("caption") or ""

        if text.startswith("/start"):
            handle_start(chat_id, name)
            return
        if text.startswith("/help"):
            handle_help(chat_id)
            return
        if text.startswith("/about"):
            handle_about(chat_id)
            return
        if text.startswith("/status"):
            handle_status(chat_id)
            return

        urls = [u.rstrip(".,);]") for u in URL_RE.findall(text)]
        if urls:
            seen = []
            for u in urls[:5]:
                if u not in seen:
                    seen.append(u)
            for u in seen:
                fetch_and_prompt(chat_id, u)
            return

        if text:
            send_message(chat_id, "🔗 <b>Send me a video link</b> and I'll download it for you! 🎬",
                         main_menu())
        return

    if "callback_query" in upd:
        cb = upd["callback_query"]
        cb_id = cb.get("id")
        data = cb.get("data") or ""
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        mid = msg.get("message_id")
        if data == "help":
            answer_cb(cb_id)
            handle_help(chat_id, mid)
        elif data == "about":
            answer_cb(cb_id)
            handle_about(chat_id, mid)
        elif data == "status":
            answer_cb(cb_id)
            handle_status(chat_id, mid)
        elif data == "back":
            answer_cb(cb_id)
            edit_message(chat_id, mid, "👋 <b>Send me a link to get started!</b> 🔗", main_menu())
        elif data.startswith("v:"):
            parts = data.split(":")
            height = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() and int(parts[2]) > 0 else None
            deliver(chat_id, parts[1], "video", height, cb_id)
        elif data.startswith("a:"):
            deliver(chat_id, data.split(":", 1)[1], "audio", None, cb_id)
        elif data.startswith("x:"):
            answer_cb(cb_id, "Cancelled")
            if mid:
                edit_message(chat_id, mid, "❌ <b>Cancelled.</b>", None)
        return


# -------------------------------------------------------------- janitors ----

def janitor_loop():
    while time.time() < DEADLINE + 120:
        time.sleep(45)
        now = time.time()
        try:
            for f in glob.glob(os.path.join(DL, "*")):
                try:
                    if os.path.isfile(f) and (now - os.path.getmtime(f)) > FILE_TTL:
                        os.remove(f)
                except OSError:
                    pass
        except Exception as exc:
            log("janitor exc", exc)


# ------------------------------------------------------------------ main ----

def trigger_rerun():
    if not GH_PAT or not GH_REPO or "/" not in GH_REPO:
        log("rerun skipped (no GH_PAT/GH_REPO)")
        return
    url = ("https://api.github.com/repos/" + GH_REPO +
           "/actions/workflows/" + WORKFLOW_FILE + "/dispatches")
    headers = {"Authorization": "Bearer " + GH_PAT, "Accept": "application/vnd.github+json"}
    try:
        r = requests.post(url, json={"ref": "main"}, headers=headers, timeout=30)
        log("rerun status", r.status_code)
    except Exception as exc:
        log("rerun exc", exc)


def main():
    log("bot starting | runtime", RUNTIME_MINUTES, "min | warp:", WARP_ENABLED)
    threading.Thread(target=janitor_loop, daemon=True).start()
    ok = setup_warp()
    if WARP_ENABLED:
        threading.Thread(target=warp_watchdog, daemon=True).start()
    log("warp ready:", ok, "| egress:", PROXY_STATE.get("ip") or "direct")
    me = api("getMe")
    log("getMe:", (me.get("result") or {}).get("username"))
    try:
        boot = api("getUpdates", {"offset": -1, "timeout": 0}, timeout=20)
        res = boot.get("result") or []
        offset = (res[-1]["update_id"] + 1) if res else 0
    except Exception:
        offset = 0
    while time.time() < DEADLINE:
        try:
            r = api("getUpdates", {"offset": offset, "timeout": 30,
                                   "allowed_updates": ["message", "callback_query"]}, timeout=60)
            for upd in r.get("result") or []:
                offset = upd["update_id"] + 1
                try:
                    handle_update(upd)
                except Exception as exc:
                    log("handle exc", exc)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            log("loop exc", exc)
            time.sleep(3)
    log("runtime budget reached, exiting")
    _kill_proxy()
    trigger_rerun()


if __name__ == "__main__":
    main()
