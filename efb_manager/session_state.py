"""Persist session age and reminder attempt without storing WeChat credentials."""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.session-')
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def fingerprint(bot):
    info = bot.core.loginInfo
    values = [info.get('wxuin'), info.get('wxsid')]
    if not all(isinstance(v, (str, int)) and str(v) for v in values):
        raise ValueError('Missing session identity')
    return hashlib.sha256(json.dumps([str(v) for v in values], separators=(',', ':')).encode()).hexdigest()


class SessionState:
    def __init__(self, path):
        self.path = Path(path)
        self.value = None

    def login(self, identity, restored, now):
        try:
            previous = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            previous = {}
        started = previous.get('started_at') if isinstance(previous, dict) else None
        reuse = (restored and isinstance(previous, dict) and previous.get('identity') == identity
                 and type(started) in (int, float) and math.isfinite(started) and 0 < started <= now)
        self.value = previous if reuse else {'identity': identity, 'started_at': now,
                                             'attempted': False, 'origin_known': not restored}
        atomic_json(self.path, self.value)
        return dict(self.value)

    def mark_attempted(self):
        if self.value is None:
            raise RuntimeError('Session is not initialized')
        self.value['attempted'] = True
        atomic_json(self.path, self.value)
