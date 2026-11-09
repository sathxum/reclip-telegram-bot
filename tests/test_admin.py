import http.client
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import admin_server as web


class AdminHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.source = str(Path(cls.fixtures.name) / 'source.mp4')
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                        '-f', 'lavfi', '-i', 'testsrc2=size=160x120:rate=25:duration=4',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4',
                        '-c:v', 'libx264', '-threads', '2', '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', '-shortest', cls.source], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def setUp(self):
        self.deleted = []
        self.server = web.AdminServer(123, time.time() + 900, self.deleted.extend, port=0)
        self.server.start()
        self.port = self.server.httpd.server_address[1]
        self.server.origin = 'https://admin-test.trycloudflare.com'

    def tearDown(self):
        self.server.close()

    def request(self, path, data=None, cookie=None, csrf=None, origin=True, host=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        headers = {'Host': host or 'admin-test.trycloudflare.com'}
        if data is not None:
            headers['Content-Type'] = 'application/json'
            if origin:
                headers['Origin'] = self.server.origin if origin is True else origin
        if cookie:
            headers['Cookie'] = cookie
        if csrf:
            headers['X-CSRF-Token'] = csrf
        connection.request('POST' if data is not None else 'GET', path,
                           json.dumps(data) if data is not None else None, headers)
        response = connection.getresponse()
        body = response.read()
        result = (response.status, dict(response.getheaders()), body)
        connection.close()
        return result

    def authenticate(self, kind='terminal', path=None):
        launch = self.server.launch(123, kind, path, 'Fixture <title>')
        token = launch.split('#')[1]
        status, headers, body = self.request('/api/launch', {'token': token})
        self.assertEqual(status, 200, body)
        self.assertIn('Secure; HttpOnly; SameSite=Strict', headers['Set-Cookie'])
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        meta = json.loads(body)
        cookie = headers['Set-Cookie'].split(';')[0]
        return meta, cookie, token

    def api(self, path, meta, cookie, data=None, **kwargs):
        path += ('&' if '?' in path else '?') + 'sid=' + meta['sid']
        return self.request(path, data, cookie, meta['csrf'], **kwargs)

    def test_fixed_admin_required(self):
        with self.assertRaises(PermissionError):
            self.server.launch(456)
        other = web.AdminServer(0, time.time() + 100, lambda _: None, port=0)
        try:
            self.assertFalse(other.start())
            other.origin = self.server.origin
            with self.assertRaises(PermissionError):
                other.launch(123)
        finally:
            other.close()

    def test_unauthenticated_endpoints_denied(self):
        for path in ['/api/session', '/api/specs', '/api/preview', '/api/media-info',
                     '/api/edit?id=x', '/api/download?id=x']:
            self.assertEqual(self.request(path)[0], 403, path)
        for path in ['/api/command', '/api/cwd', '/api/edit', '/api/logout']:
            self.assertEqual(self.request(path, {})[0], 403, path)
        self.assertEqual(self.request('/api/launch', {'token': 'invalid'})[0], 403)
        self.assertEqual(self.request('/api/specs', host='evil.example')[0], 403)

    def test_launch_csrf_origin_scope_logout(self):
        meta, cookie, token = self.authenticate()
        self.assertEqual(self.request('/api/launch', {'token': token})[0], 403)
        path = '/api/command?sid=' + meta['sid']
        self.assertEqual(self.request(path, {'command': 'true'}, cookie)[0], 403)
        self.assertEqual(self.api('/api/command', meta, cookie, {'command': 'true'}, origin=False)[0], 403)
        self.assertEqual(self.api('/api/command', meta, cookie, {'command': 'true'}, origin='https://evil.example')[0], 403)
        self.assertEqual(self.api('/api/preview', meta, cookie)[0], 403)
        self.assertEqual(self.api('/api/logout', meta, cookie, {})[0], 200)
        self.assertEqual(self.api('/api/session', meta, cookie)[0], 403)

    def test_expired_launch_and_session(self):
        token = self.server.launch(123).split('#')[1]
        self.server.launches[token]['expires'] = time.time() - 1
        self.assertEqual(self.request('/api/launch', {'token': token})[0], 403)
        meta, cookie, _ = self.authenticate()
        next(iter(self.server.sessions.values()))['expires'] = time.time() - 1
        self.assertEqual(self.api('/api/specs', meta, cookie)[0], 403)

    def test_session_cookie_tab_isolation(self):
        terminal, cookie1, _ = self.authenticate()
        studio, cookie2, _ = self.authenticate('studio', self.source)
        self.assertNotEqual(cookie1.split('=')[0], cookie2.split('=')[0])
        both = cookie1 + '; ' + cookie2
        self.assertEqual(self.api('/api/specs', terminal, both)[0], 200)
        self.assertEqual(self.api('/api/media-info', studio, both)[0], 200)
        self.assertEqual(self.api('/api/command', studio, both, {'command': 'true'})[0], 403)
        self.assertEqual(self.api('/api/session', terminal, cookie2)[0], 403)

    def test_real_shell_cwd_environment_and_limits(self):
        meta, cookie, _ = self.authenticate()
        with patch.dict(os.environ, {'BOT_TOKEN': 'DO-NOT-INHERIT', 'GH_PAT': 'DO-NOT-INHERIT'}):
            status, _, body = self.api('/api/command', meta, cookie,
                                     {'command': 'printf "%s:%s" "${BOT_TOKEN-unset}" "${GH_PAT-unset}"; printf "\\n"; uname -s; echo stderr >&2; exit 7'})
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result['returncode'], 7)
        self.assertIn('unset:unset\nLinux\nstderr', result['output'])
        self.assertEqual(self.api('/api/cwd', meta, cookie, {'path': self.fixtures.name})[0], 200)
        result = json.loads(self.api('/api/command', meta, cookie, {'command': 'pwd'})[2])
        self.assertEqual(result['output'].strip(), self.fixtures.name)
        with patch.object(web, 'COMMAND_TIMEOUT', .2):
            result = json.loads(self.api('/api/command', meta, cookie, {'command': 'sleep 10'})[2])
            self.assertTrue(result['timed_out'])
        result = json.loads(self.api('/api/command', meta, cookie, {'command': 'yes X'})[2])
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['output'].encode()), web.OUTPUT_LIMIT)
        self.assertFalse(self.server.processes)
        for command in ['', '\x00', 'x' * 4097]:
            self.assertEqual(self.api('/api/command', meta, cookie, {'command': command})[0], 400)

    def test_concurrency_denied(self):
        meta, cookie, _ = self.authenticate()
        self.server.worker.acquire()
        try:
            status, headers, _ = self.api('/api/command', meta, cookie, {'command': 'true'})
            self.assertEqual(status, 429)
            self.assertEqual(headers['Retry-After'], '2')
        finally:
            self.server.worker.release()

    def test_edit_validation_and_range(self):
        meta, cookie, _ = self.authenticate('studio', self.source)
        cases = [{}, {'segments': []}, {'segments': [{'start': -1, 'end': 1}]},
                 {'segments': [{'start': 2, 'end': 1}]}, {'segments': [{'start': 0, 'end': 10}]},
                 {'segments': [{'start': float('nan'), 'end': 1}]},
                 {'segments': [{'start': True, 'end': 1}]},
                 {'segments': [{'start': 0, 'end': 2}, {'start': 1, 'end': 3}]},
                 {'segments': [{'start': 0, 'end': 1}], 'crop': {'x': 1, 'y': 0, 'width': 80, 'height': 60}},
                 {'segments': [{'start': 0, 'end': 1}], 'crop': {'x': 0, 'y': 0, 'width': 800, 'height': 60}}]
        for case in cases:
            self.assertEqual(self.api('/api/edit', meta, cookie, case)[0], 400, case)
        path = '/api/preview?sid=' + meta['sid']
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        conn.request('GET', path, headers={'Host': 'admin-test.trycloudflare.com', 'Cookie': cookie, 'Range': 'bytes=0-99'})
        response = conn.getresponse()
        self.assertEqual(response.status, 206)
        self.assertEqual(len(response.read()), 100)
        conn.close()

    def test_render_segments_crop_and_edited_only_download(self):
        owned = str(Path(self.fixtures.name) / ('owned-' + str(time.monotonic_ns()) + '.mp4'))
        shutil.copyfile(self.source, owned)
        meta, cookie, _ = self.authenticate('studio', owned)
        self.assertEqual(self.api('/api/download?id=unknown', meta, cookie)[0], 404)
        status, _, body = self.api('/api/edit', meta, cookie, {
            'segments': [{'start': .4, 'end': 1.2}, {'start': 2, 'end': 3.2}],
            'crop': {'x': 20, 'y': 10, 'width': 100, 'height': 80}})
        self.assertEqual(status, 202, body)
        jid = json.loads(body)['job']
        stop = time.monotonic() + 15
        while time.monotonic() < stop:
            result = json.loads(self.api('/api/edit?id=' + jid, meta, cookie)[2])
            if result['status'] != 'running':
                break
            time.sleep(.1)
        self.assertEqual(result['status'], 'done', result)
        status, headers, output = self.api('/api/download?id=' + jid, meta, cookie)
        self.assertEqual(status, 200)
        self.assertIn('attachment', headers['Content-Disposition'])
        target = str(Path(self.fixtures.name) / 'edited.mp4')
        Path(target).write_bytes(output)
        info = web.media_info(target)
        self.assertAlmostEqual(info['duration'], 2, delta=.15)
        self.assertEqual((info['width'], info['height']), (100, 80))
        self.assertTrue(info['audio'])
        output_path = next(iter(self.server.sessions.values()))['jobs'][jid]['path']
        self.api('/api/logout', meta, cookie, {})
        self.assertFalse(Path(output_path).exists())
        self.assertIn(owned, self.deleted)

    def test_silent_render_and_crop_geometry_validation(self):
        silent = str(Path(self.fixtures.name) / 'silent.mp4')
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                        '-i', self.source, '-an', '-c:v', 'copy', silent], check=True)
        meta, cookie, _ = self.authenticate('studio', silent)
        session = next(iter(self.server.sessions.values()))
        info = dict(session['info'], crop_safe=False)
        with self.assertRaises(ValueError):
            web.validate_edit({'segments': [{'start': 0, 'end': 1}],
                               'crop': {'x': 0, 'y': 0, 'width': 80, 'height': 60}}, info)
        status, _, body = self.api('/api/edit', meta, cookie,
                                  {'segments': [{'start': .2, 'end': 1.2}], 'crop': None})
        self.assertEqual(status, 202)
        jid = json.loads(body)['job']
        stop = time.monotonic() + 15
        while time.monotonic() < stop and session['jobs'][jid]['status'] == 'running':
            time.sleep(.1)
        self.assertEqual(session['jobs'][jid]['status'], 'done')
        info = web.media_info(session['jobs'][jid]['path'])
        self.assertFalse(info['audio'])
        self.assertAlmostEqual(info['duration'], 1, delta=.1)

    def test_close_revokes_and_terminates_running_command(self):
        meta, cookie, _ = self.authenticate()
        session = next(iter(self.server.sessions.values()))
        result = []
        thread = threading.Thread(target=lambda: result.append(self.server.execute(
            ['/bin/bash', '-c', 'sleep 20'], session, 30)))
        thread.start()
        stop = time.monotonic() + 2
        while not self.server.processes and time.monotonic() < stop:
            time.sleep(.01)
        self.assertTrue(self.server.processes)
        self.server.close()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.server.sessions)
        self.assertFalse(self.server.processes)


