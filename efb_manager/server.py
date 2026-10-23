import argparse
import getpass
import hashlib
import ipaddress
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import signal
import threading
from urllib.parse import urlsplit, parse_qs

from .service import Service
from .reminder import ReminderSettings
from .history import History
from .access import Access, write_password


def host_allowed(value, bind_host, port, extra=()):
    try:
        parsed = urlsplit("//" + value)
        if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return False
        if (parsed.port or 80) != port:
            return False
        name = parsed.hostname
        if name in {"localhost", "127.0.0.1", bind_host, *extra}:
            return True
        return bind_host == "0.0.0.0" and ipaddress.ip_address(name).version == 4
    except (ValueError, TypeError):
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", default="default")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1", help="监听地址；服务器 IP 直连可用 0.0.0.0")
    parser.add_argument("--allowed-host", action="append", default=[], help="额外允许访问的域名，不带协议和端口，可重复设置")
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/efb-manager")
    parser.add_argument("--set-password", action="store_true", help="交互设置管理口令后退出；运行中的服务需重启生效")
    args = parser.parse_args()
    if os.name == "posix":
        os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_file = args.state_dir / "access-token"
    if args.set_password:
        value = getpass.getpass("新管理口令（8 到 256 个字符）：")
        if value != getpass.getpass("再次输入新管理口令："):
            parser.error("两次输入不一致，未修改管理口令")
        try:
            write_password(token_file, value)
        except ValueError as exc:
            parser.error(str(exc))
        print("管理口令已保存。若管理器正在运行，请重启服务使其生效。")
        return
    access = Access(token_file)
    history = History(args.state_dir / "messages.sqlite3")
    reminder_path = args.state_dir / ("reminder-" + hashlib.sha256(args.profile.encode()).hexdigest()[:16] + ".json")
    reminder = ReminderSettings(reminder_path)
    service = Service(args.profile, history, reminder_path)
    cleanup_stop = threading.Event()
    cleanup_wake = threading.Event()
    def cleanup():
        while not cleanup_stop.is_set():
            try:
                history.enforce_limit()
                with service.lock:
                    service.state["cleanup_error"] = None
            except Exception:
                with service.lock:
                    service.state["cleanup_error"] = "历史清理暂未完成，将自动重试，请检查磁盘空间和数据库权限"
            cleanup_wake.wait(2)
            cleanup_wake.clear()
    cleaner = threading.Thread(target=cleanup, daemon=True)
    cleaner.start()
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
                return access.authorized(cookie["efb_session"].value)
            except (KeyError, ValueError):
                return False

        def valid_host(self):
            return host_allowed(self.headers.get("Host", ""), args.host, args.port, args.allowed_host)

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
                        return self.send(200, {**service.snapshot(), "archive": history.stats(args.profile), "storage": history.settings(), "reminder": reminder.read()})
                    if url.path == "/api/reminder-settings":
                        return self.send(200, reminder.read())
                    if url.path == "/api/archive-settings":
                        return self.send(200, history.settings())
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
                if not 0 < length <= 8192:
                    raise ValueError("请求大小无效")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("请求格式无效")
            except (ValueError, UnicodeError):
                return self.send(400, {"error": "请求格式无效"})
            if self.path in ("/api/session", "/api/password"):
                if self.path == "/api/password" and not self.authorized():
                    return self.send(401, {"error": "请先登录管理器"})
                import time
                with login_lock:
                    now = time.monotonic()
                    login_attempts[:] = [t for t in login_attempts if now - t < 60]
                    if len(login_attempts) >= 10:
                        return self.send(429, {"error": "尝试过于频繁，请稍后重试"})
                    login_attempts.append(now)
                if self.path == "/api/password":
                    try:
                        session = access.change(body.get("current"), body.get("new"))
                    except ValueError as exc:
                        return self.send(400, {"error": str(exc)})
                    except OSError:
                        return self.send(503, {"error": "口令保存失败，请检查数据目录权限"})
                else:
                    session = access.login(body.get("token", ""))
                    if not session:
                        return self.send(401, {"error": "管理口令不正确"})
                return self.send(200, {"ok": True}, cookie=f"efb_session={session}; HttpOnly; SameSite=Strict; Path=/")
            if not self.authorized():
                return self.send(401, {"error": "请先登录管理器"})
            if self.path == "/api/delete-records":
                try:
                    scope = body.get("scope")
                    if scope not in ("message", "conversation"):
                        raise ValueError("删除范围无效")
                    if scope == "message" and type(body.get("id")) is not int:
                        raise ValueError("消息编号无效")
                    captured_at = body.get("captured_at")
                    if scope == "message" and (type(captured_at) not in (int, float) or not math.isfinite(captured_at)):
                        raise ValueError("消息时间无效，请刷新后重试")
                    count = history.delete_records(args.profile, body.get("chat"),
                                                   body["id"] if scope == "message" else None,
                                                   captured_at if scope == "message" else None)
                    return self.send(200, {"deleted": count})
                except ValueError as exc:
                    return self.send(400, {"error": str(exc)})
                except Exception:
                    return self.send(503, {"error": "记录删除失败，请稍后重试"})
            if self.path == "/api/reminder-settings":
                try:
                    return self.send(200, reminder.update(body))
                except ValueError as exc:
                    return self.send(400, {"error": str(exc)})
                except OSError:
                    return self.send(503, {"error": "提醒设置保存失败"})
            if self.path == "/api/archive-settings":
                try:
                    settings = history.update_settings(body)
                except ValueError as exc:
                    return self.send(400, {"error": str(exc)})
                except Exception:
                    return self.send(503, {"error": "归档设置保存失败"})
                cleanup_wake.set()
                return self.send(200, settings)
            action = self.path.removeprefix("/api/")
            if action not in ("start", "stop", "restart", "login", "login_qr", "logout"):
                return self.send(404, {"error": "不存在"})
            if not actions_lock.acquire(blocking=False):
                return self.send(409, {"error": "另一个操作尚未结束"})
            try:
                if action in ("stop", "restart"):
                    service.stop()
                if action in ("start", "restart"):
                    service.start()
                if action in ("login", "login_qr", "logout"):
                    service.command(action)
                return self.send(200, {"ok": True})
            except (ValueError, OSError) as exc:
                return self.send(409, {"error": str(exc)})
            finally:
                actions_lock.release()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    def shutdown(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f"监听地址：{args.host}:{args.port}\n管理口令文件：{token_file}", flush=True)
    try:
        server.serve_forever()
    finally:
        cleanup_stop.set()
        cleanup_wake.set()
        cleaner.join(timeout=5)
        service.stop()
        server.server_close()


if __name__ == "__main__":
    main()
