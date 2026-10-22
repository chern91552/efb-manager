import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from efb_manager.access import Access, validate_password, write_password
from efb_manager.server import main


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('EFB_MANAGER_TEST_TMP'))
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'access-token'

    def test_custom_password_and_persistence(self):
        write_password(self.path, '自定义管理密码123')
        access = Access(self.path)
        self.assertTrue(access.login('自定义管理密码123'))
        self.assertIsNone(access.login('wrong'))
        self.assertFalse(access.authorized('错误会话'))
        if os.name == 'posix':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_change_rotates_sessions_and_persists(self):
        access = Access(self.path)
        old_password = self.path.read_text()
        old_session = access.login(old_password)
        new_session = access.change(old_password, 'custom-new-password')
        self.assertFalse(access.authorized(old_session))
        self.assertTrue(access.authorized(new_session))
        self.assertIsNone(access.login(old_password))
        self.assertTrue(Access(self.path).login('custom-new-password'))

    def test_invalid_or_wrong_password_cannot_change_file(self):
        access = Access(self.path)
        before = self.path.read_bytes()
        for value in ('short', ' abcdefgh', 'abcdefgh\n', 'a'*257, None):
            with self.assertRaises(ValueError):
                access.change(before.decode(), value)
            self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(ValueError):
            access.change('wrong', 'valid-password')
        self.assertEqual(self.path.read_bytes(), before)

    def test_disk_failure_keeps_existing_password_and_session(self):
        access = Access(self.path)
        password = self.path.read_text()
        session = access.login(password)
        with patch('efb_manager.access.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                access.change(password, 'another-password')
        self.assertEqual(self.path.read_text(), password)
        self.assertTrue(access.authorized(session))
        self.assertEqual(list(self.path.parent.glob('.password-*')), [])

    def test_cli_sets_password_without_starting_server(self):
        with patch('sys.argv', ['efb-manager', '--state-dir', self.tmp.name, '--set-password']), \
             patch('getpass.getpass', side_effect=['custom-cli-password', 'custom-cli-password']), \
             patch('builtins.print'), patch('efb_manager.server.ThreadingHTTPServer') as server:
            main()
        server.assert_not_called()
        self.assertEqual(self.path.read_text(), 'custom-cli-password')

    def test_cli_mismatch_keeps_existing_file(self):
        write_password(self.path, 'original-password')
        with patch('sys.argv', ['efb-manager', '--state-dir', self.tmp.name, '--set-password']), \
             patch('getpass.getpass', side_effect=['new-password', 'different-password']), \
             patch('sys.stderr'):
            with self.assertRaises(SystemExit):
                main()
        self.assertEqual(self.path.read_text(), 'original-password')
