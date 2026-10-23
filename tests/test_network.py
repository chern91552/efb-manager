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

from efb_manager.server import host_allowed


class NetworkTests(unittest.TestCase):
    def test_host_validation(self):
        self.assertTrue(host_allowed('127.0.0.1:8765','127.0.0.1',8765))
        self.assertFalse(host_allowed('203.0.113.10:8765','127.0.0.1',8765))
        self.assertTrue(host_allowed('203.0.113.10:8765','0.0.0.0',8765))
        self.assertFalse(host_allowed('203.0.113.10:9999','0.0.0.0',8765))
        self.assertFalse(host_allowed('evil.example:8765','0.0.0.0',8765))
        self.assertTrue(host_allowed('manager.example:8765','0.0.0.0',8765,['manager.example']))
        for value in ('', 'user@127.0.0.1:8765','127.0.0.1:8765/path','127.0.0.1:99999'):
            self.assertFalse(host_allowed(value,'0.0.0.0',8765))

    def test_external_ip_host_login_still_requires_password_and_same_origin(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        with tempfile.TemporaryDirectory(dir=os.environ.get('EFB_MANAGER_TEST_TMP')) as tmp:
            child=subprocess.Popen([sys.executable,'-m','efb_manager.server','--no-auto-start','--host','0.0.0.0','--port',str(port),'--state-dir',tmp],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                def request(method,path,body=None,cookie=None,origin=None,host=None):
                    conn=http.client.HTTPConnection('127.0.0.1',port,timeout=3)
                    headers={'Host':host or f'203.0.113.10:{port}','Content-Type':'application/json'}
                    if cookie:headers['Cookie']=cookie
                    if origin:headers['Origin']=origin
                    conn.request(method,path,json.dumps(body) if body is not None else None,headers)
                    response=conn.getresponse();result=(response.status,response.getheader('Set-Cookie'),response.read());conn.close();return result
                for _ in range(100):
                    try:
                        self.assertEqual(request('GET','/')[0],200);break
                    except OSError:
                        if child.poll() is not None:self.fail(child.stderr.read().decode())
                        time.sleep(.05)
                else:self.fail('Server did not start')
                token=(Path(tmp)/'access-token').read_text()
                self.assertEqual(request('GET','/api/state')[0],401)
                self.assertEqual(request('POST','/api/session',{'token':token},origin='http://evil.example')[0],403)
                self.assertEqual(request('POST','/api/session',{'token':'wrong'},origin=f'http://203.0.113.10:{port}')[0],401)
                status,cookie,_=request('POST','/api/session',{'token':token},origin=f'http://203.0.113.10:{port}')
                self.assertEqual(status,200)
                self.assertEqual(request('GET','/api/state',cookie=cookie)[0],200)
                self.assertEqual(request('GET','/',host=f'evil.example:{port}')[0],403)
            finally:
                child.terminate();child.communicate(timeout=20)
