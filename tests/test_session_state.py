import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from efb_manager.session_state import SessionState, fingerprint
from efb_manager.runner import LoginController


class SessionTests(unittest.TestCase):
    def test_restore_continues_age_and_reminder_attempt_across_restart(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'session.json'
            first = SessionState(path)
            value = first.login('same-session-hash', False, 100)
            self.assertTrue(value['origin_known'])
            first.mark_attempted()
            second = SessionState(path)
            value = second.login('same-session-hash', True, 100 + 29*86400)
            self.assertEqual(value['started_at'], 100)
            self.assertTrue(value['attempted'])
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            value = second.login('same-session-hash', False, 3000000)
            self.assertEqual(value['started_at'], 3000000)
            self.assertFalse(value['attempted'])

    def test_session_change_and_unknown_first_restore(self):
        with tempfile.TemporaryDirectory() as root:
            state = SessionState(Path(root)/'session.json')
            self.assertFalse(state.login('one', True, 100)['origin_known'])
            self.assertEqual(state.login('two', True, 200)['started_at'], 200)
            self.assertEqual(state.login('two', True, 250)['started_at'], 200)

    def test_only_session_fingerprint_is_stored(self):
        bot = NS(core=NS(loginInfo={'wxuin': 'private-uin', 'wxsid': 'private-cookie'}))
        identity = fingerprint(bot)
        self.assertNotIn('private', identity)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'state.json'
            SessionState(path).login(identity, False, 100)
            self.assertNotIn('private', path.read_text())
            path.write_text('{bad json')
            self.assertEqual(SessionState(path).login(identity, True, 200)['started_at'], 200)

    def test_controller_restores_timer_and_does_not_resend_attempted_reminder(self):
        with tempfile.TemporaryDirectory() as root:
            bot = NS(alive=True, core=NS(loginInfo={'wxuin': 'u', 'wxsid': 'sid'}))
            state = SessionState(Path(root)/'session.json')
            state.login(fingerprint(bot), False, 100)
            state.mark_attempted()
            controller = LoginController()
            controller.session_state = SessionState(state.path)
            with patch('efb_manager.runner.time.time', return_value=100+29*86400), \
                 patch('efb_manager.runner.time.monotonic', return_value=1000), patch('efb_manager.runner.emit'):
                self.assertEqual(controller.session_login(bot, True), 100)
            self.assertEqual(1000-controller.reminder.started, 29*86400)
            self.assertTrue(controller.reminder.attempted)
