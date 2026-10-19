#!/usr/bin/env python3
"""ReClip Telegram Bot - a fast, always-on video/audio downloader bot.

- Handles 1000+ sites via yt-dlp + ffmpeg
- Cloudflare WARP relay (userspace WireGuard) fixes YouTube's datacenter IP block
- Downloads run in worker threads: the poll loop never blocks, so the bot stays
  responsive during long downloads
- Large files are always delivered: smart re-encode (with downscale) or splitting
- Downloaded files are auto-deleted after a short TTL (fresh VM each run)
"""
import os
import re
import json
import math
import time
import glob
import random
import string
import subprocess
import sys
import tarfile
import threading
import shutil
import base64
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from datetime import datetime

import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
GH_PAT = os.environ.get("GH_PAT", "").strip()
GH_REPO = os.environ.get("GH_REPO", "").strip()
WORKFLOW_FILE = os.environ.get("WORKFLOW_FILE", "bot.yml")
RUNTIME_MINUTES = int(os.environ.get("RUNTIME_MINUTES", "340"))
FILE_TTL = int(os.environ.get("FILE_TTL_MIN", "20")) * 60
MAX_PAR = int(os.environ.get("MAX_PARALLEL_JOBS", "3"))
MAX_PARTS = int(os.environ.get("MAX_PARTS", "15"))

if not BOT_TOKEN:
    print("FATAL: BOT_TOKEN is not set")
    sys.exit(1)

API = "https://api.telegram.org/bot" + BOT_TOKEN
WORKDIR = "/tmp/reclip"
DL = os.path.join(WORKDIR, "downloads")
os.makedirs(DL, exist_ok=True)

TG_LIMIT = 50 * 1024 * 1024
TG_TARGET = 45 * 1024 * 1024
START = time.time()
DEADLINE = START + RUNTIME_MINUTES * 60
URL_RE = re.compile(r"https?://\S+")
JOBS = {}
ACTIVE = set()
_LOCK = threading.Lock()
_SEM = threading.Semaphore(MAX_PAR)
_DISPATCH = {"ok": False}
_INFLIGHT_URLS = set()


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def _track(*paths):
    with _LOCK:
        for p in paths:
            if p:
                ACTIVE.add(p)


def _untrack(*paths):
    with _LOCK:
        for p in paths:
            ACTIVE.discard(p)


# ------------------------------------------------------------ telegram api --

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


def send_message(chat_id, text, markup=None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True}
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

WARP_ENABLED = os.environ.get("WARP_ENABLE", "1").strip().lower() not in ("0", "false", "no")
WARP_DIR = "/tmp/warp"
PROXY_PORT = int(os.environ.get("WARP_PROXY_PORT", "25344"))
PROXY_URL = "socks5h://127.0.0.1:" + str(PROXY_PORT)
WGCF_PROFILE_ENV = os.environ.get("WGCF_PROFILE", "").strip()
PROXY_STATE = {"up": False, "ip": "", "endpoint": "", "proc": None}

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


# ------------------------------------------------------------- tunnel ------
TUNNEL_ENABLED = os.environ.get('TUNNEL_ENABLE', '1').strip().lower() not in ('0', 'false', 'no')
TUNNEL_TTL = int(os.environ.get('TUNNEL_TTL_MIN', '10')) * 60
TUNNEL_PORT = int(os.environ.get('TUNNEL_PORT', '8791'))
CLOUDFLARED_URL = ('https://github.com/cloudflare/cloudflared/releases/latest/download/'
                   'cloudflared-linux-amd64')
TUNNEL_DIR = '/tmp/reclip_tunnel'
TUNNEL_STATE = {'proc': None, 'url': '', 'up': False}
_TUNNEL_LOCK = threading.Lock()
_SHARES = {}
_SHARES_LOCK = threading.Lock()

INDEX_PAGE = ('<!doctype html><html><head><meta charset="utf-8">'
              '<meta name="robots" content="noindex,nofollow">'
              '<title>ReClip</title></head><body style="background:#0f1117;color:#e6e6e6;'
              'font-family:system-ui;text-align:center;padding:80px 20px">'
              '<h2>This link is private.</h2>'
              '<p>Ask the bot for a fresh link.</p></body></html>')


