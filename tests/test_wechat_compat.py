from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from efb_manager.wechat_compat import install_legacy_login, WeChatLoginError


def legacy_process(core, content):
    return False


def patched_process(core, content):
    return {"client-version": "existing", "extspam": "existing"}


REDIRECT = 'window.redirect_uri="https://wx2.qq.com/cgi-bin/mmwebwx-bin/webwxnewloginpage?ticket=PRIVATE-TICKET";'
SUCCESS = '<error><ret>0</ret><skey>fake&amp;key</skey><wxsid>fake-sid</wxsid><wxuin>123</wxuin><pass_ticket>fake-ticket</pass_ticket></error>'


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.module = NS(config=NS(BASE_URL='https://login.weixin.qq.com'), process_login_info=legacy_process)
        self.core_class = type('Core', (), {'get_QRuuid': legacy_process})
        self.assertTrue(install_legacy_login('2.0.7', self.module, self.core_class))
        self.core = NS(user_agent='test-agent', loginInfo={}, s=NS(
            get=Mock(return_value=NS(status_code=200,text=SUCCESS)),
            cookies=NS(get_dict=lambda:{})))

    def test_only_legacy_version_is_adapted(self):
        module=NS(process_login_info=legacy_process)
        self.assertFalse(install_legacy_login('2.0.9',module))
        self.assertIs(module.process_login_info,legacy_process)

    def test_existing_patch_is_kept(self):
        module=NS(process_login_info=patched_process)
        self.assertFalse(install_legacy_login('2.0.7',module))
        self.assertIs(module.process_login_info,patched_process)

    def test_desktop_qr_request(self):
        self.core.s.get.return_value=NS(text='window.QRLogin.code = 200; window.QRLogin.uuid = "demo-uuid";')
        self.assertEqual(self.module.get_QRuuid(self.core),'demo-uuid')
        kwargs=self.core.s.get.call_args.kwargs
        self.assertIn('mod=desktop',kwargs['params']['redirect_uri'])
        self.assertEqual(kwargs['params']['appid'],'wx782c26e4c19acffb')

    def test_already_bound_core_method_is_updated(self):
        core = self.core_class()
        core.user_agent = self.core.user_agent
        core.s = self.core.s
        core.s.get.return_value = NS(text='window.QRLogin.code = 200; window.QRLogin.uuid = "bound-uuid";')
        self.assertEqual(core.get_QRuuid(), 'bound-uuid')
        self.assertIs(self.core_class.get_QRuuid, self.module.get_QRuuid)

    def test_genuine_credentials_and_uos_headers(self):
        self.assertTrue(self.module.process_login_info(self.core,REDIRECT))
        kwargs=self.core.s.get.call_args.kwargs
        self.assertTrue(kwargs['headers']['client-version'])
        self.assertTrue(kwargs['headers']['extspam'])
        self.assertFalse(kwargs['allow_redirects'])
        info=self.core.loginInfo
        self.assertEqual(info['skey'],'fake&key')
        self.assertEqual(info['pass_ticket'],'fake-ticket')
        self.assertNotEqual(info['BaseRequest']['DeviceID'],info['pass_ticket'])
        self.assertEqual(info['url'],'https://wx2.qq.com/cgi-bin/mmwebwx-bin')
        self.assertEqual(info['syncUrl'],'https://webpush.wx2.qq.com/cgi-bin/mmwebwx-bin')

    def test_html_is_rejected_without_leaking_body(self):
        self.core.s.get.return_value.text='<!DOCTYPE html><html><head>SECRET-BODY</head><body></html>'
        with self.assertRaises(WeChatLoginError) as caught:
            self.module.process_login_info(self.core,REDIRECT)
        self.assertIn('HTML',str(caught.exception))
        self.assertNotIn('SECRET-BODY',str(caught.exception))
        self.assertEqual(self.core.loginInfo,{})

    def test_invalid_xml_and_rejected_login_never_succeed(self):
        for text in ['<error><ret>0</ret>', '<error><ret>1203</ret><message>PRIVATE</message></error>',
                     '<error><ret>0</ret></error>']:
            self.core.s.get.return_value.text=text
            with self.assertRaises(WeChatLoginError) as caught:
                self.module.process_login_info(self.core,REDIRECT)
            self.assertNotIn('PRIVATE',str(caught.exception))
            self.assertEqual(self.core.loginInfo,{})

    def test_http_error_and_unknown_host_are_rejected(self):
        self.core.s.get.return_value.status_code=302
        with self.assertRaisesRegex(WeChatLoginError,'HTTP 302'):
            self.module.process_login_info(self.core,REDIRECT)
        self.core.s.get.reset_mock()
        with self.assertRaises(WeChatLoginError):
            self.module.process_login_info(self.core,'window.redirect_uri="https://evil.example/cgi-bin/mmwebwx-bin/test";')
        self.core.s.get.assert_not_called()

    def test_network_error_does_not_leak_ticket(self):
        self.core.s.get.side_effect=RuntimeError('PRIVATE-TICKET')
        with self.assertRaises(WeChatLoginError) as caught:
            self.module.process_login_info(self.core,REDIRECT)
        self.assertNotIn('PRIVATE-TICKET',str(caught.exception))

    def test_session_cookies_are_used_when_xml_omits_session_fields(self):
        self.core.s.get.return_value.text='<error><ret>0</ret><pass_ticket>fake-ticket</pass_ticket></error>'
        self.core.s.cookies.get_dict=lambda:{'wxsid':'cookie-sid','wxuin':'123'}
        self.assertTrue(self.module.process_login_info(self.core,REDIRECT))
        self.assertEqual(self.core.loginInfo['wxsid'],'cookie-sid')
