import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest

from efb_manager.history import History, capture, install_recorder
from test_history import message, SelfChatMember


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('EFB_MANAGER_TEST_TMP'))
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'messages.sqlite3'
        self.history = History(self.path)

    def settings(self, **changes):
        result = dict(record_private=True, record_group=True, record_public=False, max_mb=100)
        result.update(changes)
        return result

    def test_public_classification_and_self_chat_exception(self):
        msg = message()
        msg.chat = NS(module_id='blueset.wechat', uid='public', display_name='公众号', vendor_specific={'is_mp':True})
        self.assertEqual(capture(msg,'default')['chat_type'],'Public')
        member=SelfChatMember()
        msg.chat.self=member
        msg.chat.other=member
        self.assertNotEqual(capture(msg,'default')['chat_type'],'Public')

    def test_each_category_can_be_disabled_without_affecting_forwarding(self):
        for category, setting in [('Private','record_private'),('Group','record_group'),('Public','record_public')]:
            self.history.update_settings(self.settings(**{setting:False}))
            item=capture(message(uid=category),'default')
            item['chat_type']=category
            self.assertFalse(self.history.save(item))
        calls=[]
        coordinator=NS(send_message=lambda m:calls.append(m) or m)
        self.history.update_settings(self.settings(record_group=False))
        install_recorder(coordinator,self.history,'default',lambda:None)
        msg=message()
        self.assertIs(coordinator.send_message(msg),msg)
        self.assertEqual(calls,[msg])
        self.assertEqual(self.history.stats('default')['messages'],0)

    def test_rules_persist_and_child_reads_new_policy(self):
        second=History(self.path)
        self.history.update_settings(self.settings(record_group=False))
        self.assertFalse(second.save(capture(message(),'default')))
        self.history.update_settings(self.settings(record_group=True))
        self.assertTrue(second.save(capture(message(),'default')))
        self.assertTrue(History(self.path).settings()['record_group'])

    def test_default_public_is_off_and_can_be_enabled(self):
        item=capture(message(),'default');item['chat_type']='Public'
        self.assertFalse(self.history.save(item))
        self.history.update_settings(self.settings(record_public=True))
        self.assertTrue(self.history.save(item))

    def test_disable_does_not_remove_existing_history(self):
        self.history.save(capture(message(),'default'))
        self.history.update_settings(self.settings(record_group=False))
        self.history.enforce_limit()
        self.assertEqual(self.history.stats('default')['messages'],1)

    def test_reject_invalid_limits_and_booleans(self):
        before=self.history.settings()
        for bad in [0,10241,True,'100',1.5]:
            with self.assertRaises(ValueError):
                self.history.update_settings(self.settings(max_mb=bad))
        with self.assertRaises(ValueError):
            self.history.update_settings(self.settings(record_private='false'))
        self.assertEqual(self.history.settings()['max_mb'],before['max_mb'])

    def test_limit_removes_oldest_and_physically_shrinks_file(self):
        # Begin above 1 MiB so lowering the limit triggers genuine disk compaction.
        for n in range(30):
            item=capture(message(body='A'*60000,uid=str(n)),'default')
            item['captured_at']=n
            self.history.save(item)
        before=self.history.disk_bytes()
        self.assertGreater(before,1024*1024)
        self.history.update_settings(self.settings(max_mb=1))
        removed=self.history.enforce_limit()
        self.assertGreater(removed,0)
        self.assertLess(self.history.stats('default')['messages'],30)
        self.assertLessEqual(self.history.disk_bytes(),1024*1024)
        items=self.history.messages('default',History.key('blueset.wechat','chat-1'),limit=100)['items']
        self.assertEqual(items[-1]['captured_at'],29)
        self.assertGreater(items[0]['captured_at'],0)
        self.assertEqual(self.history.settings()['total_removed'],removed)
        self.assertEqual(self.history.enforce_limit(),0)

    def test_single_oversized_record_can_be_cleaned(self):
        self.history.update_settings(self.settings(max_mb=1))
        self.history.save(capture(message(body='X'*2000000),'default'))
        self.assertEqual(self.history.enforce_limit(),1)
        self.assertEqual(self.history.stats('default')['messages'],0)
        self.assertLessEqual(self.history.disk_bytes(),1024*1024)