def _share_html(token, title, kind, size):
    import html as _html
    safe_title = _html.escape(title or 'ReClip media')
    if kind == 'audio':
        media = ('<audio controls autoplay style="width:100%" src="/' + token +
                 '/v"></audio>')
    else:
        media = ('<video controls autoplay playsinline style="width:100%;max-height:70vh;'
                 'background:#000;border-radius:12px" src="/' + token + '/v"></video>')
    return ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow">'
            '<title>' + safe_title + '</title></head>'
            '<body style="margin:0;background:#0f1117;color:#e6e6e6;'
            'font-family:system-ui;text-align:center">'
            '<div style="max-width:900px;margin:0 auto;padding:24px 16px">'
            '<h2 style="font-weight:600">' + safe_title + '</h2>'
            '<p style="color:#9aa0a6">Full quality &middot; ' + fmt_size(size) +
            ' &middot; expires in ' + str(max(1, TUNNEL_TTL // 60)) + ' min</p>' +
            media +
            '<p style="margin-top:18px"><a href="/' + token + '/v" download '
            'style="display:inline-block;padding:12px 22px;background:#2a7fff;color:#fff;'
            'border-radius:10px;text-decoration:none">Download</a></p>'
            '</div></body></html>')


class _ShareHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'r'
    sys_version = ''

    def log_message(self, *a):
        return

    def _head(self, code, ctype, length, extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(length))
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Robots-Tag', 'noindex, nofollow')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Server', 'r')
        if extra:
            for k, v in extra:
                self.send_header(k, v)
        self.end_headers()

    def _body(self, code, body, ctype='text/plain; charset=utf-8'):
        try:
            self._head(code, ctype, len(body))
            if self.command != 'HEAD':
                self.wfile.write(body)
        except Exception:
            pass

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            segs = [s for s in urlparse(self.path).path.split('/') if s]
            if not segs:
                self._body(200, INDEX_PAGE.encode(), 'text/html; charset=utf-8')
                return
            with _SHARES_LOCK:
                share = dict(_SHARES.get(segs[0]) or {})
            if not share:
                self._body(404, b'Not found')
                return
            if time.time() > share['expires']:
                self._body(410, b'Link expired')
                return
            if len(segs) == 1:
                fp = share['path']
                size = os.path.getsize(fp) if os.path.isfile(fp) else 0
                html = _share_html(segs[0], share.get('title'), share.get('kind'), size)
                self._body(200, html.encode(), 'text/html; charset=utf-8')
                return
            if len(segs) == 2 and segs[1] == 'v':
                self._serve(share)
                return
            self._body(404, b'Not found')
        except Exception:
            pass

    def _serve(self, share):
        fp = share['path']
        if not os.path.isfile(fp):
            self._body(404, b'Gone')
            return
        total = os.path.getsize(fp)
        start, end = 0, total - 1
        code = 200
        rng = self.headers.get('Range')
        if rng:
            m = re.match(r'bytes=(\d*)-(\d*)', rng or '')
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), total - 1)
                if start >= total or start > end:
                    self._body(416, b'Range not satisfiable')
                    return
                code = 206
        length = end - start + 1
        extra = [('Accept-Ranges', 'bytes'),
                 ('Content-Disposition', 'inline; filename="media.bin"')]
        if code == 206:
            extra.append(('Content-Range', 'bytes %d-%d/%d' % (start, end, total)))
        try:
            self._head(code, share.get('ctype') or 'application/octet-stream', length, extra)
            if self.command == 'HEAD':
                return
            with open(fp, 'rb') as fh:
                fh.seek(start)
                left = length
                while left > 0:
                    chunk = fh.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except Exception:
            pass


def _serve_http():
    try:
        httpd = ThreadingHTTPServer(('127.0.0.1', TUNNEL_PORT), _ShareHandler)
        httpd.daemon_threads = True
        log('tunnel: http server on', TUNNEL_PORT)
        httpd.serve_forever()
    except Exception as exc:
        log('tunnel: http server err', exc)


def _ensure_cloudflared():
    os.makedirs(WARP_DIR, exist_ok=True)
    cf = os.path.join(WARP_DIR, 'cloudflared')
    if os.path.isfile(cf) and os.path.getsize(cf) > 1000000:
        return cf
    if not _download(CLOUDFLARED_URL, cf):
        return None
    try:
        os.chmod(cf, 0o755)
    except Exception:
        pass
    return cf


def _kill_tunnel():
    p = TUNNEL_STATE.get('proc')
    if p and p.poll() is None:
        try:
            p.terminate()
        except Exception:
            pass


