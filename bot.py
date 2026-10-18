#!/usr/bin/env python3
"""ReClip Telegram Bot - a self-hosted video/audio downloader bot.

Runs as a long-polling worker on GitHub Actions. Handles 1000+ sites via yt-dlp.
"""
import os
import re
import json
import time
import glob
import random
import string
import shutil
import subprocess
import sys
from datetime import datetime

import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
GH_PAT = os.environ.get("GH_PAT", "").strip()
GH_REPO = os.environ.get("GH_REPO", "").strip()
WORKFLOW_FILE = os.environ.get("WORKFLOW_FILE", "bot.yml")
RUNTIME_MINUTES = int(os.environ.get("RUNTIME_MINUTES", "340"))

if not BOT_TOKEN:
    print("FATAL: BOT_TOKEN is not set")
    sys.exit(1)

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
WORKDIR = "/tmp/reclip"
DL = os.path.join(WORKDIR, "downloads")
os.makedirs(DL, exist_ok=True)
COOKIES = os.path.join(WORKDIR, "cookies.txt")
HAS_COOKIES = False
_ck = os.environ.get("YT_COOKIES", "").strip()
if _ck:
    try:
        with open(COOKIES, "w") as fh:
            fh.write(_ck.replace("\\n", "\n"))
        HAS_COOKIES = True
    except Exception as exc:  # pragma: no cover
        print("cookie write failed:", exc)

TG_LIMIT = 50 * 1024 * 1024
START = time.time()
DEADLINE = START + RUNTIME_MINUTES * 60
URL_RE = re.compile(r"https?://[^\s<>\"']+")
JOBS = {}


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def api(method, data=None, files=None, timeout=90):
    url = f"{API}/{method}"
    for _ in range(3):
        try:
            if files:
                r = requests.post(url, data=data, files=files, timeout=timeout)
            else:
                r = requests.post(url, json=data, timeout=timeout)
            j = r.json()
            if not j.get("ok"):
                log("api!", method, str(j.get("description"))[:120])
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
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
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


def base_cmd(extra=None):
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--socket-timeout", "20"]
    if HAS_COOKIES:
        cmd += ["--cookies", COOKIES]
    if extra:
        cmd += extra
    return cmd


def yt_info(url):
    try:
        res = subprocess.run(base_cmd(["-j"]) + [url], capture_output=True,
                             text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return None
    if res.returncode != 0 and not res.stdout.strip():
        log("info-fail", (res.stderr or "").strip().splitlines()[-1:] or "")
        return None
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except Exception:
                continue
    return None


def quality_options(info):
    heights = set()
    for f in info.get("formats") or []:
        h = f.get("height")
        if f.get("vcodec") and f.get("vcodec") != "none" and h:
            heights.add(int(h))
    if not heights:
        return []
    ordered = sorted(heights, reverse=True)
    labels = []
    for h in ordered:
        name = {2160: "4K", 1440: "2K", 1080: "Full HD", 720: "HD", 480: "SD", 360: "Low"}.get(h, f"{h}p")
        labels.append((h, f"{name} ({h}p)"))
    return labels


def progress_reader(proc, on_line):
    for raw in proc.stdout:
        on_line(raw.rstrip("\n"))


def run_download(url, opts, out_base, on_progress=None):
    """Run yt-dlp download. Returns (path, error)."""
    cmd = base_cmd(opts + ["-o", out_base + ".%(ext)s", url])
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
            if m and on_progress and time.time() - t0 > 4:
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


def compress_to_fit(path, duration):
    """Shrink a media file under the Telegram upload limit using ffmpeg."""
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
    ext = os.path.splitext(path)[1].lower()
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


def main_menu():
    return {"inline_keyboard": [[
        {"text": "🎞 How to use", "callback_data": "help"},
        {"text": "ℹ️ About", "callback_data": "about"},
    ]]}


def handle_start(chat_id, name):
    text = (
        f"👋 <b>Welcome {esc(name)}!</b>\n\n"
        "I'm <b>ReClip Downloader</b> — paste any video link and I'll–\n"
        "• download it for you in MP4 or MP3\n"
        "• let you pick the quality\n"
        "• send it straight to this chat\n\n"
        "<b>Just send me a link 🔗</b>\n"
        "Works with TikTok, Instagram, Facebook, Twitter/X, YouTube, Reddit, "
        "Vimeo and 1000+ more sites."
    )
    send_message(chat_id, text, main_menu())


def handle_help(chat_id, message_id=None):
    text = (
        "📖 <b>How to use ReClip</b>\n\n"
        "1️⃣ Send me one or more video links\n"
        "2️⃣ I fetch the video info + thumbnail\n"
        "3️⃣ Pick a <b>quality</b> or tap <b>MP3</b> for audio\n"
        "4️⃣ The file is delivered right here\n\n"
        "<b>Tips</b>\n"
        "• Multiple links in one message are supported\n"
        "• Files over 50 MB are auto-compressed to fit\n"
        "• Tap ❌ Cancel anytime to abort"
    )
    if message_id:
        edit_message(chat_id, message_id, text, {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "back"}]]})
    else:
        send_message(chat_id, text, main_menu())


