import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from efb_manager.reminder import LoginReminder, ReminderSettings
from efb_manager.runner import LoginController


class ReminderTests(unittest.TestCase):
    def test_settings_persist_and_reject_invalid_values(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'settings.json'
            settings = ReminderSettings(path)
            self.assertFalse(settings.read()['enabled'])
            policy = {'enabled': True, 'hours': 0.01, 'message': '请重新登录微信\n这是自定义提醒'}
            settings.update(policy)
            self.assertEqual(ReminderSettings(path).read(), policy)
            for hours in [0, -1, True, '24', float('nan'), float('inf'), 8761]:
                with self.assertRaises(ValueError):
                    settings.update({'enabled': True, 'hours': hours})
            self.assertEqual(settings.read(), policy)

    def test_threshold_once_and_new_login(self):
        reminder, bot = LoginReminder(), Mock(alive=True)
        policy = {'enabled': True, 'hours': 1}
        reminder.login(100)
        self.assertFalse(reminder.tick(bot, True, False, policy, 3699))
        self.assertTrue(reminder.tick(bot, True, False, policy, 3700))
        self.assertFalse(reminder.tick(bot, True, False, policy, 7200))
        bot.file_helper.send.assert_called_once()
        reminder.login(8000)
        self.assertFalse(reminder.tick(bot, True, False, policy, 8001))
        self.assertTrue(reminder.tick(bot, True, False, policy, 11600))
        self.assertEqual(bot.file_helper.send.call_count, 2)

    def test_disabled_offline_busy_and_dead_skip(self):
        reminder, bot = LoginReminder(), Mock(alive=True)
        reminder.login(0)
        for enabled, online, busy, alive in [(False, True, False, True), (True, False, False, True),
                                           (True, True, True, True), (True, True, False, False)]:
            bot.alive = alive
            self.assertFalse(reminder.tick(bot, online, busy, {'enabled': enabled, 'hours': 1}, 4000))
        bot.file_helper.send.assert_not_called()
        bot.alive = True
        self.assertTrue(reminder.tick(bot, True, False, {'enabled': True, 'hours': 1}, 4005))

    def test_send_failure_is_reported_without_retry(self):
        controller = LoginController()
        bot = Mock(alive=True)
        bot.file_helper.send.side_effect = TimeoutError('private response')
        controller.channel = SimpleNamespace(bot=bot)
        controller.online = True
        controller.reminder.login(0)
        with patch('efb_manager.runner.time.monotonic', return_value=4000), patch('efb_manager.runner.emit') as emit:
            controller.check_reminder({'enabled': True, 'hours': 1})
            self.assertEqual(emit.call_args.args[0], 'reminder_error')
            self.assertNotIn('private response', emit.call_args.kwargs['message'])
            controller.check_reminder({'enabled': True, 'hours': 1})
            bot.file_helper.send.assert_called_once()

    def test_custom_message_sent_verbatim_and_blank_uses_default(self):
        for message in ['微信登录满 29 天了\n请检查 {原文保留}', '', '   ']:
            reminder, bot = LoginReminder(), Mock(alive=True)
            reminder.login(0)
            reminder.tick(bot, True, False, {'enabled': True, 'hours': 696, 'message': message}, 696*3600)
            sent = bot.file_helper.send.call_args.args[0]
            if message.strip():
                self.assertEqual(sent, message)
            else:
                self.assertIn('EFB Manager', sent)

    def test_old_settings_and_invalid_messages(self):
        from efb_manager.reminder import validate_settings
        self.assertEqual(validate_settings({'enabled': True, 'hours': 24})['message'], '')
        for message in [None, 123, 'x'*501, 'bad\x00text']:
            with self.assertRaises(ValueError):
                validate_settings({'enabled': True, 'hours': 24, 'message': message})