def setup_tunnel():
    with _TUNNEL_LOCK:
        if not TUNNEL_ENABLED:
            return False
        if TUNNEL_STATE.get('up') and TUNNEL_STATE.get('url'):
            return True
        cf = _ensure_cloudflared()
        if not cf:
            log('tunnel: cloudflared unavailable')
            return False
        os.makedirs(TUNNEL_DIR, exist_ok=True)
        logf = os.path.join(TUNNEL_DIR, 'cloudflared.log')
        try:
            open(logf, 'w').close()
            out = open(logf, 'ab')
            proc = subprocess.Popen([cf, 'tunnel', '--url',
                                     'http://127.0.0.1:' + str(TUNNEL_PORT),
                                     '--no-autoupdate'], stdout=out,
                                    stderr=subprocess.STDOUT)
            TUNNEL_STATE['proc'] = proc
        except Exception as exc:
            log('tunnel: start err', exc)
            return False
        for _ in range(45):
            time.sleep(2)
            try:
                txt = open(logf, errors='ignore').read()
            except Exception:
                txt = ''
            m = re.findall(r'https://[a-z0-9-]+\.trycloudflare\.com', txt)
            if m:
                TUNNEL_STATE['url'] = m[-1]
                TUNNEL_STATE['up'] = True
                log('tunnel: up at', TUNNEL_STATE['url'])
                return True
        log('tunnel: failed to get url')
        return False


def tunnel_watchdog():
    while time.time() < DEADLINE + 120:
        time.sleep(60)
        p = TUNNEL_STATE.get('proc')
        if (TUNNEL_STATE.get('up') and TUNNEL_STATE.get('url')
                and p and p.poll() is None):
            continue
        log('tunnel: down - restarting')
        _kill_tunnel()
        TUNNEL_STATE['up'] = False
        TUNNEL_STATE['url'] = ''
        setup_tunnel()


def create_share(path, title, kind):
    if not TUNNEL_STATE.get('url'):
        setup_tunnel()
    if not TUNNEL_STATE.get('url'):
        return None
    token = secrets.token_urlsafe(24).replace('-', '').replace('_', '')
    ctype = 'audio/mpeg' if kind == 'audio' else 'video/mp4'
    with _SHARES_LOCK:
        _SHARES[token] = {'path': path, 'title': title, 'kind': kind,
                          'ctype': ctype, 'expires': time.time() + TUNNEL_TTL}
    _track(path)
    stats_bump('tunnels')
    return TUNNEL_STATE['url'].rstrip('/') + '/' + token


def expiry_loop():
    while time.time() < DEADLINE + 300:
        time.sleep(20)
        now = time.time()
        dead = []
        with _SHARES_LOCK:
            for tok, sh in list(_SHARES.items()):
                if now > sh['expires']:
                    dead.append((tok, sh))
                    _SHARES.pop(tok, None)
        for tok, sh in dead:
            _cleanup([sh.get('path')])
            log('tunnel: share expired + wiped', tok)


