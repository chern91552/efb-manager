import json
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from efb_manager.history import History, install_recorder
from efb_manager.reminder import LoginReminder
from efb_manager.reminder_delivery import ReminderDelivery, ReminderSyncError


class PrivateChat:
    module_id = 'blueset.wechat'
    uid = 'filehelper'
    name = '文件传输助手'


class SelfChatMember:
    name = '我'


def message(uid=123, text='提醒'):
    return NS(chat=PrivateChat(), author=SelfChatMember(), type=NS(name='Text'),
              uid=json.dumps([[str(uid)]]), text=text, edit=False,
              deliver_to=NS(channel_id='blueset.telegram'))


class ReminderDeliveryTests(unittest.TestCase):
    def setup_delivery(self, history=None):
        original = Mock(side_effect=lambda msg: msg)
        coordinator = NS(send_message=original)
        if history:
            install_recorder(coordinator, history, 'default', Mock())
        delivery = ReminderDelivery(coordinator)
        delivery.build_message = Mock(return_value=message())
        return coordinator, delivery, original

    def test_send_once_archive_and_forward_once_with_late_echo(self):
        with tempfile.TemporaryDirectory() as root:
            history = History(Path(root)/'messages.sqlite3')
            coordinator, delivery, original = self.setup_delivery(history)
            bot = Mock(alive=True)
            bot.file_helper.send.return_value = NS(id=123)
            reminder = LoginReminder()
            reminder.login(0)
            callback = lambda sent, text: delivery.forward(NS(bot=bot), sent, text)
            reminder.tick(bot, True, False, {'enabled': True, 'hours': 1, 'message': '提醒'}, 3600, callback)
            coordinator.send_message(message())
            bot.file_helper.send.assert_called_once_with('提醒')
            original.assert_called_once()
            self.assertEqual(original.call_args.args[0].deliver_to.channel_id, 'blueset.telegram')
            self.assertEqual(history.stats('default')['messages'], 1)
            row = history.messages('default', History.key('blueset.wechat', 'filehelper'))['items'][0]
            self.assertEqual((row['body'], row['direction']), ('提醒', 'out'))

    def test_early_echo_wins_without_second_forward(self):
        coordinator, delivery, original = self.setup_delivery()
        coordinator.send_message(message())
        delivery.forward(Mock(), NS(id=123), '提醒')
        coordinator.send_message(message())
        original.assert_called_once()
        coordinator.send_message(message(456))
        self.assertEqual(original.call_count, 2)

    def test_edits_and_other_chat_messages_not_suppressed(self):
        coordinator, delivery, original = self.setup_delivery()
        delivery.forward(Mock(), NS(id=123), '提醒')
        edit = message()
        edit.edit = True
        coordinator.send_message(edit)
        other = message()
        other.chat = NS(module_id='blueset.wechat', uid='another-chat')
        coordinator.send_message(other)
        self.assertEqual(original.call_count, 3)

    def test_sync_failure_does_not_resend_wechat_and_is_archived(self):
        with tempfile.TemporaryDirectory() as root:
            history = History(Path(root)/'messages.sqlite3')
            coordinator, delivery, original = self.setup_delivery(history)
            original.side_effect = RuntimeError('TG unavailable')
            reminder, bot = LoginReminder(), Mock(alive=True)
            reminder.login(0)
            callback = lambda sent, text: delivery.forward(Mock(), sent, text)
            policy = {'enabled': True, 'hours': 1}
            with self.assertRaises(ReminderSyncError):
                reminder.tick(bot, True, False, policy, 3600, callback)
            self.assertFalse(reminder.tick(bot, True, False, policy, 3700, callback))
            bot.file_helper.send.assert_called_once()
            row = history.messages('default', History.key('blueset.wechat', 'filehelper'))['items'][0]
            self.assertEqual(row['delivery'], 'failed')
