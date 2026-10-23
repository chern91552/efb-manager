import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from efb_manager.server import main


class StartupTests(unittest.TestCase):
    def run_server(self, args=(), error=None):
        events = []
        server, service = Mock(), Mock()
        service.lock = threading.RLock()
        service.state = {}
        def start():
            events.append('start')
            if error:
                raise error
        service.start.side_effect = start
        server.serve_forever.side_effect = lambda: events.append('serve')
        def bind(*args):
            events.append('bind')
            return server
        with tempfile.TemporaryDirectory() as root, \
             patch('sys.argv', ['efb-manager', '--state-dir', root, *args]), \
             patch('efb_manager.server.Service', return_value=service), \
             patch('efb_manager.server.ThreadingHTTPServer', side_effect=bind), \
             patch('efb_manager.server.signal.signal'), patch('builtins.print'):
            main()
        return events, service, server

    def test_starts_once_after_binding_before_serving(self):
        events, service, server = self.run_server()
        self.assertEqual(events, ['bind', 'start', 'serve'])
        service.start.assert_called_once()
        service.stop.assert_called_once()
        server.server_close.assert_called_once()

    def test_manual_mode_does_not_start(self):
        events, service, _ = self.run_server(['--no-auto-start'])
        self.assertEqual(events, ['bind', 'serve'])
        service.start.assert_not_called()

    def test_failed_start_keeps_manager_available(self):
        events, service, _ = self.run_server(error=OSError('private environment details'))
        self.assertEqual(events, ['bind', 'start', 'serve'])
        self.assertEqual(service.event.call_args.args[0]['kind'], 'fatal')
        self.assertNotIn('private environment details', service.event.call_args.args[0]['error'])
