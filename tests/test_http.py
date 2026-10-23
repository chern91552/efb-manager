import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlencode

from efb_manager.history import History
from test_history import message
from efb_manager.history import capture


class HTTPTests(unittest.TestCase):
    def test_auth_origin_and_state(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        with tempfile.TemporaryDirectory(dir=os.environ.get('EFB_MANAGER_TEST_TMP')) as tmp:
            archive = History(Path(tmp) / 'messages.sqlite3')
            archive.save(capture(message(body='HTTP 测试文字'), 'default'))
            archive.save(capture(message('Image', body='不保存的图片说明', uid='image'), 'default'))
            child = subprocess.Popen([sys.executable, '-m', 'efb_manager.server',
                                      '--port', str(port), '--state-dir', tmp],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for _ in range(100):
                    try:
                        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                        conn.request('GET', '/api/state')
                        response = conn.getresponse()
                        self.assertEqual(response.status, 401)
                        response.read()
                        conn.close()
                        break
                    except OSError:
                        if child.poll() is not None:
                            self.fail(child.stderr.read().decode())
                        time.sleep(.05)
                else:
                    self.fail('server did not start')
                token = (Path(tmp) / 'access-token').read_text()
                def request(method, path, body=None, cookie=None, origin=True, host=None):
                    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                    headers = {'Content-Type': 'application/json'}
                    if origin:
                        headers['Origin'] = f'http://127.0.0.1:{port}'
                    if cookie:
                        headers['Cookie'] = cookie
                    if host:
                        headers['Host'] = host
                    connection.request(method, path, json.dumps(body) if body is not None else None, headers)
                    result = connection.getresponse()
                    status, set_cookie, data = result.status, result.getheader('Set-Cookie'), result.read()
                    connection.close()
                    return status, set_cookie, data
                self.assertEqual(request('POST', '/api/session', {'token': token}, origin=False)[0], 403)
                self.assertEqual(request('POST', '/api/session', {'token': 'wrong'})[0], 401)
                status, cookie, _ = request('POST', '/api/session', {'token': token})
                self.assertEqual(status, 200)
                self.assertIn('HttpOnly', cookie)
                status, _, data = request('GET', '/api/state', cookie=cookie)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(data)['process'], 'stopped')
                self.assertEqual(json.loads(data)['archive']['messages'], 2)
                reminder_policy = {'enabled': True, 'hours': 23.5, 'message': '登录到期提醒\n请检查微信'}
                self.assertEqual(request('GET', '/api/reminder-settings')[0], 401)
                self.assertEqual(request('POST', '/api/reminder-settings', reminder_policy)[0], 401)
                self.assertEqual(request('POST', '/api/reminder-settings', reminder_policy, cookie=cookie, origin=False)[0], 403)
                self.assertEqual(request('POST', '/api/reminder-settings', {'enabled': True, 'hours': 0}, cookie=cookie)[0], 400)
                self.assertEqual(request('POST', '/api/reminder-settings', reminder_policy, cookie=cookie)[0], 200)
                status, _, data = request('GET', '/api/reminder-settings', cookie=cookie)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(data), reminder_policy)
                status, _, data = request('GET', '/api/state', cookie=cookie)
                self.assertEqual(json.loads(data)['reminder'], reminder_policy)
                self.assertEqual(request('GET', '/api/messages?chat=abc')[0], 401)
                status, _, data = request('GET', '/api/conversations', cookie=cookie)
                key = json.loads(data)['items'][0]['key']
                self.assertEqual(status, 200)
                status, _, data = request('GET', '/api/messages?' + urlencode({'chat': key}), cookie=cookie)
                self.assertEqual(status, 200)
                self.assertEqual([m['body'] for m in json.loads(data)['items']], ['HTTP 测试文字', '[图片]'])
                status, _, data = request('GET', '/api/messages?' + urlencode({'chat': key, 'q': '图片'}), cookie=cookie)
                self.assertEqual(json.loads(data)['items'], [])
                self.assertEqual(request('GET', '/api/messages?chat=invalid', cookie=cookie)[0], 400)
                self.assertEqual(request('GET', '/api/state', cookie=cookie, host='evil.example')[0], 403)
                self.assertEqual(request('POST', '/api/login', {}, cookie=cookie)[0], 409)
                self.assertEqual(request('POST', '/api/login_qr', {})[0], 401)
                self.assertEqual(request('POST', '/api/login_qr', {}, cookie=cookie)[0], 409)
                self.assertEqual(request('POST', '/api/stop', {}, cookie=cookie)[0], 200)
                status, _, html = request('GET', '/')
                self.assertEqual(status, 200)
                self.assertIn('扫码登录'.encode(), html)
                self.assertIn('data-action="login_qr"'.encode(), html)
                status,_,data=request('GET','/api/archive-settings',cookie=cookie)
                self.assertEqual(status,200)
                self.assertFalse(json.loads(data)['record_public'])
                policy=dict(record_private=True,record_group=False,record_public=False,max_mb=100)
                self.assertEqual(request('POST','/api/archive-settings',policy)[0],401)
                self.assertEqual(request('POST','/api/archive-settings',dict(policy,max_mb=0),cookie=cookie)[0],400)
                self.assertEqual(request('POST','/api/archive-settings',policy,cookie=cookie)[0],200)
                self.assertFalse(archive.save(capture(message(uid='disabled'),'default')))
                policy['record_group']=True
                self.assertEqual(request('POST','/api/archive-settings',policy,cookie=cookie)[0],200)
                for n in range(30):
                    archive.save(capture(message(body='A'*60000,uid='bulk-'+str(n)),'default'))
                policy['max_mb']=1
                self.assertEqual(request('POST','/api/archive-settings',policy,cookie=cookie)[0],200)
                for _ in range(100):
                    status,_,data=request('GET','/api/archive-settings',cookie=cookie)
                    stored=json.loads(data)
                    if stored['total_removed']>0 and stored['disk_bytes']<=1048576:
                        break
                    time.sleep(.1)
                else:
                    self.fail('Background cleanup did not reclaim disk space')
                self.assertEqual(request('POST', '/api/password', {'current':token,'new':'custom-http-password'})[0], 401)
                self.assertEqual(request('POST', '/api/password', {'current':'wrong','new':'custom-http-password'},cookie=cookie)[0], 400)
                self.assertEqual(request('POST', '/api/password', {'current':token,'new':'short'},cookie=cookie)[0], 400)
                self.assertEqual((Path(tmp)/'access-token').read_text(), token)
                status,new_cookie,_=request('POST', '/api/password', {'current':token,'new':'custom-http-password'},cookie=cookie)
                self.assertEqual(status, 200)
                self.assertEqual(request('GET','/api/state',cookie=cookie)[0],401)
                self.assertEqual(request('GET','/api/state',cookie=new_cookie)[0],200)
                self.assertEqual(request('POST','/api/session',{'token':token})[0],401)
                self.assertEqual(request('POST','/api/session',{'token':'custom-http-password'})[0],200)
                self.assertEqual((Path(tmp)/'access-token').read_text(),'custom-http-password')
            finally:
                child.terminate()
                child.communicate(timeout=20)
