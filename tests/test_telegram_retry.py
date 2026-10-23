from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

from efb_manager.telegram_retry import TelegramRetry


class NetworkError(Exception):
    pass


class NewConnectionError(Exception):
    pass


class BadRequest(NetworkError):
    pass


class RetryTests(unittest.TestCase):
    def test_read_retries_then_send_succeeds(self):
        emit, sleep = Mock(), Mock()
        retry = TelegramRetry(emit, (1, 2), sleep)
        read = Mock(side_effect=[NetworkError('response lost'), b'ok'])
        write = Mock(return_value=b'sent')
        msg = NS(vendor_specific={})
        with retry.message(msg):
            self.assertEqual(retry.request(read, 'POST', 'https://api.telegram.org/botPRIVATE/getChat'), b'ok')
            retry.request(write, 'POST', 'https://api.telegram.org/botPRIVATE/sendMessage')
        self.assertEqual(read.call_count, 2)
        write.assert_called_once()
        sleep.assert_called_once_with(1)
        self.assertEqual(msg.vendor_specific['_manager_delivery'], 'retry_succeeded')
        self.assertNotIn('PRIVATE', str(emit.call_args_list))

    def test_ambiguous_send_never_retries_or_replays_earlier_chunk(self):
        retry, msg = TelegramRetry(Mock(), sleep=Mock()), NS(vendor_specific={})
        failed = Mock(side_effect=NetworkError('response lost'))
        with retry.message(msg):
            retry.request(Mock(return_value=b'ok'), 'POST', 'https://host/sendMessage')
            with self.assertRaises(NetworkError):
                retry.request(failed, 'POST', 'https://host/sendMessage')
            retry.request(Mock(return_value=b'ok'), 'POST', 'https://host/getChat')
        failed.assert_called_once()
        retry.sleep.assert_not_called()
        self.assertEqual(msg.vendor_specific['_manager_delivery'], 'uncertain')

    def test_proven_connect_failure_can_retry_send(self):
        error = NetworkError('connect failed')
        error.__cause__ = NewConnectionError('not connected')
        request = Mock(side_effect=[error, b'ok'])
        retry, msg = TelegramRetry(Mock(), (1,), Mock()), NS(vendor_specific={})
        with retry.message(msg):
            retry.request(request, 'POST', 'https://host/sendMessage')
        self.assertEqual(request.call_count, 2)
        self.assertEqual(msg.vendor_specific['_manager_delivery'], 'retry_succeeded')

    def test_read_exhaustion_and_api_rejection_are_not_success(self):
        for error, count, expected in [(NetworkError('no'), 3, 'retry_failed'), (BadRequest('bad'), 1, 'failed')]:
            request = Mock(side_effect=error)
            retry, msg = TelegramRetry(Mock(), (1, 2), Mock()), NS(vendor_specific={})
            with retry.message(msg):
                with self.assertRaises(type(error)):
                    retry.request(request, 'POST', 'https://host/getChat')
            self.assertEqual(request.call_count, count)
            self.assertEqual(msg.vendor_specific['_manager_delivery'], expected)

    def test_swallowed_exception_is_visible_to_recorder(self):
        from efb_manager.history import install_recorder
        from test_history import message
        store = Mock()
        retry = TelegramRetry(Mock(), (), Mock())
        def send(msg):
            with retry.message(msg):
                try:
                    retry.request(Mock(side_effect=NetworkError()), 'POST', 'https://host/getChat')
                except NetworkError:
                    pass  # ETM catches and logs these errors itself.
            return msg
        coordinator = NS(send_message=send)
        install_recorder(coordinator, store, 'default', Mock())
        coordinator.send_message(message())
        self.assertEqual(store.save.call_args.args[0]['delivery'], 'retry_failed')
