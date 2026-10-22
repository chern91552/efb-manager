"""Local password storage and session rotation."""
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import tempfile
import threading


def validate_password(value):
    if not isinstance(value, str) or not 8 <= len(value) <= 256:
        raise ValueError("管理口令需要 8 到 256 个字符")
    if value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("管理口令不能包含控制字符或首尾空白")
    return value


def write_password(path, value):
    validate_password(value)
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".password-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Access:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        if not self.path.exists():
            write_password(self.path, secrets.token_urlsafe(32))
        self.password = validate_password(self.path.read_text(encoding="utf-8").strip())
        if os.name == "posix":
            self.path.chmod(0o600)
        self.session = secrets.token_urlsafe(32)

    def matches(self, supplied):
        if not isinstance(supplied, str):
            return False
        with self.lock:
            return hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(),
                                       hashlib.sha256(self.password.encode()).digest())

    def login(self, supplied):
        with self.lock:
            return self.session if self.matches(supplied) else None

    def authorized(self, session):
        with self.lock:
            return isinstance(session, str) and hmac.compare_digest(session.encode(), self.session.encode())

    def change(self, current, new):
        validate_password(new)
        with self.lock:
            if not self.matches(current):
                raise ValueError("当前管理口令不正确")
            write_password(self.path, new)
            self.password = new
            self.session = secrets.token_urlsafe(32)
            return self.session
