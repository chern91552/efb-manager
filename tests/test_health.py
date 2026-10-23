from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch
from efb_manager.health import install_wechat_health
from efb_manager.service import Service


class HealthTests(unittest.TestCase):
    def test_wechat_sync_success_and_error_are_observed_without_contents(self):
        original = Mock(side_effect=['0', None, RuntimeError('private server response')])
        module, core = NS(sync_check=original), type('Core', (), {})
        emit = Mock()
        install_wechat_health(emit, module, core)
        self.assertEqual(module.sync_check(object()), '0')
        self.assertEqual(emit.call_args.kwargs['status'], 'ok')
        self.assertIsNone(core().sync_check())
        self.assertEqual(emit.call_args.kwargs['status'], 'error')
        with self.assertRaises(RuntimeError):
            module.sync_check(object())
        self.assertNotIn('private server response', str(emit.call_args_list))

    def test_components_are_independent_and_stale_is_not_healthy(self):
        service = Service()
        service.state.update(process='running', wechat='online')
        service.event({'kind':'health','component':'wechat','status':'received','at':100})
        service.event({'kind':'health','component':'telegram','status':'retrying','retry':True,'error':'NetworkError'})
        service.event({'kind':'archive_error','error':'database locked'})
        with patch('efb_manager.service.time.time', return_value=101):
            health = service.snapshot()['health']
        self.assertEqual(health['wechat']['status'], 'ok')
        self.assertEqual(health['telegram']['retries'], 1)
        self.assertEqual(health['archive']['status'], 'error')
        with patch('efb_manager.service.time.time', return_value=300):
            self.assertEqual(service.snapshot()['health']['wechat']['status'], 'stale')
        service.event({'kind':'offline'})
        self.assertEqual(service.snapshot()['health']['wechat']['status'], 'offline')

    def test_successful_api_probe_does_not_hide_failed_delivery(self):
        service = Service()
        service.state.update(process='running', wechat='online')
        service.event({'kind':'telegram_delivery','message_key':'one','status':'uncertain'})
        service.event({'kind':'health','component':'telegram','status':'ok','at':100})
        with patch('efb_manager.service.time.time', return_value=101):
            self.assertEqual(service.snapshot()['health']['telegram']['status'], 'attention')
            self.assertEqual(service.snapshot()['health']['telegram']['unresolved'], 1)
        service.event({'kind':'telegram_delivery','message_key':'one','status':'forwarded'})
        with patch('efb_manager.service.time.time', return_value=101):
            self.assertEqual(service.snapshot()['health']['telegram']['status'], 'ok')
