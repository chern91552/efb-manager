from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock
from efb_manager.history import History, capture, install_recorder
from test_history import message


class BlacklistTests(unittest.TestCase):
    def test_profile_isolation_restore_and_existing_history(self):
        with tempfile.TemporaryDirectory() as root:
            history = History(Path(root)/'db.sqlite3')
            msg = message()
            key = history.key(msg.chat.module_id, msg.chat.uid)
            history.save(capture(msg, 'default'))
            history.set_blacklist('default', key, True, '工作群')
            self.assertFalse(history.save(capture(message(uid='blocked'), 'default')))
            self.assertTrue(history.save(capture(message(uid='other-profile'), 'other')))
            self.assertEqual(history.stats('default')['messages'], 1)
            self.assertEqual(History(history.path).blacklist('default')[0]['name'], '工作群')
            history.set_blacklist('default', key, False)
            self.assertTrue(history.save(capture(message(uid='restored'), 'default')))
            self.assertEqual(history.stats('default')['messages'], 2)

    def test_blacklist_does_not_interrupt_forwarding_or_report_archive_error(self):
        with tempfile.TemporaryDirectory() as root:
            history = History(Path(root)/'db.sqlite3')
            msg = message()
            key = history.key(msg.chat.module_id, msg.chat.uid)
            history.set_blacklist('default', key, True)
            original, error = Mock(return_value=msg), Mock()
            coordinator = NS(send_message=original)
            install_recorder(coordinator, history, 'default', error)
            coordinator.send_message(msg)
            original.assert_called_once()
            error.assert_not_called()
            self.assertEqual(history.stats('default')['messages'], 0)

    def test_blacklisting_inflight_message_blocks_final_write(self):
        with tempfile.TemporaryDirectory() as root:
            history = History(Path(root)/'db.sqlite3')
            msg = message()
            key = history.key(msg.chat.module_id, msg.chat.uid)
            def original(msg):
                history.set_blacklist('default', key, True)
                return msg
            coordinator = NS(send_message=original)
            install_recorder(coordinator, history, 'default', Mock())
            coordinator.send_message(msg)
            self.assertEqual(history.messages('default', key)['items'][0]['delivery'], 'pending')