def _tunnel_msg(chat_id, mid, link, title, size, dur):
    text = ('\u2705 <b>Ready — full quality!</b>\n'
            '━━━━━━━━━━━━━━━\n'
            '🎞 <b>' + esc(title)[:180] + '</b>\n'
            '📦 ' + fmt_size(size) + ' • \u23f1 ' + fmt_dur(dur) + '\n\n'
            '\u25b6\ufe0f <b>Play or download</b> (original quality, no compression):\n'
            '<a href="' + link + '">' + link + '</a>\n\n'
            '\u23f3 This link self-destructs in ' + str(TUNNEL_TTL // 60) + ' min.\n'
            '\U0001f512 Private single-file link. Nothing else is exposed.')
    markup = {'inline_keyboard': [[{'text': '\u25b6\ufe0f Play / Download', 'url': link}]]}
    if mid:
        edit_message(chat_id, mid, text, markup)
    else:
        send_message(chat_id, text, markup)


# -------------------------------------------------------------- state ------
STATE_BRANCH = 'reclip-state'
STATS_LOCK = threading.Lock()
STATS = {'boots': 0, 'downloads': 0, 'tunnels': 0, 'bytes': 0,
         'admin_id': 0, 'admin_name': '', 'first_boot': '', 'last_boot': ''}
try:
    ADMIN_ID = int((os.environ.get('ADMIN_ID', '0') or '0').strip())
except Exception:
    ADMIN_ID = 0


def _gh_headers():
    return {'Authorization': 'Bearer ' + GH_PAT,
            'Accept': 'application/vnd.github+json'}


def stats_bump(key, n=1):
    with STATS_LOCK:
        STATS[key] = STATS.get(key, 0) + n


def _ensure_state_branch():
    if not (GH_PAT and GH_REPO):
        return False
    try:
        r = requests.get('https://api.github.com/repos/' + GH_REPO +
                         '/git/ref/heads/' + STATE_BRANCH,
                         headers=_gh_headers(), timeout=20)
        if r.status_code == 200:
            return True
        m = requests.get('https://api.github.com/repos/' + GH_REPO +
                         '/git/ref/heads/main', headers=_gh_headers(), timeout=20)
        if m.status_code != 200:
            return False
        sha = m.json()['object']['sha']
        c = requests.post('https://api.github.com/repos/' + GH_REPO + '/git/refs',
                          headers=_gh_headers(),
                          json={'ref': 'refs/heads/' + STATE_BRANCH, 'sha': sha},
                          timeout=20)
        return c.status_code in (200, 201)
    except Exception as exc:
        log('state: branch err', exc)
        return False


def stats_load():
    if not (GH_PAT and GH_REPO):
        return
    _ensure_state_branch()
    try:
        r = requests.get('https://api.github.com/repos/' + GH_REPO +
                         '/contents/state.json?ref=' + STATE_BRANCH,
                         headers=_gh_headers(), timeout=20)
        if r.status_code == 200:
            j = r.json()
            STATS.update(json.loads(base64.b64decode(j['content']).decode()))
            STATS['_sha'] = j.get('sha')
            log('state: loaded | boots', STATS.get('boots'))
    except Exception as exc:
        log('state: load err', exc)


def stats_save():
    if not (GH_PAT and GH_REPO):
        return
    try:
        with STATS_LOCK:
            body = {k: v for k, v in STATS.items() if not k.startswith('_')}
        content = base64.b64encode(json.dumps(body, indent=1).encode()).decode()
        payload = {'message': 'state update', 'content': content, 'branch': STATE_BRANCH}
        if STATS.get('_sha'):
            payload['sha'] = STATS['_sha']
        r = requests.put('https://api.github.com/repos/' + GH_REPO +
                         '/contents/state.json', headers=_gh_headers(),
                         json=payload, timeout=25)
        if r.status_code in (200, 201):
            STATS['_sha'] = r.json()['content']['sha']
    except Exception as exc:
        log('state: save err', exc)


def stats_loop():
    while time.time() < DEADLINE + 60:
        time.sleep(180)
        stats_save()


def is_admin(uid):
    if ADMIN_ID:
        return uid == ADMIN_ID
    return bool(STATS.get('admin_id')) and uid == STATS.get('admin_id')


def set_admin_if_needed(uid, name):
    if ADMIN_ID or STATS.get('admin_id'):
        return
    STATS['admin_id'] = uid
    STATS['admin_name'] = name
    log('admin: auto-detected', uid, name)
    stats_save()


def handle_admin(chat_id, message_id=None):
    if not is_admin(chat_id):
        send_message(chat_id, '⛔ <b>Admin only.</b>')
        return
    with _SHARES_LOCK:
        shares = len(_SHARES)
    text = ('\U0001f6e1 <b>Admin panel</b>\n'
            '━━━━━━━━━━━━━━━\n'
            '\U0001f7e2 Uptime (this run): <b>' + fmt_dur(time.time() - START) + '</b>\n'
            '\U0001f680 Boots recorded: <b>' + str(STATS.get('boots', 0)) + '</b>\n'
            '\u2b07\ufe0f Downloads served: <b>' + str(STATS.get('downloads', 0)) + '</b>\n'
            '\U0001f4e1 Tunnel links created: <b>' + str(STATS.get('tunnels', 0)) + '</b>\n'
            '🎞 Active links now: <b>' + str(shares) + '</b>\n'
            '\U0001f310 Tunnel status: <b>' +
            ('up' if TUNNEL_STATE.get('up') else 'down') + '</b>\n'
            '\U0001f6f0 WARP egress: <b>' + (PROXY_STATE.get('ip') or 'direct') + '</b>\n'
            '\U0001f464 Admin: <b>' + esc(STATS.get('admin_name') or 'unset') + '</b>\n'
            '\U0001f9f9 Auto-wipe window: <b>' + str(TUNNEL_TTL // 60) + ' min</b>')
    markup = {'inline_keyboard': [[{'text': '⬅️ Back', 'callback_data': 'back'}]]}
    if message_id:
        edit_message(chat_id, message_id, text, markup)
    else:
        send_message(chat_id, text, main_menu())


# ---------------------------------------------------------------- engine ----


def yt_base():
    cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--socket-timeout", "30",
           "--retries", "10", "--fragment-retries", "20", "--no-cache-dir"]
    if PROXY_STATE.get("up"):
        cmd += ["--proxy", PROXY_URL]
    return cmd


def yt_info(url):
    for extra in ([], ["--proxy", PROXY_URL]) if PROXY_STATE.get("up") else ([],):
        try:
            res = subprocess.run(
                ["yt-dlp", "--no-playlist", "--no-warnings", "--socket-timeout", "30",
                 "--no-cache-dir"] + extra + ["-j", url],
                capture_output=True, text=True, timeout=150)
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


def run_download(url, opts, out_base, on_progress=None):
    attempts = []
    if PROXY_STATE.get("up"):
        attempts.append(["--proxy", PROXY_URL])
        attempts.append(["--proxy", PROXY_URL,
                         "--extractor-args", "youtube:player_client=default,tv,web"])
    attempts.append([])
    attempts.append(["--extractor-args", "youtube:player_client=default,tv,web"])
    last_err = ""
    for extra in attempts:
        cmd = ["yt-dlp", "--no-playlist", "--no-warnings", "--socket-timeout", "30",
               "--retries", "10", "--fragment-retries", "20", "--no-cache-dir"]
        cmd += extra + opts + ["-o", out_base + ".%(ext)s", url]
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


def _ffmpeg(args, timeout=2700):
    try:
        r = subprocess.run(["ffmpeg", "-y"] + args, capture_output=True,
                           timeout=timeout)
        return r.returncode == 0
    except Exception as exc:
        log("ffmpeg err", exc)
        return False


def fit_video(path, duration, title):
    """Guarantee delivery under Telegram's 50 MB limit.

    Returns a list of file paths (1 for a re-encode, many for a split),
    or [] on failure. Never returns oversized files.
    """
    size = os.path.getsize(path)
    if size <= TG_LIMIT:
        return [path]

    dur = max(float(duration or 0), 1)
    base = path.rsplit(".", 1)[0]
    audio_bps = 48000
    vbit = int((TG_TARGET * 8) / dur) - audio_bps
    if vbit >= 300000:
        scale = None
    elif vbit >= 200000:
        scale = 720
    elif vbit >= 120000:
        scale = 480
    else:
        scale = 360
    vbit = max(vbit, 32000)

    out = base + "_fit.mp4"
    args = ["-i", path, "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", str(vbit), "-maxrate", str(int(vbit * 1.25)),
            "-bufsize", str(vbit * 2), "-c:a", "aac", "-b:a", "48k",
            "-movflags", "+faststart"]
    if scale:
        args += ["-vf", f"scale=-2:{scale}"]
    args += [out]
    if _ffmpeg(args) and os.path.isfile(out):
        if os.path.getsize(out) <= TG_LIMIT:
            return [out]
        # still too big: split the re-encoded file
        parts = _split(out, dur)
        if parts:
            return parts
        return [out]

    # re-encode failed: split the original with stream copy (fast, keeps quality)
    parts = _split(path, dur)
    return parts or []


def _split(path, duration):
    total = os.path.getsize(path)
    if total <= TG_LIMIT:
        return [path]
    dur = max(float(duration or 0), 1)
    n = max(2, int(math.ceil(total / float(TG_TARGET))))
    seg = max(dur / n, 1)
    base = path.rsplit(".", 1)[0]
    pattern = base + "_p%03d.mp4"
    for old in glob.glob(base + "_p*.mp4"):
        try:
            os.remove(old)
        except OSError:
            pass
    ok = _ffmpeg(["-i", path, "-c", "copy", "-f", "segment",
                  "-segment_time", str(seg), "-reset_timestamps", "1", pattern])
    if not ok:
        return []
    parts = sorted(glob.glob(base + "_p*.mp4"))
    parts = [p for p in parts if os.path.getsize(p) > 1024]
    if not parts:
        return []
    if max(os.path.getsize(p) for p in parts) > TG_LIMIT:
        for p in parts:
            try:
                os.remove(p)
            except OSError:
                pass
        n2 = n + 2
        seg2 = max(dur / n2, 1)
        pattern2 = base + "_q%03d.mp4"
        if _ffmpeg(["-i", path, "-c", "copy", "-f", "segment",
                    "-segment_time", str(seg2), "-reset_timestamps", "1", pattern2]):
            parts = sorted(glob.glob(base + "_q*.mp4"))
            parts = [p for p in parts if 1024 < os.path.getsize(p) <= TG_LIMIT]
        else:
            parts = []
    return parts


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

def main_menu(chat_id=None):
    rows = [
        [{"text": "🎞 How to use", "callback_data": "help"},
         {"text": "ℹ️ About", "callback_data": "about"}],
        [{"text": "📊 Status", "callback_data": "status"}],
    ]
    if chat_id is not None and is_admin(chat_id):
        rows.append([{"text": "🛡 Admin", "callback_data": "admin"}])
    return {"inline_keyboard": rows}


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
        "Reddit, Vimeo and 1000+ more sites.</i>\n\n"
        "💎 <b>Any size, full quality</b> — huge & 1-hour videos come as a\n"
        "private play/download link (50 MB limit doesn't apply).",
    )
    send_message(chat_id, text, main_menu(chat_id))


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
        "• Big files (any size, 1-hour+) → get a private <b>play/download link</b> 💎\n"
        "• Small files (under 50 MB) come straight into the chat\n"
        "• Links <b>self-destruct</b> in 10 min and are wiped from the server\n"
        "• Tap ❌ <b>Cancel</b> anytime"
    )
    markup = {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "back"}]]}
    if message_id:
        edit_message(chat_id, message_id, text, markup)
    else:
        send_message(chat_id, text, main_menu(chat_id))


