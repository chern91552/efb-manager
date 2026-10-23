import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from efb_manager.history import History, PLACEHOLDERS, capture, install_recorder


class GroupChat:
    module_id = 'blueset.wechat'
    uid = 'chat-1'
    display_name = '工作群'


class SelfChatMember:
    display_name = '我'


def message(kind='Text', body='你好，https://example.test', uid='message-1'):
    return NS(chat=GroupChat(), author=NS(display_name='小林'), type=NS(name=kind),
              text=body, uid=uid, deliver_to=NS(channel_id='blueset.telegram'))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('EFB_MANAGER_TEST_TMP'))
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'messages.sqlite3'
        self.store = History(self.path)
        self.key = History.key('blueset.wechat', 'chat-1')

    def test_media_does_not_read_text_file_or_path(self):
        class Media:
            chat = GroupChat()
            author = NS(display_name='小林')
            uid = 'image-1'
            deliver_to = NS(channel_id='blueset.telegram')
            type = NS(name='Image')
            @property
            def text(self):
                raise AssertionError('must not read media caption')
            @property
            def file(self):
                raise AssertionError('must not read media file')
            @property
            def path(self):
                raise AssertionError('must not read media path')
        item = capture(Media(), 'default')
        self.assertEqual(item['body'], '[图片]')
        self.store.save(item)
        result = self.store.messages('default', self.key)['items'][0]
        self.assertEqual(result['body'], '[图片]')
        self.assertEqual(set(result), {'id','sender','direction','kind','body','captured_at','delivery'})

    def test_storage_enforces_all_placeholders_and_text_only_search(self):
        for n, kind in enumerate(PLACEHOLDERS):
            if kind == 'Status':
                continue
            item = capture(message(kind, uid=str(n)), 'default')
            item['body'] = 'secret media caption or URL'
            self.store.save(item)
        items = self.store.messages('default', self.key)['items']
        for item in items:
            self.assertEqual(item['body'], PLACEHOLDERS[item['kind']])
        self.assertEqual(self.store.messages('default', self.key, query='secret')['items'], [])
        self.assertEqual(self.store.messages('default', self.key, query='图片')['items'], [])

    def test_text_persistence_profile_isolation_and_dedup(self):
        item = capture(message(), 'default')
        self.store.save(item)
        item.update(body='更新的文字', delivery='forwarded')
        self.store.save(item)
        other = dict(item, profile='other')
        self.store.save(other)
        reopened = History(self.path)
        self.assertEqual(reopened.stats('default'), dict(messages=1, conversations=1))
        self.assertEqual(reopened.messages('default', self.key)['items'][0]['body'], '更新的文字')
        self.assertEqual(reopened.messages('default', self.key, query='更新')['items'][0]['delivery'], 'forwarded')
        self.assertEqual(reopened.stats('missing')['messages'], 0)
        self.assertEqual(reopened.conversations('default', '工作')[0]['chat_type'], 'Group')

    def test_pagination_has_no_overlap_and_filters(self):
        for n in range(125):
            self.store.save(capture(message(uid=str(n)), 'default'))
        first = self.store.messages('default', self.key)
        second = self.store.messages('default', self.key, before=first['next_before'])
        third = self.store.messages('default', self.key, before=second['next_before'])
        ids = [m['id'] for page in (first, second, third) for m in page['items']]
        self.assertEqual(len(set(ids)), 125)
        self.assertIsNone(third['next_before'])
        self.assertEqual(self.store.messages('default', self.key, kind='other')['items'], [])
        self.assertEqual(len(self.store.messages('default', self.key, limit=500)['items']), 100)

    def test_recorder_preserves_original_message_and_return(self):
        msg = message(body='原始文字')
        payload = object()
        def original(m):
            self.assertIs(m, msg)
            m.text = '中间件改写的文字'
            return payload
        coordinator = NS(send_message=original)
        error = Mock()
        install_recorder(coordinator, self.store, 'default', error)
        self.assertIs(coordinator.send_message(msg), payload)
        item = self.store.messages('default', self.key)['items'][0]
        self.assertEqual(item['body'], '原始文字')
        self.assertEqual(item['delivery'], 'forwarded')
        error.assert_not_called()

    def test_failure_and_missing_result_are_distinguished(self):
        def original(m):
            raise RuntimeError('upstream failed')
        coordinator = NS(send_message=original)
        install_recorder(coordinator, self.store, 'default', Mock())
        with self.assertRaisesRegex(RuntimeError, 'upstream failed'):
            coordinator.send_message(message())
        self.assertEqual(self.store.messages('default', self.key)['items'][0]['delivery'], 'failed')
        coordinator = NS(send_message=lambda m: None)
        install_recorder(coordinator, self.store, 'default', Mock())
        coordinator.send_message(message(uid='filtered'))
        self.assertEqual(self.store.messages('default', self.key)['items'][-1]['delivery'], 'no_result')

    def test_database_failure_does_not_break_forwarding(self):
        broken = Mock()
        broken.save.side_effect = OSError('disk full')
        error, original = Mock(), Mock(return_value='result')
        coordinator = NS(send_message=original)
        install_recorder(coordinator, broken, 'default', error)
        self.assertEqual(coordinator.send_message(message()), 'result')
        original.assert_called_once()
        self.assertTrue(error.called)

    def test_direction_and_excluded_channels(self):
        msg = message()
        self.assertEqual(capture(msg, 'default')['direction'], 'in')
        msg.author = SelfChatMember()
        self.assertEqual(capture(msg, 'default')['direction'], 'out')
        msg.author = NS(display_name='我')
        msg.deliver_to.channel_id = 'blueset.wechat'
        self.assertEqual(capture(msg, 'default')['direction'], 'out')
        msg.chat = NS(module_id='other.channel', uid='chat')
        self.assertIsNone(capture(msg, 'default'))
        msg.chat = NS(module_id='blueset.wechat', uid='__ews_user_auth__')
        self.assertIsNone(capture(msg, 'default'))


