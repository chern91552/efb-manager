import argparse
import hashlib
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import threading
from urllib.parse import urlsplit, parse_qs

from .service import Service
from .history import History


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="default")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/efb-manager")
    args = parser.parse_args()
    if os.name == "posix":
        os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_file = args.state_dir / "access-token"
    if not token_file.exists():
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    token = token_file.read_text().strip()
    if len(token) < 24:
        raise RuntimeError("管理口令过短，请删除 access-token 后重新启动")
    # Cookie value differs from the password and changes on each server start.
    session = secrets.token_urlsafe(32)
    history = History(args.state_dir / "messages.sqlite3")
    service = Service(args.profile, history)
    actions_lock = threading.Lock()
    login_lock = threading.Lock()
    login_attempts = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, content_type="application/json; charset=utf-8", cookie=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                return hmac.compare_digest(cookie["efb_session"].value, session)
            except (KeyError, ValueError):
                return False

        def valid_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{args.port}", f"localhost:{args.port}")

        def do_GET(self):
            if not self.valid_host():
                return self.send(403, {"error": "不允许的 Host"})
            if self.path == "/":
                return self.send(200, Path(__file__).with_name("index.html").read_bytes(), "text/html; charset=utf-8")
            url = urlsplit(self.path)
            if url.path.startswith("/api/"):
                if not self.authorized():
                    return self.send(401, {"error": "请先登录管理器"})
                params = parse_qs(url.query, keep_blank_values=True)
                def param(name, default=""):
                    return params.get(name, [default])[0]
                try:
                    if url.path == "/api/state":
                        return self.send(200, {**service.snapshot(), "archive": history.stats(args.profile)})
                    if url.path == "/api/conversations":
                        return self.send(200, {"items": history.conversations(args.profile, param("q"))})
                    if url.path == "/api/messages":
                        return self.send(200, history.messages(args.profile, param("chat"), param("q"),
                                         param("kind", "all"), param("before") or None, param("limit", "50")))
                except (ValueError, TypeError):
                    return self.send(400, {"error": "查询参数无效"})
                except Exception:
                    return self.send(503, {"error": "消息数据库暂不可用"})
            return self.send(404, {"error": "不存在"})

        def do_POST(self):
            if not self.valid_host() or self.headers.get("Origin") != "http://" + self.headers.get("Host", ""):
                return self.send(403, {"error": "来源验证失败"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError("请求大小无效")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("请求格式无效")
            except (ValueError, UnicodeError):
                return self.send(400, {"error": "请求格式无效"})
            if self.path == "/api/session":
                import time
                with login_lock:
                    now = time.monotonic()
                    login_attempts[:] = [t for t in login_attempts if now - t < 60]
                    if len(login_attempts) >= 10:
                        return self.send(429, {"error": "尝试过于频繁，请稍后重试"})
                    login_attempts.append(now)
                supplied = body.get("token", "")
                if not isinstance(supplied, str) or not hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(), hashlib.sha256(token.encode()).digest()):
                    return self.send(401, {"error": "管理口令不正确"})
                return self.send(200, {"ok": True}, cookie=f"efb_session={session}; HttpOnly; SameSite=Strict; Path=/")
            if not self.authorized():
                return self.send(401, {"error": "请先登录管理器"})
            action = self.path.removeprefix("/api/")
            if action not in ("start", "stop", "restart", "login", "logout"):
                return self.send(404, {"error": "不存在"})
            if not actions_lock.acquire(blocking=False):
                return self.send(409, {"error": "另一个操作尚未结束"})
            try:
                if action in ("stop", "restart"):
                    service.stop()
                if action in ("start", "restart"):
                    service.start()
                if action in ("login", "logout"):
                    service.command(action)
                return self.send(200, {"ok": True})
            except (ValueError, OSError) as exc:
                return self.send(409, {"error": str(exc)})
            finally:
                actions_lock.release()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"管理界面：http://127.0.0.1:{args.port}\n管理口令文件：{token_file}", flush=True)
    try:
        server.serve_forever()
    finally:
        service.stop()
        server.server_close()


if __name__ == "__main__":
    main()