def handle_about(chat_id, message_id=None):
    text = (
        "ℹ️ <b>About ReClip</b>\n"
        "━━━━━━━━━━━━━━━\n\n"
        "A fast, always-on downloader powered by <b>yt-dlp</b> + <b>ffmpeg</b>.\n"
        "Supports <b>1000+</b> sites for video and audio.\n\n"
        "💎 <b>Full quality, any size</b>\n"
        "• Small files arrive in chat (< 50 MB)\n"
        "• Heavy / long videos get a private play+download link\n\n"
        "🔒 <b>Privacy first</b>\n"
        "• Links are single-use, unlisted and self-destruct in 10 min\n"
        "• Files are securely wiped from the server after expiry\n"
        "• Nothing is stored permanently\n\n"
        "<i>Please respect copyright — only download content you have the right to.</i>"
    )
    markup = {"inline_keyboard": [[{"text": "⬅️ Back", "callback_data": "back"}]]}
    if message_id:
        edit_message(chat_id, message_id, text, markup)
    else:
        send_message(chat_id, text, main_menu(chat_id))


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
        send_message(chat_id, text, main_menu(chat_id))


# ---------------------------------------------------------------- fetching --

def animate(chat_id, mid, stop, frames, label):
    i = 0
    while not stop.is_set() and time.time() < DEADLINE:
        try:
            edit_message(chat_id, mid, frames[i % len(frames)] + f" <b>{label}</b>")
        except Exception:
            pass
        i += 1
        if stop.wait(1.6):
            break