def handle_about(chat_id, message_id=None):
    text = (
        "ℹ️ <b>About ReClip Bot</b>\n\n"
        "Powering downloads with <b>yt-dlp</b> + <b>ffmpeg</b>.\n"
        "Hosted on <b>GitHub Actions</b> — free, always-on, auto-restarting.\n\n"
        "Please respect copyright and only download content you have the right to."
    )
    if message_id:
        edit_message(chat_id, message_id, text, {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "back"}]]})
    else:
        send_message(chat_id, text, main_menu())


def fetch_and_prompt(chat_id, url):
    msg = send_message(chat_id, "🔍 <b>Fetching info...</b>\n<code>" + esc(url[:120]) + "</code>")
    mid = (msg.get("result") or {}).get("message_id")
    info = yt_info(url)
    if not info:
        edit_message(chat_id, mid, "❌ <b>Sorry, I couldn't read that link.</b>\n"
                     "The site may be blocked, private, or unsupported.\n\n"
                     "<code>" + esc(url[:200]) + "</code>",
                     {"inline_keyboard": [[{"text": "🔁 Retry", "callback_data": "retry:" + _short(url)}]]})
        return

    title = info.get("title") or "Untitled"
    dur = fmt_dur(info.get("duration"))
    uploader = info.get("uploader") or info.get("channel") or info.get("uploader_id") or ""
    jid = _new_job(chat_id, url, info)
    JOBS[jid] = {"chat_id": chat_id, "url": url, "info": info, "msg_id": mid}

    rows = []
    qs = quality_options(info)
    for h, label in qs[:8]:
        rows.append([{"text": f"🎬 {label}", "callback_data": f"v:{jid}:{h}"}])
    rows.append([{"text": "🎵 MP3 (Audio)", "callback_data": f"a:{jid}"}])
    rows.append([{"text": "❌ Cancel", "callback_data": f"x:{jid}"}])

    cap = f"🎞 <b>{esc(title)[:200]}</b>\n"
    if uploader:
        cap += f"👤 {esc(uploader)[:80]}\n"
    cap += f"⏱ {dur}\n\n<b>Choose a format below 👇</b>"
    markup = {"inline_keyboard": rows}

    thumb = info.get("thumbnail")
    if thumb:
        try:
            r = requests.get(thumb, timeout=20)
            if r.status_code == 200 and len(r.content) > 0:
                files = {"photo": ("thumb.jpg", r.content, "image/jpeg")}
                data = {"chat_id": chat_id, "caption": cap, "parse_mode": "HTML",
                        "reply_markup": json.dumps(markup)}
                res = api("sendPhoto", data, files=files, timeout=60)
                if res.get("ok"):
                    if mid:
                        api("deleteMessage", {"chat_id": chat_id, "message_id": mid})
                    return
        except Exception:
            pass
    edit_message(chat_id, mid, cap, markup)


def _new_job(chat_id, url, info):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=10))