# Import does not start the bot; dummy token solely satisfies its import guard.
os.environ.setdefault('BOT_TOKEN', 'local-test-not-a-real-token')
import bot


class BotIntegrationTests(unittest.TestCase):
    def setUp(self):
        bot.JOBS.clear()
        bot.JOBS['job'] = {'chat_id': 123, 'url': 'https://example.invalid/video',
                           'info': {'title': 'Fixture', 'duration': 4}, 'msg_id': 1}
        self.admin = patch.object(bot, 'ADMIN_ID', 123)
        self.admin.start()
        self.messages = patch.object(bot, 'send_message', return_value={'result': {'message_id': 2}})
        self.send = self.messages.start()
        self.cb = patch.object(bot, 'answer_cb')
        self.cb.start()
        self.threads = patch.object(bot.threading, 'Thread')
        self.thread = self.threads.start()

    def tearDown(self):
        patch.stopall()
        bot.JOBS.clear()

    def callback(self, data, uid=123, chat=123):
        bot.handle_update({'callback_query': {'id': 'cb', 'from': {'id': uid},
            'data': data, 'message': {'chat': {'id': chat}, 'message_id': 1}}})

    def test_admin_choice_and_nonadmin_denial(self):
        self.callback('v:job:720')
        markup = self.send.call_args.args[2]['inline_keyboard'][0]
        self.assertEqual([b['callback_data'] for b in markup], ['t:job:720', 'u:job:720'])
        self.assertFalse(self.thread.called)
        self.callback('u:job:720', uid=456)
        self.assertFalse(self.thread.called)
        self.callback('t:job:720')
        self.assertEqual(self.thread.call_args.kwargs['args'][-1], 'telegram')

    def test_general_user_unchanged_and_forged_job_denied(self):
        bot.JOBS['job']['chat_id'] = 456
        self.callback('v:job:720', uid=456, chat=456)
        self.assertEqual(self.thread.call_args.kwargs['args'], (456, 'job', 'video', 720, None))
        self.thread.reset_mock()
        self.callback('u:job:720', uid=456, chat=456)
        self.assertFalse(self.thread.called)
        self.callback('v:job:720', uid=123, chat=123)
        self.assertFalse(self.thread.called)

    def test_auto_admin_has_no_shell(self):
        with patch.object(bot, 'ADMIN_ID', 0), patch.dict(bot.STATS, {'admin_id': 456}):
            self.assertTrue(bot.is_admin(456))
            self.assertFalse(bot.fixed_admin_private(456, 456))
            bot.handle_cmd(456, 456)
            self.assertIn('Fixed ADMIN_ID', self.send.call_args.args[1])
        self.assertFalse(bot.fixed_admin_private(123, -123))

    def test_small_general_delivery_and_admin_studio(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'fixture.mp4')
            Path(path).write_bytes(b'test')
            with patch.object(bot, 'run_download', return_value=(path, '')), \
                 patch.object(bot, 'send_media', return_value=(True, 4)) as send_media, \
                 patch.object(bot, 'edit_message'), patch.object(bot, 'chat_action'), \
                 patch.object(bot, 'api'), patch.object(bot, '_cleanup') as cleanup, \
                 patch.object(bot, 'admin_web') as server, patch.object(bot, 'create_share') as share:
                bot.JOBS['job']['chat_id'] = 456
                bot.deliver(456, 'job', 'video')
                send_media.assert_called_once()
                server.assert_not_called()
                send_media.reset_mock(); cleanup.reset_mock()
                bot.JOBS['job']['chat_id'] = 123
                server.return_value.launch.return_value = 'https://admin.invalid/#launch'
                bot.deliver(123, 'job', 'video', delivery='studio')
                server.return_value.launch.assert_called_once_with(123, 'studio', path, 'Fixture')
                send_media.assert_not_called(); share.assert_not_called(); cleanup.assert_not_called()
                bot._untrack(path)

    def test_public_share_does_not_expose_admin_api(self):
        self.threads.stop()
        server = bot.ThreadingHTTPServer(('127.0.0.1', 0), bot._ShareHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path in ['/api/command', '/api/preview', '/api/specs']:
                conn = http.client.HTTPConnection('127.0.0.1', server.server_address[1])
                conn.request('GET', path)
                response = conn.getresponse()
                self.assertEqual(response.status, 404)
                response.read(); conn.close()
        finally:
            server.shutdown(); server.server_close()


if __name__ == '__main__':
    unittest.main()