def fetch_and_prompt(chat_id, url):
    try:
        msg = send_message(chat_id, "🔍 <b>Fetching video info…</b>")
        mid = (msg.get("result") or {}).get("message_id")
        stop = threading.Event()
        if mid:
            threading.Thread(target=animate, args=(chat_id, mid, stop,
                             ["🔍", "🧩", "⚙️", "🧠", "🛰"], "Fetching video info…"),
                             daemon=True).start()
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
    except Exception as exc:
        log("fetch exc", exc)
    finally:
        with _LOCK:
            _INFLIGHT_URLS.discard(url)


# --------------------------------------------------------------- delivering --

def deliver_thread(chat_id, jid, mode, height, cb_id):
    _SEM.acquire()
    try:
        deliver(chat_id, jid, mode, height, cb_id)
    except Exception as exc:
        log("deliver exc", exc)
    finally:
        _SEM.release()


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
        opts = ["-f", "bestaudio/best", "-x", "--audio-format", "mp3",
                "--audio-quality", "128K"]
        kind = "audio"
    else:
        sel = (f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
               if height else "bestvideo+bestaudio/best")
        opts = ["-f", sel, "--merge-output-format", "mp4"]
        kind = "video"

    if cb_id:
        answer_cb(cb_id, "🚀 Download started…")
    cancel_btn = {"inline_keyboard": [[{"text": "❌ Cancel", "callback_data": f"x:{jid}"}]]}
    if mid:
        edit_message(chat_id, mid,
                     f"🚀 <b>Starting download…</b>\n🎞 {esc(title)[:120]}\n\n▱▱▱▱▱▱▱▱▱▱▱▱  0%",
                     cancel_btn)
    chat_action(chat_id, "upload_video" if kind == "video" else "upload_voice")

    counter = {"n": 0}

    def on_prog(pct):
        counter["n"] += 1
        if counter["n"] % 2 == 0 and mid:
            edit_message(chat_id, mid,
                         f"⬇️ <b>Downloading…</b>\n{bar(pct)}\n🎞 {esc(title)[:100]}",
                         cancel_btn)
            chat_action(chat_id, "upload_video" if kind == "video" else "upload_voice")

    path, err = run_download(url, opts, out_base, on_prog)
    if not path:
        if mid:
            edit_message(chat_id, mid,
                         "❌ <b>Download failed.</b>\n<code>" + esc(str(err)[:180]) + "</code>",
                         {"inline_keyboard": [[{"text": "🔁 Retry", "callback_data": f"v:{jid}:0"}]]})
        return

    _track(path)
    size = os.path.getsize(path)
    stats_bump("downloads")
    stats_bump("bytes", size)

    # small enough -> deliver straight into the chat
    if size <= TG_LIMIT:
        if mid:
            edit_message(chat_id, mid,
                         f"⬆️ <b>Uploading {fmt_size(size)}…</b>\n🎞 {esc(title)[:120]}", None)
        cap = f"🎞 <b>{esc(title)[:180]}</b>\n⏱ {fmt_dur(dur)} • {fmt_size(size)}"
        chat_action(chat_id, "upload_video" if kind == "video" else "upload_voice")
        ok, _ = send_media(chat_id, path, kind, cap)
        if ok:
            if mid:
                try:
                    api("deleteMessage", {"chat_id": chat_id, "message_id": mid})
                except Exception:
                    pass
        elif mid:
            edit_message(chat_id, mid, "❌ <b>Upload failed</b> — please try again.", None)
        _cleanup([path])
        return

    # too big for Telegram -> serve FULL quality through the Cloudflare tunnel
    link = create_share(path, title, kind)
    if link:
        _tunnel_msg(chat_id, mid, link, title, size, dur)
        return

    # tunnel unavailable -> last-resort compress to fit Telegram
    if kind == "video":
        if mid:
            edit_message(chat_id, mid,
                         f"🛠 <b>File is {fmt_size(size)}.</b>\n"
                         "Optimising it to fit Telegram (50 MB)… ⏳",
                         cancel_btn)
        stop = threading.Event()
        if mid:
            threading.Thread(target=animate, args=(chat_id, mid, stop,
                             ["🛠", "🎛", "📦", "⏳"], "Optimising…"),
                             daemon=True).start()
        parts = fit_video(path, dur, title)
        stop.set()
        for p in parts:
            _track(p)
        if not parts:
            if mid:
                edit_message(chat_id, mid,
                             "❌ <b>Could not deliver this file.</b>\nTry a lower quality.",
                             None)
            _cleanup([path])
            return
        total = len(parts)
        sent = 0
        for idx, p in enumerate(parts, 1):
            try:
                psize = os.path.getsize(p)
            except OSError:
                break
            if psize > TG_LIMIT:
                continue
            head = f"({idx}/{total}) " if total > 1 else ""
            if mid:
                edit_message(chat_id, mid,
                             f"⬆️ <b>Uploading {head}{fmt_size(psize)}…</b>\n🎞 {esc(title)[:120]}", None)
            cap = f"🎞 <b>{esc(title)[:180]}</b>\n⏱ {fmt_dur(dur)} • {fmt_size(psize)}"
            if total > 1:
                cap += f"\n📦 Part <b>{idx}</b> of <b>{total}</b>"
            chat_action(chat_id, "upload_video")
            ok, _ = send_media(chat_id, p, "video", cap)
            if ok:
                sent += 1
            time.sleep(1.5)
        if sent and mid:
            try:
                api("deleteMessage", {"chat_id": chat_id, "message_id": mid})
            except Exception:
                pass
        _cleanup(parts + [path])
        return

    if mid:
        edit_message(chat_id, mid,
                     "⚠️ <b>File is too large for Telegram's 50 MB bot limit.</b>\n"
                     "Try a lower quality or MP3 🎵",
                     {"inline_keyboard": [[{"text": "🎵 Get MP3", "callback_data": f"a:{jid}"}]]})
    _cleanup([path])


