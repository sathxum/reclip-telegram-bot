"""Ephemeral, fixed-admin-only console/studio. Never mount on the share server.

Launch capabilities live in fragments (not access logs); every API is scoped to
an expiring session. The console is trusted administrator remote code execution,
NOT a sandbox. Commands run as the bot's OS user, without inherited credentials.
"""
import atexit
import base64
import hashlib
import json
import math
import os
import re
import secrets
import selectors
import signal
import socket
import subprocess
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

LAUNCH_TTL = 90
SESSION_TTL = 600
COMMAND_TIMEOUT = 30
EDIT_TIMEOUT = 180
OUTPUT_LIMIT = 64 * 1024
COOKIE = '__Host-reclip_admin'


def kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def validate_edit(data, info):
    """Accept numbers only, ordered non-overlapping ranges and even pixel crop."""
    def number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('Timestamps must be finite numbers in seconds.')
        return float(value)
    segments = data.get('segments')
    if not isinstance(segments, list) or not 1 <= len(segments) <= 8:
        raise ValueError('Choose 1 to 8 segments.')
    previous = 0
    clean = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError('Invalid segment.')
        start, end = number(segment.get('start')), number(segment.get('end'))
        if start < previous or end - start < .05 or end > info['duration']:
            raise ValueError('Segments must be ordered, non-overlapping and inside the video.')
        clean.append((start, end))
        previous = end
    if sum(end - start for start, end in clean) > 600:
        raise ValueError('Edited output is limited to 10 minutes.')
    crop = data.get('crop')
    if crop is not None:
        if not info.get('crop_safe', True):
            raise ValueError('Pixel cropping unavailable for rotated or non-square-pixel video.')
        if not isinstance(crop, dict):
            raise ValueError('Invalid crop.')
        values = [crop.get(k) for k in ('x', 'y', 'width', 'height')]
        if any(type(v) is not int or v < 0 or v % 2 for v in values):
            raise ValueError('Crop values must be non-negative even pixel integers.')
        x, y, w, h = values
        if w < 2 or h < 2 or x + w > info['width'] or y + h > info['height']:
            raise ValueError('Crop must fit inside the video.')
        crop = values
    return clean, crop