class DeleteTests(unittest.TestCase):
    def test_delete_is_scoped_and_late_updates_do_not_restore_records(self):
        with tempfile.TemporaryDirectory() as root:
            history = History(Path(root)/'messages.sqlite3')
            item = capture(message(uid='delete-me'), 'default')
            history.save(item)
            history.save(dict(item, profile='other'))
            history.save(dict(item, chat_id='another'))
            key = history.key(item['channel'], item['chat_id'])
            record = history.messages('default', key)['items'][0]
            self.assertEqual(history.delete_records('default', history.key(item['channel'], 'another'), record['id']), 0)
            self.assertEqual(history.delete_records('default', key, record['id']), 1)
            self.assertFalse(history.save(dict(item, delivery='forwarded')))
            self.assertEqual(history.stats('other')['messages'], 1)
            self.assertEqual(history.stats('default')['messages'], 1)

    def test_clear_conversation_allows_future_messages(self):
        with tempfile.TemporaryDirectory() as root:
            history = History(Path(root)/'messages.sqlite3')
            item = capture(message(), 'default')
            history.save(item)
            key = history.key(item['channel'], item['chat_id'])
            self.assertEqual(history.delete_records('default', key), 1)
            self.assertFalse(history.save(dict(item, message_id='in-flight')))
            self.assertTrue(history.save(capture(message(uid='new-message'), 'default')))
            self.assertEqual(history.stats('default')['messages'], 1)
            for invalid in [None, 'bad', '{}', '["only-one"]']:
                with self.assertRaises(ValueError):
                    history.delete_records('default', invalid)

    def test_stale_browser_cannot_delete_a_reused_row_id(self):
        with tempfile.TemporaryDirectory() as root:
            history = History(Path(root)/'messages.sqlite3')
            item = capture(message(), 'default')
            history.save(item)
            key = history.key(item['channel'], item['chat_id'])
            row = history.messages('default', key)['items'][0]
            history.delete_records('default', key, row['id'], row['captured_at'])
            fresh = capture(message(uid='fresh'), 'default')
            history.save(fresh)
            self.assertEqual(history.delete_records('default', key, row['id'], row['captured_at']), 0)
            self.assertEqual(history.stats('default')['messages'], 1)
