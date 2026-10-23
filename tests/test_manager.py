from contextlib import nullcontext
import threading
import time
import unittest
from unittest.mock import patch

from efb_manager.runner import LoginController
from efb_manager.service import Service, redact


class FakeBot:
    alive = True
    def __init__(self, channel):
        self.channel = channel
    def logout(self):
        self.alive = False
        self.channel.exit_callback()


def channel_class():
    class Channel:
        def authenticate(self, qr_reload, first_start=False):
            self.bot = FakeBot(self)
            self.done = True
    return Channel


class ControllerTests(unittest.TestCase):
    def setUp(self):
        patcher = patch('efb_manager.runner.cached_login_then_wait', lambda _: nullcontext())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_first_login_automatically_restores_and_logout_stays_offline(self):
        cls = channel_class()
        controller = LoginController()
        controller.install(cls)
        channel = cls()
        with patch('efb_manager.runner.emit') as emit:
            worker = threading.Thread(target=channel.authenticate, kwargs={'first_start': True})
            worker.start()
            for _ in range(100):
                if controller.ready:
                    break
                time.sleep(.01)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertTrue(controller.online)
            with self.assertRaises(RuntimeError):
                controller.command('login')
            controller.command('logout')
            self.assertFalse(controller.online)
            self.assertFalse(channel.bot.alive)
            self.assertFalse(controller.gate.is_set())
            self.assertIn('offline', [call.args[0] for call in emit.call_args_list])
            channel.chats = object()
            controller.command('login')
            for _ in range(100):
                if not controller.busy:
                    break
                time.sleep(.01)
            self.assertTrue(controller.online)

    def test_login_failure_never_marks_online(self):
        cls = channel_class()
        def fail(*args, **kwargs):
            raise RuntimeError('authentication failed')
        cls.authenticate = fail
        controller = LoginController()
        controller.install(cls)
        channel = cls()
        with patch('efb_manager.runner.emit') as emit:
            with self.assertRaises(RuntimeError):
                channel.authenticate()
            self.assertFalse(controller.online)
            self.assertFalse(controller.busy)
            self.assertNotIn('online', [call.args[0] for call in emit.call_args_list])

    def test_concurrent_login_is_rejected(self):
        c = LoginController()
        c.channel = object()
        c.ready = True
        c.busy = True
        with self.assertRaises(RuntimeError):
            c.command('login')


class StateTests(unittest.TestCase):
    def test_duration_and_relogin(self):
        service = Service()
        with patch('efb_manager.service.time.time', return_value=110):
            service.event(dict(kind='online', at=100))
            self.assertEqual(service.snapshot()['online_seconds'], 10)
            service.event(dict(kind='offline'))
            self.assertEqual(service.snapshot()['last_duration'], 10)
            self.assertEqual(service.snapshot()['online_seconds'], 0)
            service.event(dict(kind='offline'))
            self.assertEqual(service.snapshot()['last_duration'], 10)
            service.event(dict(kind='online', at=108))
            self.assertEqual(service.snapshot()['online_seconds'], 2)

    def test_qr_is_cleared_after_login_and_failure(self):
        s = Service()
        s.event(dict(kind='qr', image='data:image/svg+xml;base64,abc'))
        self.assertIsNotNone(s.snapshot()['qr'])
        s.event(dict(kind='online', at=time.time()))
        self.assertIsNone(s.snapshot()['qr'])
        s.event(dict(kind='fatal', error='missing channel'))
        self.assertEqual(s.snapshot()['wechat'], 'offline')

    def test_token_redaction(self):
        text = '123456789:abcdefghijklmnopqrstuvwx https://login.weixin.qq.com/l/private'
        self.assertNotIn('private', redact(text))
        self.assertNotIn('abcdefghijkl', redact(text))


if __name__ == '__main__':
    unittest.main()


class ArchiveStateTests(unittest.TestCase):
    def test_recovered_clears_visible_error_and_retains_log(self):
        service = Service()
        service.event({'kind': 'archive_error', 'error': '数据库忙'})
        self.assertEqual(service.snapshot()['archive_error'], '数据库忙')
        service.event({'kind': 'archive_recovered'})
        self.assertIsNone(service.snapshot()['archive_error'])
        self.assertIn('数据库忙', service.snapshot()['logs'])
