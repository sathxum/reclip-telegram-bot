"""No external HTTP: credential logging and blocked-probe lifecycle regressions."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import admin_server as web

os.environ.setdefault('BOT_TOKEN', 'local-test-not-a-real-token')
import bot


class CredentialLoggingTests(unittest.TestCase):
    def test_http_transport_exception_logs_exclude_credentials_and_urls(self):
        token = 'SYNTHETIC-TELEGRAM-TOKEN-NEVER-LOG'
        pat = 'SYNTHETIC-GITHUB-TOKEN-NEVER-LOG'
        url = 'https://api.telegram.org/bot' + token + '/sendMessage'
        error = bot.requests.exceptions.ConnectionError(
            'Transport failure at ' + url + '?access_token=' + pat)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
             patch.object(bot, 'API', 'https://api.telegram.org/bot' + token), \
             patch.object(bot, 'GH_PAT', pat), patch.object(bot, 'GH_REPO', 'local/test'), \
             patch.object(bot.requests, 'post', side_effect=error) as post, \
             patch.object(bot.requests, 'get', side_effect=error) as get, \
             patch.object(bot.requests, 'put', side_effect=error), \
             patch.object(bot.time, 'sleep'):
            self.assertEqual(bot.api('sendMessage'), {'ok': False})
            self.assertEqual(post.call_count, 3)
            self.assertIsNone(bot._http_get(url))
            self.assertFalse(bot._ensure_state_branch())
            with patch.object(bot, '_ensure_state_branch', return_value=False):
                bot.stats_load()
            bot.stats_save()
            self.assertFalse(bot.trigger_rerun())
            self.assertGreaterEqual(get.call_count, 3)
        logged = output.getvalue()
        for sensitive in (token, pat, url, 'https://', str(error)):
            self.assertNotIn(sensitive, logged)
        for prefix in ('api-exc sendMessage', 'get err', 'state: branch err',
                       'state: load err', 'state: save err', 'rerun exc'):
            self.assertIn(prefix, logged)
        self.assertIn('ConnectionError', logged)


    def test_streamed_download_exception_logs_only_class(self):
        token = 'SYNTHETIC-STREAM-TOKEN-NEVER-LOG'
        error = bot.requests.exceptions.ChunkedEncodingError(
            'Read failed: https://example.invalid/?token=' + token)
        response = Mock()
        response.iter_content.side_effect = error
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(output), \
             patch.object(bot, '_http_get', return_value=response):
            self.assertFalse(bot._download('https://example.invalid', str(Path(directory) / 'download')))
        self.assertIn('save err ChunkedEncodingError', output.getvalue())
        self.assertNotIn(token, output.getvalue())
        self.assertNotIn('https://', output.getvalue())


class LaunchLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.files = tempfile.TemporaryDirectory()
        self.source = Path(self.files.name) / 'source.mp4'
        self.source.write_bytes(b'caller-owned fixture')
        self.cleaned = []
        self.server = web.AdminServer(123, time.time() + 900, self.cleanup)
        self.server.origin = 'https://old.trycloudflare.com'

    def cleanup(self, paths):
        self.cleaned.extend(paths)
        for path in paths:
            Path(path).unlink(missing_ok=True)

    def tearDown(self):
        self.server.close()
        self.files.cleanup()

    def blocked_launch(self, transition):
        entered, release = threading.Event(), threading.Event()
        result = {}
        info = {'duration': 4, 'width': 160, 'height': 120, 'audio': False}

        def probe(path):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Test probe was not released.')
            return info

        def launch():
            try:
                result['url'] = self.server.launch(123, 'studio', str(self.source))
            except Exception as exc:
                result['error'] = exc

        with patch.object(web, 'media_info', side_effect=probe):
            thread = threading.Thread(target=launch)
            thread.start()
            try:
                self.assertTrue(entered.wait(2), 'Launch did not reach metadata probe.')
                transition()
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertNotIn('url', result, 'Obsolete probe minted a new capability.')
        self.assertIsInstance(result.get('error'), ValueError)
        self.assertFalse(self.server.launches)
        self.assertFalse(self.server.sessions)
        # Failed launch never transfers ownership; caller must still clean it up.
        self.assertTrue(self.source.exists())
        self.assertNotIn(str(self.source), self.cleaned)
        self.cleanup([str(self.source)])
        self.assertFalse(self.source.exists())

    def test_shutdown_during_blocked_probe_rejects_launch(self):
        self.blocked_launch(self.server.close)

    def test_origin_replacement_during_blocked_probe_rejects_launch(self):
        def replace():
            with self.server.lock:
                self.server.origin = 'https://new.trycloudflare.com'
        self.blocked_launch(replace)

    def test_actual_tunnel_replacement_during_probe_rejects_launch(self):
        def replace():
            proc = Mock()
            proc.poll.return_value = None
            # Reusing the hostname still starts a new lifecycle generation.
            proc.stdout = io.BytesIO(b'https://old.trycloudflare.com\n')
            with patch.object(web.subprocess, 'Popen', return_value=proc) as popen:
                self.assertTrue(self.server.ensure_tunnel('mock-cloudflared'))
            popen.assert_called_once()
            self.assertEqual(self.server.origin, 'https://old.trycloudflare.com')
        self.blocked_launch(replace)
        # A fresh launch after replacement is permitted and uses the new generation.
        url = self.server.launch(123)
        self.assertTrue(url.startswith('https://old.trycloudflare.com/#'))
        self.assertEqual(len(self.server.launches), 1)

    def test_shutdown_prevents_late_tunnel_origin_publication(self):
        entered, release = threading.Event(), threading.Event()
        result = {}
        class BlockedOutput:
            def __iter__(self):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('Test tunnel output not released.')
                yield b'https://late.trycloudflare.com\n'
            def close(self):
                pass
        proc = Mock()
        proc.poll.return_value = None
        proc.stdout = BlockedOutput()
        with patch.object(web.subprocess, 'Popen', return_value=proc):
            thread = threading.Thread(target=lambda: result.update(
                ready=self.server.ensure_tunnel('mock-cloudflared')))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.server.close()
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(result['ready'])
        self.assertEqual(self.server.origin, '')
        self.assertFalse(self.server.launches)
        proc.terminate.assert_called()

    def test_deadline_during_blocked_probe_rejects_launch(self):
        def expire():
            with self.server.lock:
                self.server.deadline = time.time() - 1
        self.blocked_launch(expire)


class StudioFailureCleanupTests(unittest.TestCase):
    def test_failed_launch_cleans_caller_owned_download(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'source.mp4')
            Path(path).write_bytes(b'fixture')
            job = {'chat_id': 123, 'url': 'https://example.invalid/video',
                   'info': {'title': 'Fixture', 'duration': 4}, 'msg_id': 1}
            with patch.object(bot, 'ADMIN_ID', 123), patch.dict(bot.JOBS, {'cleanup-job': job}), \
                 patch.object(bot, 'run_download', return_value=(path, '')), \
                 patch.object(bot, 'send_message', return_value={'result': {'message_id': 2}}), \
                 patch.object(bot, 'edit_message'), patch.object(bot, 'chat_action'), \
                 patch.object(bot, 'stats_bump'), patch.object(bot, 'admin_web') as server:
                server.return_value.launch.side_effect = ValueError('Lifecycle changed during probe.')
                bot.deliver(123, 'cleanup-job', 'video', delivery='studio')
            self.assertFalse(Path(path).exists())
            self.assertNotIn(path, bot.ACTIVE)


if __name__ == '__main__':
    unittest.main()