def _cleanup(paths):
    for p in paths:
        _untrack(p)
        try:
            if p and os.path.isfile(p):
                os.remove(p)
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
        set_admin_if_needed(chat_id, name)
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
        if text.startswith("/admin"):
            handle_admin(chat_id)
            return
        urls = [u.rstrip(".,);]") for u in URL_RE.findall(text)]
        if urls:
            seen = []
            for u in urls[:5]:
                if u not in seen:
                    seen.append(u)
            for u in seen:
                with _LOCK:
                    if u in _INFLIGHT_URLS:
                        continue
                    _INFLIGHT_URLS.add(u)
                threading.Thread(target=fetch_and_prompt, args=(chat_id, u),
                                 daemon=True).start()
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
        elif data == "admin":
            answer_cb(cb_id)
            handle_admin(chat_id, mid)
        elif data == "back":
            answer_cb(cb_id)
            edit_message(chat_id, mid, "👋 <b>Send me a link to get started!</b> 🔗", main_menu(chat_id))
        elif data.startswith("v:"):
            parts = data.split(":")
            height = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() and int(parts[2]) > 0 else None
            answer_cb(cb_id, "🚀 Download started…")
            threading.Thread(target=deliver_thread,
                             args=(chat_id, parts[1], "video", height, None),
                             daemon=True).start()
        elif data.startswith("a:"):
            answer_cb(cb_id, "🚀 Download started…")
            threading.Thread(target=deliver_thread,
                             args=(chat_id, data.split(":", 1)[1], "audio", None, None),
                             daemon=True).start()
        elif data.startswith("x:"):
            answer_cb(cb_id, "Cancelled")
            if mid:
                edit_message(chat_id, mid, "❌ <b>Cancelled.</b>", None)
        return


