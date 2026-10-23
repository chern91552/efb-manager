"""Retry individual safe Bot API requests, never replay a partially sent message."""
from contextlib import contextmanager
import functools
import hashlib
import json
import threading
import time
from urllib.parse import urlsplit


READ_METHODS = {'getchat', 'getchatmember', 'getchatadministrators', 'getchatmembercount',
                'getme', 'getupdates', 'getfile', 'getuserprofilephotos', 'sendchataction'}


def network_error(exc):
    names = {base.__name__ for base in type(exc).__mro__}
    return bool(names & {'NetworkError', 'TimedOut', 'ConnectionError', 'TimeoutError'}) and not bool(
        names & {'BadRequest', 'Unauthorized', 'InvalidToken', 'Forbidden'})


def before_connection(exc):
    """Only explicit connect failures prove that a send was never submitted."""
    seen, pending = set(), [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if type(current).__name__ in {'NewConnectionError', 'ConnectTimeoutError', 'NameResolutionError'}:
            return True
        for value in [getattr(current, '__cause__', None), getattr(current, '__context__', None),
                      getattr(current, 'reason', None), *getattr(current, 'args', ())]:
            if isinstance(value, BaseException):
                pending.append(value)
    return False


class TelegramRetry:
    def __init__(self, emit, delays=(1, 2, 4), sleep=time.sleep):
        self.emit, self.delays, self.sleep = emit, delays, sleep
        self.local = threading.local()

    @contextmanager
    def message(self, message):
        previous = getattr(self.local, 'message', None)
        previous_scope = getattr(self.local, 'scope', None)
        self.local.message = message
        self.local.scope = {'writes': False, 'retried': False, 'rate_limited': None}
        vendor = getattr(message, 'vendor_specific', None)
        if vendor is not None:
            vendor.pop('_manager_delivery', None)
        try:
            yield
        finally:
            scope = self.local.scope
            vendor = getattr(message, 'vendor_specific', None) or {}
            if scope['rate_limited']:
                self.status('failed')
            if vendor.get('_manager_delivery') not in ('uncertain', 'retry_failed', 'failed'):
                self.status(('retry_succeeded' if scope['retried'] else 'forwarded') if scope['writes'] else 'filtered')
            final = (getattr(message, 'vendor_specific', None) or {}).get('_manager_delivery', 'filtered')
            chat = getattr(message, 'chat', None)
            key = hashlib.sha256(json.dumps([str(getattr(chat, 'module_id', '')),
                                            str(getattr(chat, 'uid', '')), str(getattr(message, 'uid', ''))]).encode()).hexdigest()
            self.emit('telegram_delivery', message_key=key, status=final, at=time.time())
            self.local.message = previous
            self.local.scope = previous_scope

    def status(self, value):
        message = getattr(self.local, 'message', None)
        if message is not None:
            vendor = getattr(message, 'vendor_specific', None)
            if vendor is None:
                vendor = message.vendor_specific = {}
            # An uncertain or failed earlier chunk must not be hidden by a later success.
            old = vendor.get('_manager_delivery')
            if old not in ('uncertain', 'retry_failed', 'failed'):
                vendor['_manager_delivery'] = value

    def request(self, original, *args, **kwargs):
        url = args[1] if len(args) > 1 else kwargs.get('url', '')
        method = urlsplit(str(url)).path.rsplit('/', 1)[-1].lower()
        http_method = str(args[0] if args else kwargs.get('method', '')).upper()
        read_only = method in READ_METHODS or http_method in ('GET', 'HEAD')
        retried = False
        for attempt in range(len(self.delays) + 1):
            try:
                result = original(*args, **kwargs)
            except Exception as exc:
                if not network_error(exc):
                    scope = getattr(self.local, 'scope', None)
                    if type(exc).__name__ == 'RetryAfter' and scope is not None:
                        scope['rate_limited'] = method
                    else:
                        self.status('failed')
                    self.emit('health', component='telegram', status='error', error=type(exc).__name__)
                    raise
                safe = read_only or before_connection(exc)
                if not safe:
                    self.status('uncertain')
                    self.emit('health', component='telegram', status='uncertain', error=type(exc).__name__)
                    raise
                if attempt == len(self.delays):
                    self.status('retry_failed')
                    self.emit('health', component='telegram', status='error', error=type(exc).__name__)
                    raise
                retried = True
                scope = getattr(self.local, 'scope', None)
                if scope is not None:
                    scope['retried'] = True
                self.status('retrying')
                self.emit('health', component='telegram', status='retrying', retry=True,
                          operation=method, attempt=attempt + 1, error=type(exc).__name__)
                self.sleep(self.delays[attempt])
            else:
                scope = getattr(self.local, 'scope', None)
                if scope is not None:
                    if scope['rate_limited'] == method:
                        scope['rate_limited'] = None
                    if not read_only:
                        scope['writes'] = True
                self.emit('health', component='telegram', status='ok', at=time.time(), recovered=retried, operation=method)
                return result

    def install(self, request_class, message_class):
        original_request = request_class._request_wrapper
        original_send = message_class.send_message
        monitor = self

        @functools.wraps(original_request)
        def request(instance, *args, **kwargs):
            return monitor.request(lambda *a, **k: original_request(instance, *a, **k), *args, **kwargs)

        @functools.wraps(original_send)
        def send(instance, message, *args, **kwargs):
            with monitor.message(message):
                return original_send(instance, message, *args, **kwargs)

        request_class._request_wrapper = request
        message_class.send_message = send