def media_info(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams',
                             '-of', 'json', path], capture_output=True, timeout=15, check=True)
    if len(result.stdout) > 1024 * 1024:
        raise ValueError('Media metadata too large.')
    metadata = json.loads(result.stdout)
    video = next(s for s in metadata['streams'] if s['codec_type'] == 'video')
    # Display/crop coordinates are the encoded pixels; disable ffmpeg autorotation.
    duration = float(metadata['format']['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Invalid video duration.')
    rotation = any(float(item.get('rotation', 0)) % 360 for item in video.get('side_data_list', [])) or float(video.get('tags', {}).get('rotate', 0)) % 360
    crop_safe = not rotation and video.get('sample_aspect_ratio', '1:1') in ('1:1', '0:1', 'N/A')
    return dict(duration=duration, width=int(video['width']), height=int(video['height']), crop_safe=crop_safe,
                audio=any(s['codec_type'] == 'audio' for s in metadata['streams']))


class LimitedServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    def __init__(self, address, handler):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class AdminServer:
    def __init__(self, admin_id, deadline, cleanup, port=8792, cwd=None):
        self.admin_id = admin_id if type(admin_id) is int and admin_id > 0 else 0
        self.deadline = deadline
        self.cleanup_file = cleanup
        self.port = port
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.origin = ''
        self.tunnel_generation = 0
        self.lock = threading.RLock()
        self.worker = threading.BoundedSemaphore(1)
        self.launches, self.sessions, self.processes = {}, {}, set()
        self.stopped = threading.Event()
        self.httpd = None
        self.tunnel = None
        self.tunnel_lock = threading.Lock()
        atexit.register(self.close)
        self.directory = tempfile.TemporaryDirectory(prefix='reclip-admin-')
        self.html = Path(__file__).with_name('admin_ui.html').read_bytes()
        text = self.html.decode()
        hashes = {}
        for tag in ('script', 'style'):
            hashes[tag] = ' '.join("'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'"
                                   for s in re.findall(r'<' + tag + r'[^>]*>(.*?)</' + tag + '>', text, re.S))
        self.csp = ("default-src 'none'; script-src " + hashes['script'] +
                    "; style-src " + hashes['style'] + "; connect-src 'self'; media-src 'self'; "
                    "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

    def start(self):
        if not self.admin_id:
            return False
        self.httpd = LimitedServer(('127.0.0.1', self.port), self.handler())
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        threading.Thread(target=self._reaper, daemon=True).start()
        return True

    def _reaper(self):
        while not self.stopped.wait(1):
            self.expire()
            if time.time() >= self.deadline:
                self.close()
                return

    def expire(self):
        with self.lock:
            for store in (self.launches, self.sessions):
                for key, entry in list(store.items()):
                    if entry['expires'] <= time.time():
                        store.pop(key)
                        self._dispose(entry)

    def _dispose(self, entry):
        entry['revoked'] = True
        for proc in list(entry.get('processes', set())):
            kill_group(proc)
        if entry.get('path'):
            self.cleanup_file([entry['path']])
        for job in entry.get('jobs', {}).values():
            try:
                os.unlink(job['path'])
            except FileNotFoundError:
                pass

    def close(self):
        with self.lock:
            if self.stopped.is_set():
                return
            self.stopped.set()
            self.origin = ''
            self.tunnel_generation += 1
            for entry in list(self.launches.values()) + list(self.sessions.values()):
                self._dispose(entry)
            self.launches.clear()
            self.sessions.clear()
            for proc in list(self.processes):
                kill_group(proc)
        if self.tunnel and self.tunnel.poll() is None:
            self.tunnel.terminate()
            try:
                self.tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.tunnel.kill()
                self.tunnel.wait()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.directory.cleanup()
        atexit.unregister(self.close)

    def ensure_tunnel(self, binary):
        """Separate quick tunnel; lifecycle publication shares the launch/close lock."""
        with self.tunnel_lock:
            with self.lock:
                if self.stopped.is_set() or time.time() >= self.deadline:
                    return False
                if self.tunnel and self.tunnel.poll() is None and self.origin:
                    return True
                # Invalidate probes too, even if a replacement reuses the hostname.
                self.tunnel_generation += 1
                generation = self.tunnel_generation
                for entry in list(self.launches.values()) + list(self.sessions.values()):
                    self._dispose(entry)
                self.launches.clear()
                self.sessions.clear()
                self.origin = ''
                ready = threading.Event()
                # Register the subprocess while holding the same lock as close(),
                # so shutdown cannot miss a newly created tunnel.
                proc = subprocess.Popen([binary, 'tunnel', '--url',
                    'http://127.0.0.1:' + str(self.port), '--no-autoupdate'],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                self.tunnel = proc

            def drain():
                # No request/token logs are forwarded or persisted.
                for line in proc.stdout:
                    match = re.search(rb'https://[a-z0-9-]+\.trycloudflare\.com', line)
                    if match and not ready.is_set():
                        with self.lock:
                            if (not self.stopped.is_set() and time.time() < self.deadline
                                    and self.tunnel is proc
                                    and self.tunnel_generation == generation):
                                self.origin = match.group().decode()
                            ready.set()
                proc.stdout.close()

            threading.Thread(target=drain, daemon=True).start()
            signaled = ready.wait(60)
            with self.lock:
                if (signaled and not self.stopped.is_set() and time.time() < self.deadline
                        and self.tunnel is proc and self.tunnel_generation == generation
                        and self.origin and proc.poll() is None):
                    return True
                if self.tunnel is proc:
                    self.origin = ''
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            return False

    def launch(self, uid, kind='terminal', path=None, title=''):
        """Transfer source ownership only on successful capability insertion.

        Metadata probing intentionally does not block shutdown. On any failure the
        caller still owns path and must delete/untrack it (bot.deliver does this).
        """
        if not self.admin_id or uid != self.admin_id or kind not in ('terminal', 'studio'):
            raise PermissionError('Fixed admin required.')
        with self.lock:
            if not self.origin or self.stopped.is_set() or time.time() >= self.deadline:
                raise ValueError('Admin tunnel unavailable.')
            origin, generation = self.origin, self.tunnel_generation
        info = media_info(path) if kind == 'studio' else None
        self.expire()
        with self.lock:
            # Probe results must never revive a closed or superseded lifecycle.
            if (self.stopped.is_set() or time.time() >= self.deadline
                    or self.origin != origin or self.tunnel_generation != generation):
                raise ValueError('Admin lifecycle changed during launch; request a fresh link.')
            if len(self.launches) + len(self.sessions) >= 8:
                raise ValueError('Too many admin sessions; close a session or wait for expiry.')
            token = secrets.token_urlsafe(32)
            self.launches[token] = dict(uid=uid, kind=kind, path=path, title=title,
                info=info, expires=min(time.time() + LAUNCH_TTL, self.deadline), jobs={},
                processes=set(), cwd=self.cwd)
            return origin + '/#' + token

    def execute(self, argv, session, timeout, output_limit=OUTPUT_LIMIT, cwd=None):
        env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TERM') if k in os.environ}
        env['PATH'] = env.get('PATH', '/usr/local/bin:/usr/bin:/bin')
        env['TERM'] = 'dumb'
        with self.lock:
            if session.get('revoked') or session['expires'] <= time.time() or self.stopped.is_set():
                raise ValueError('Session expired.')
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, cwd=cwd or self.cwd,
                                    env=env, start_new_session=True)
            self.processes.add(proc)
            session['processes'].add(proc)
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        output = bytearray()
        truncated = timed_out = False
        stop = min(time.monotonic() + timeout,
                   time.monotonic() + session['expires'] - time.time())
        try:
            while True:
                if time.monotonic() >= stop or session.get('revoked') or self.stopped.is_set():
                    timed_out = True
                    kill_group(proc)
                    break
                if not selector.select(.1):
                    continue
                chunk = os.read(proc.stdout.fileno(), 8192)
                if not chunk:
                    break
                remaining = output_limit - len(output)
                output.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
                    kill_group(proc)
                    break
            # Kill ordinary background children too. A trusted admin can deliberately
            # escape the process group; this is not an OS sandbox.
            kill_group(proc)
            proc.wait(timeout=5)
        finally:
            kill_group(proc)
            proc.wait(timeout=5)
            selector.close()
            proc.stdout.close()
            with self.lock:
                self.processes.discard(proc)
                session['processes'].discard(proc)
        return dict(output=output.decode(errors='replace'), returncode=proc.returncode,
                    truncated=truncated, timed_out=timed_out, cwd=session['cwd'])

    def render(self, session, job, segments, crop):
        try:
            filters, concat = [], []
            for i, (start, end) in enumerate(segments):
                vf = f'[0:v:0]trim=start={start}:end={end},setpts=PTS-STARTPTS'
                if crop:
                    x, y, w, h = crop
                    vf += f',crop={w}:{h}:{x}:{y}'
                vf += f',scale=trunc(iw*sar/2)*2:trunc(ih/2)*2,setsar=1[v{i}]'
                filters.append(vf)
                concat.append(f'[v{i}]')
                if session['info']['audio']:
                    filters.append(f'[0:a:0]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]')
                    concat.append(f'[a{i}]')
            audio = session['info']['audio']
            filters.append(''.join(concat) + f'concat=n={len(segments)}:v=1:a={int(audio)}[v]' + ('[a]' if audio else ''))
            argv = ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                    '-i', session['path'], '-filter_complex_threads', '2',
                    '-filter_complex', ';'.join(filters), '-map', '[v]']
            if audio:
                argv += ['-map', '[a]', '-c:a', 'aac', '-b:a', '128k']
            argv += ['-c:v', 'libx264', '-threads', '2', '-preset', 'veryfast', '-crf', '23',
                     '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-fs', str(256 * 1024 * 1024), job['path']]
            result = self.execute(argv, session, EDIT_TIMEOUT)
            if result['returncode'] != 0 or result['truncated'] or result['timed_out']:
                raise ValueError('Render failed or exceeded the time/output limit. Try a shorter clip.')
            actual = media_info(job['path'])
            expected = sum(end - start for start, end in segments)
            if abs(actual['duration'] - expected) > .3 or os.path.getsize(job['path']) >= 256 * 1024 * 1024:
                raise ValueError('Output exceeded the size limit or is incomplete.')
            with self.lock:
                if session.get('revoked'):
                    raise ValueError('Session expired.')
                job['status'] = 'done'
        except Exception:
            job.update(status='failed', error='Render failed, expired or exceeded limits. Try a shorter clip.')
            try:
                os.unlink(job['path'])
            except FileNotFoundError:
                pass
        finally:
            self.worker.release()

    def handler(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            server_version = 'ReClip'
            sys_version = ''
            def setup(self):
                super().setup()
                self.connection.settimeout(10)
            def abort_read(self):
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            def handle_one_request(self):
                self.read_timer = threading.Timer(10, self.abort_read)
                self.read_timer.daemon = True
                self.read_timer.start()
                try:
                    super().handle_one_request()
                finally:
                    self.read_timer.cancel()
                    self.close_connection = True
            def parse_request(self):
                try:
                    return super().parse_request()
                finally:
                    self.read_timer.cancel()
            def log_message(self, *args):
                pass
            def send(self, code, body, content='application/json', headers=None):
                if not isinstance(body, bytes):
                    body = json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Type', content)
                self.send_header('Content-Length', str(len(body)))
                self.security_headers()
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(body)
            def security_headers(self):
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('X-Robots-Tag', 'noindex, nofollow')
                self.send_header('Content-Security-Policy', owner.csp)
                self.send_header('X-Frame-Options', 'DENY')
                self.send_header('Connection', 'close')
                self.send_header('Permissions-Policy', 'camera=(), microphone=(), geolocation=()')
            def body(self):
                if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise ValueError('JSON required.')
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384:
                    raise ValueError('Invalid request size.')
                timer = threading.Timer(10, self.abort_read)
                timer.daemon = True
                timer.start()
                try:
                    data = json.loads(self.rfile.read(length))
                finally:
                    timer.cancel()
                if not isinstance(data, dict):
                    raise ValueError('JSON object required.')
                return data
            def session(self):
                cookie = SimpleCookie()
                try:
                    cookie.load(self.headers.get('Cookie', ''))
                    sid = parse_qs(urlsplit(self.path).query).get('sid', [''])[0]
                    if not re.fullmatch(r'[a-f0-9]{24}', sid):
                        raise PermissionError('Missing session selector.')
                    key = cookie[COOKIE + '_' + sid].value
                except (KeyError, ValueError):
                    raise PermissionError('Authentication required.')
                with owner.lock:
                    session = owner.sessions.get(key)
                    if not session or session.get('sid') != sid or session['uid'] != owner.admin_id or not owner.admin_id or session['expires'] <= time.time() or session.get('revoked') or time.time() >= owner.deadline:
                        raise PermissionError('Session expired. Ask the bot for a new link.')
                    return key, session
            def origin_ok(self):
                parts = urlsplit(owner.origin)
                if not owner.origin or self.headers.get('Host') != parts.netloc:
                    raise PermissionError('Invalid host.')
                if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                    raise PermissionError('Cross-site request denied.')
                if self.command == 'POST' and self.headers.get('Origin') != owner.origin:
                    raise PermissionError('Invalid origin.')
                if self.command != 'POST' and self.headers.get('Origin') not in (None, owner.origin):
                    raise PermissionError('Invalid origin.')
            def meta(self, session):
                return {k: session[k] for k in ('csrf', 'kind', 'title', 'expires', 'sid')}
            def scoped(self, session, kind):
                if session['kind'] != kind:
                    raise PermissionError('Wrong session scope.')
            def do_HEAD(self):
                self.do_GET()
            def do_GET(self):
                try:
                    self.origin_ok()
                    url = urlsplit(self.path)
                    if url.path == '/':
                        # A public, inert login shell; no data until authenticated.
                        self.send(200, owner.html, 'text/html; charset=utf-8')
                        return
                    _, session = self.session()
                    if url.path == '/api/session':
                        self.send(200, self.meta(session))
                    elif url.path == '/api/specs':
                        self.scoped(session, 'terminal')
                        release = {}
                        try:
                            for line in Path('/etc/os-release').read_text().splitlines():
                                if '=' in line:
                                    k, v = line.split('=', 1)
                                    release[k] = v.strip('"')
                        except OSError:
                            pass
                        cpu = next((line.split(':', 1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines() if line.startswith('model name')), 'unknown')
                        memory = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:'))) / 1024 ** 2
                        disk = os.statvfs(session['cwd'])
                        self.send(200, dict(os=release.get('PRETTY_NAME', os.uname().sysname), cpu=cpu,
                            cores=os.cpu_count(), memory_gib=round(memory, 2),
                            disk_gib=round(disk.f_blocks * disk.f_frsize / 1024 ** 3, 2),
                            shell='/bin/bash (non-interactive)', cwd=session['cwd']))
                    elif url.path == '/api/media-info':
                        self.scoped(session, 'studio')
                        self.send(200, dict(session['info'], title=session['title']))
                    elif url.path == '/api/preview':
                        self.scoped(session, 'studio')
                        self.file(session['path'], False, session)
                    elif url.path in ('/api/edit', '/api/download'):
                        self.scoped(session, 'studio')
                        jid = parse_qs(url.query).get('id', [''])[0]
                        job = session['jobs'].get(jid)
                        if not job:
                            self.send(404, {'error': 'Unknown edit.'})
                        elif url.path == '/api/edit':
                            self.send(200, {k: v for k, v in job.items() if k != 'path'})
                        elif job['status'] != 'done':
                            self.send(409, {'error': 'Edited clip is not ready.'})
                        else:
                            self.file(job['path'], True, session)
                    else:
                        self.send(404, {'error': 'Not found.'})
                except PermissionError as exc:
                    self.send(403, {'error': str(exc)})
                except (ValueError, OSError) as exc:
                    self.send(400, {'error': 'Invalid or unavailable resource.'})
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def file(self, path, download, session):
                with open(path, 'rb') as fh:
                    total = os.fstat(fh.fileno()).st_size
                    start, end, code = 0, total - 1, 200
                    rng = self.headers.get('Range')
                    if rng:
                        match = re.fullmatch(r'bytes=(\d*)-(\d*)', rng)
                        if not match or not any(match.groups()):
                            self.send(416, {'error': 'Invalid range.'}, headers={'Content-Range': f'bytes */{total}'})
                            return
                        a, b = match.groups()
                        if a:
                            start, end = int(a), min(int(b), end) if b else end
                        else:
                            start = max(0, total - int(b))
                        if start > end or start >= total:
                            self.send(416, {'error': 'Invalid range.'}, headers={'Content-Range': f'bytes */{total}'})
                            return
                        code = 206
                    self.send_response(code)
                    self.send_header('Content-Type', 'video/mp4')
                    self.send_header('Content-Length', str(end - start + 1))
                    self.send_header('Accept-Ranges', 'bytes')
                    self.send_header('Content-Disposition', ('attachment' if download else 'inline') + '; filename="edited-clip.mp4"')
                    if code == 206:
                        self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
                    self.security_headers()
                    self.end_headers()
                    if self.command == 'HEAD':
                        return
                    fh.seek(start)
                    left = end - start + 1
                    while left and not session.get('revoked') and time.time() < session['expires']:
                        chunk = fh.read(min(65536, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            def do_POST(self):
                acquired = False
                try:
                    self.origin_ok()
                    path = urlsplit(self.path).path
                    if path == '/api/launch':
                        data = self.body()
                        token = data.get('token')
                        if not isinstance(token, str):
                            raise PermissionError('Invalid launch token.')
                        with owner.lock:
                            session = owner.launches.pop(token, None)
                            if not session or session['expires'] <= time.time() or session['uid'] != owner.admin_id or not owner.admin_id or time.time() >= owner.deadline:
                                if session:
                                    owner._dispose(session)
                                raise PermissionError('Launch link expired or already used.')
                            session['expires'] = min(time.time() + SESSION_TTL, owner.deadline)
                            session['csrf'] = secrets.token_urlsafe(32)
                            session['sid'] = secrets.token_hex(12)
                            key = secrets.token_urlsafe(32)
                            owner.sessions[key] = session
                        self.send(200, self.meta(session), headers={'Set-Cookie': f'{COOKIE}_{session["sid"]}={key}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={int(session["expires"] - time.time())}'})
                        return
                    key, session = self.session()
                    csrf = self.headers.get('X-CSRF-Token', '')
                    if not secrets.compare_digest(csrf, session['csrf']):
                        raise PermissionError('CSRF validation failed.')
                    data = self.body()
                    if path == '/api/logout':
                        with owner.lock:
                            owner.sessions.pop(key, None)
                            owner._dispose(session)
                        self.send(200, {'ok': True}, headers={'Set-Cookie': f'{COOKIE}_{session["sid"]}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0'})
                        return
                    if path in ('/api/command', '/api/cwd'):
                        self.scoped(session, 'terminal')
                    elif path == '/api/edit':
                        self.scoped(session, 'studio')
                    else:
                        self.send(404, {'error': 'Not found.'})
                        return
                    if not owner.worker.acquire(blocking=False):
                        self.send(429, {'error': 'Another admin task is running.'}, headers={'Retry-After': '2'})
                        return
                    acquired = True
                    if path == '/api/command':
                        command = data.get('command')
                        if not isinstance(command, str) or not command.strip() or len(command) > 4096 or '\x00' in command:
                            raise ValueError('Enter a command of 1 to 4096 characters.')
                        self.send(200, owner.execute(['/bin/bash', '--noprofile', '--norc', '-c', command], session, COMMAND_TIMEOUT, cwd=session['cwd']))
                    elif path == '/api/cwd':
                        value = data.get('path')
                        if not isinstance(value, str) or not value or len(value) > 4096 or '\x00' in value:
                            raise ValueError('Invalid directory.')
                        cwd = os.path.realpath(os.path.join(session['cwd'], value))
                        if not os.path.isdir(cwd):
                            raise ValueError('Directory does not exist.')
                        session['cwd'] = cwd
                        self.send(200, {'cwd': cwd})
                    else:
                        segments, crop = validate_edit(data, session['info'])
                        with owner.lock:
                            if len(session['jobs']) >= 3:
                                raise ValueError('Maximum 3 renders per session; request a fresh studio.')
                            jid = secrets.token_urlsafe(16)
                            job = {'status': 'running', 'path': os.path.join(owner.directory.name, jid + '.mp4')}
                            session['jobs'][jid] = job
                        threading.Thread(target=owner.render, args=(session, job, segments, crop), daemon=True).start()
                        acquired = False
                        self.send(202, {'job': jid})
                except PermissionError as exc:
                    self.send(403, {'error': str(exc)})
                except (ValueError, TypeError, OSError):
                    self.send(400, {'error': 'Invalid request. Check timestamps, crop, command or directory.'})
                finally:
                    if acquired:
                        owner.worker.release()
                    # Never reuse connections containing rejected/unread request bodies.
                    self.close_connection = True
        return Handler