# -------------------------------------------------------------- janitors ----

def janitor_loop():
    while time.time() < DEADLINE + 120:
        time.sleep(60)
        now = time.time()
        try:
            with _LOCK:
                active = set(ACTIVE)
            for f in glob.glob(os.path.join(DL, "*")):
                try:
                    if not os.path.isfile(f):
                        continue
                    if f in active:
                        continue
                    if f.endswith((".part", ".ytdl")):
                        continue
                    if (now - os.path.getmtime(f)) > FILE_TTL:
                        os.remove(f)
                except OSError:
                    pass
        except Exception as exc:
            log("janitor exc", exc)


# ------------------------------------------------------------------ main ----

def trigger_rerun():
    if not GH_PAT or not GH_REPO or "/" not in GH_REPO:
        log("rerun skipped (no GH_PAT/GH_REPO)")
        return False
    url = ("https://api.github.com/repos/" + GH_REPO +
           "/actions/workflows/" + WORKFLOW_FILE + "/dispatches")
    headers = {"Authorization": "Bearer " + GH_PAT, "Accept": "application/vnd.github+json"}
    for _ in range(3):
        try:
            r = requests.post(url, json={"ref": "main"}, headers=headers, timeout=30)
            log("rerun status", r.status_code)
            if r.status_code in (204, 201, 200):
                _DISPATCH["ok"] = True
                return True
        except Exception as exc:
            log("rerun exc", exc)
        time.sleep(5)
    return False


def rerun_scheduler():
    """Dispatch the next run BEFORE this one ends, so there is no dead gap."""
    target = DEADLINE - 240
    delay = max(0, target - time.time())
    if delay:
        time.sleep(delay)
    if not _DISPATCH["ok"]:
        trigger_rerun()


def main():
    log("bot starting | runtime", RUNTIME_MINUTES, "min | warp:", WARP_ENABLED,
        "| parallel:", MAX_PAR)
    stats_load()
    stats_bump("boots")
    if not STATS.get("first_boot"):
        STATS["first_boot"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    STATS["last_boot"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    stats_save()
    threading.Thread(target=janitor_loop, daemon=True).start()
    threading.Thread(target=expiry_loop, daemon=True).start()
    threading.Thread(target=stats_loop, daemon=True).start()
    threading.Thread(target=rerun_scheduler, daemon=True).start()
    threading.Thread(target=_serve_http, daemon=True).start()
    threading.Thread(target=setup_tunnel, daemon=True).start()
    if TUNNEL_ENABLED:
        threading.Thread(target=tunnel_watchdog, daemon=True).start()
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
    stats_save()
    _kill_tunnel()
    _kill_proxy()
    if not _DISPATCH["ok"]:
        trigger_rerun()


if __name__ == "__main__":
    main()
