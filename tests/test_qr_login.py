from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import Mock, patch

from efb_manager.qr_login import fresh_qr_login
from efb_manager.runner import LoginController


class FreshQRTests(unittest.TestCase):
    def test_both_shortcuts_are_skipped_and_restored(self):
        push = Mock(return_value='push-uuid')
        restore = Mock(return_value=True)
        module = SimpleNamespace(push_login=push)
        core_class = type('Core', (), {'load_login_status': restore})
        core = core_class()
        cookies = {'wxuin': 'fake-uin'}
        core.cookies = cookies
        with fresh_qr_login(module, core_class):
            self.assertFalse(module.push_login(core))
            self.assertFalse(core.load_login_status('existing-cache', loginCallback=Mock()))
            push.assert_not_called()
            restore.assert_not_called()
            self.assertIs(core.cookies, cookies)
        self.assertIs(module.push_login, push)
        self.assertIs(core_class.load_login_status, restore)

    def test_failure_restores_original_methods(self):
        push, restore = Mock(), Mock()
        module = SimpleNamespace(push_login=push)
        core_class = type('Core', (), {'load_login_status': restore})
        with self.assertRaises(RuntimeError):
            with fresh_qr_login(module, core_class):
                raise RuntimeError('failed login')
        self.assertIs(module.push_login, push)
        self.assertIs(core_class.load_login_status, restore)

    def test_qr_command_applies_to_first_login_and_relogin_only(self):
        active = []
        modes = []
        @contextmanager
        def context():
            active.append(True)
            try:
                yield
            finally:
                active.pop()
        class Channel:
            def authenticate(self, qr_reload, first_start=False):
                modes.append(bool(active))
                self.bot = SimpleNamespace(alive=True)
        controller = LoginController()
        controller.install(Channel)
        channel = Channel()
        with patch('efb_manager.runner.fresh_qr_login', context), patch('efb_manager.runner.cached_login_then_wait', lambda _: nullcontext()), patch('efb_manager.runner.emit'):
            worker = threading.Thread(target=channel.authenticate, kwargs={'first_start': True})
            worker.start()
            for _ in range(100):
                if controller.ready:
                    break
                time.sleep(.01)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(modes, [False])
            self.assertFalse(controller.force_qr)
            with self.assertRaises(RuntimeError):
                controller.command('login_qr')
            channel.chats = object()
            for action, expected in [('login', False), ('login_qr', True)]:
                controller.online = False
                controller.command(action)
                for _ in range(100):
                    if not controller.busy:
                        break
                    time.sleep(.01)
                self.assertEqual(modes[-1], expected)
                self.assertFalse(controller.force_qr)
        self.assertEqual(active, [])


class CachedLoginTests(unittest.TestCase):
    def test_cache_success_does_not_wait_or_request_qr(self):
        from efb_manager.qr_login import cached_login_then_wait
        restore, login, push, wait = Mock(return_value=True), Mock(), Mock(), Mock()
        core_class = type('Core', (), {'load_login_status': restore, 'login': login})
        module = SimpleNamespace(push_login=push)
        with cached_login_then_wait(wait, module, core_class):
            core = core_class()
            if not core.load_login_status('wxpy.pkl'):
                core.login()
        wait.assert_not_called()
        login.assert_not_called()
        self.assertIs(core_class.login, login)
        self.assertIs(core_class.load_login_status, restore)
        self.assertIs(module.push_login, push)

    def test_cache_failure_waits_for_qr_click_in_same_authentication(self):
        from efb_manager.qr_login import cached_login_then_wait
        for fail in (False, ValueError('invalid cache')):
            with self.subTest(fail=fail):
                restore = Mock(return_value=False)
                if fail:
                    restore.side_effect = fail
                login, push = Mock(), Mock()
                core_class = type('Core', (), {'load_login_status': restore, 'login': login})
                module = SimpleNamespace(push_login=push)
                controller = LoginController()
                def context(wait):
                    return cached_login_then_wait(wait, module, core_class)
                class Channel:
                    def authenticate(self, qr_reload, first_start=False):
                        core = core_class()
                        if not core.load_login_status('wxpy.pkl'):
                            core.login()
                        self.bot = SimpleNamespace(alive=True)
                controller.install(Channel)
                channel = Channel()
                with patch('efb_manager.runner.cached_login_then_wait', context), patch('efb_manager.runner.emit') as emit:
                    worker = threading.Thread(target=channel.authenticate, kwargs={'first_start': True})
                    worker.start()
                    try:
                        for _ in range(100):
                            if controller.waiting_for_scan:
                                break
                            time.sleep(.01)
                        self.assertTrue(controller.waiting_for_scan)
                        self.assertFalse(controller.online)
                        login.assert_not_called()
                        push.assert_not_called()
                        self.assertIn('awaiting_scan', [call.args[0] for call in emit.call_args_list])
                        with self.assertRaises(RuntimeError):
                            controller.command('login')
                        controller.command('login_qr')
                    finally:
                        controller.gate.set()
                        worker.join(2)
                    self.assertFalse(worker.is_alive())
                    login.assert_called_once()
                    self.assertTrue(controller.online)
                    self.assertFalse(controller.busy)
                    self.assertIs(core_class.load_login_status, restore)