def _short(url):
    return str(abs(hash(url)) % 10**12)


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
        sel = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best" if height else "bestvideo+bestaudio/best"
        opts = ["-f", sel, "--merge-output-format", "mp4"]
        kind = "video"

    status = f"⬇️ <b>Downloading...</b>\n🎞 {esc(title)[:120]}"
    if cb_id:
        answer_cb(cb_id, "Download started...")
    if mid:
        edit_message(chat_id, mid, status, {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": f"x:{jid}"}]]})
    chat_action(chat_id, "upload_video" if kind == "video" else "upload_voice")

    counter = {"n": 0}

    def on_prog(pct):
        counter["n"] += 1
        if counter["n"] % 3 == 0 and mid:
            edit_message(chat_id, mid, f"⬇️ <b>Downloading... {pct:.0f}%</b>\n🎞 {esc(title)[:120]}",
                         {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": f"x:{jid}"}]]})

    path, err = run_download(url, opts, out_base, on_prog)
    if not path:
        if mid:
            edit_message(chat_id, mid, f"❌ <b>Download failed</b>\n<code>{esc(str(err)[:180])}</code>\n\n"
                         "This site may be blocked from my server.", None)
        return

    size = os.path.getsize(path)
    if size > TG_LIMIT and kind == "video":
        if mid:
            edit_message(chat_id, mid, "🛠 <b>File too large — compressing to fit Telegram (50 MB)...</b>", None)
        comp = compress_to_fit(path, dur)
        if comp:
            path = comp
            size = os.path.getsize(path)

    size = os.path.getsize(path)
    if size > TG_LIMIT:
        if mid:
            edit_message(chat_id, mid,
                         f"⚠️ <b>File is {fmt_size(size)}</b> which exceeds Telegram's 50 MB bot limit, "
                         "and compression couldn't bring it under.\n\nTry a lower quality or MP3.", None)
        try:
            os.remove(path)
        except OSError:
            pass
        return

    if mid:
        edit_message(chat_id, mid, f"⬆️ <b>Uploading {fmt_size(size)}...</b>\n🎞 {esc(title)[:120]}", None)
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
            edit_message(chat_id, mid, "❌ <b>Upload failed.</b> The file may be too large for Telegram.", None)
    try:
        os.remove(path)
    except OSError:
        pass


def handle_update(upd):
    if "message" in upd:
        m = upd["message"]
        chat = m.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        text = m.get("text") or m.get("caption") or ""
        name = chat.get("first_name") or chat.get("title") or "there"
        if text.startswith("/start"):
            handle_start(chat_id, name)
            return
        if text.startswith("/help"):
            handle_help(chat_id)
            return
        if text.startswith("/about"):
            handle_about(chat_id)
            return
        urls = URL_RE.findall(text)
        if urls:
            seen = []
            for u in urls[:5]:
                u = u.rstrip(".,);")
                if u not in seen:
                    seen.append(u)
            for u in seen:
                fetch_and_prompt(chat_id, u)
            return
        if text:
            send_message(chat_id, "🔗 <b>Send me a video link</b> and I'll download it for you.", main_menu())
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
        elif data == "back":
            answer_cb(cb_id)
            edit_message(chat_id, mid, "👋 <b>Send me a link to get started!</b>", main_menu())
        elif data.startswith("v:"):
            _, jid, h = data.split(":", 2)
            deliver(chat_id, jid, "video", int(h), cb_id)
        elif data.startswith("a:"):
            _, jid = data.split(":", 1)
            deliver(chat_id, jid, "audio", None, cb_id)
        elif data.startswith("x:"):
            answer_cb(cb_id, "Cancelled")
            if mid:
                edit_message(chat_id, mid, "❌ <b>Cancelled.</b>", None)
        elif data.startswith("retry:"):
            answer_cb(cb_id, "Please send the link again.")
        return


def trigger_rerun():
    if not GH_PAT or not GH_REPO or "/" not in GH_REPO:
        log("rerun skipped (no GH_PAT/GH_REPO)")
        return
    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    headers = {"Authorization": f"Bearer {GH_PAT}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.post(url, json={"ref": "main"}, headers=headers, timeout=30)
        log("rerun status", r.status_code)
    except Exception as exc:
        log("rerun exc", exc)


def main():
    log("bot starting, runtime", RUNTIME_MINUTES, "min, cookies:", HAS_COOKIES)
    me = api("getMe")
    log("getMe", me.get("result", {}).get("username"))
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
    trigger_rerun()


if __name__ == "__main__":
    main()
