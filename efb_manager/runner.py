"""EFB child entrypoint. Adapts the installed official channel without editing it."""
import base64
import importlib.metadata
import io
import json
import os
import sys
import threading
import time

PREFIX = "EFB_MANAGER_EVENT:"
_output_lock = threading.Lock()


def emit(kind, **data):
    with _output_lock:
        print(PREFIX + json.dumps(dict(kind=kind, **data)), flush=True)


class LoginController:
    def __init__(self):
        self.channel = None
        self.gate = threading.Event()
        self.lock = threading.RLock()
        self.busy = False
        self.ready = False
        self.online = False

    def install(self, cls):
        original_auth = cls.authenticate
        controller = self

        def qr(channel, uuid, status, qrcode=None):
            status = int(status)
            if status == 200:
                # QR acceptance precedes bot setup; authenticate() marks online.
                emit("confirming")
            elif status == 201:
                emit("scanned")
            elif status in (0, 408):
                import pyqrcode
                output = io.BytesIO()
                pyqrcode.create("https://login.weixin.qq.com/l/" + uuid).svg(
                    output, scale=7, quiet_zone=4)
                emit("qr", image="data:image/svg+xml;base64," +
                     base64.b64encode(output.getvalue()).decode("ascii"))
            else:
                emit("login_status", code=status)

        def authenticate(channel, qr_reload=None, first_start=False):
            if controller.channel is not None and controller.channel is not channel:
                raise RuntimeError("此版本仅支持一个微信从端实例")
            controller.channel = channel
            if first_start:
                emit("waiting")
                controller.ready = True
                controller.gate.wait()
                controller.gate.clear()
            with controller.lock:
                controller.busy = True
            try:
                emit("logging_in")
                original_auth(channel, "manager_qr_code", first_start=first_start)
                if not channel.bot.alive:
                    raise RuntimeError("微信认证未成功")
                with controller.lock:
                    controller.online = True
                emit("online", at=time.time())
            except Exception as exc:
                emit("login_error", error=type(exc).__name__ + ": " + str(exc))
                raise
            finally:
                with controller.lock:
                    controller.busy = False

        def logout_callback(channel):
            with controller.lock:
                controller.online = False
            emit("offline", at=time.time())
            # Deliberately do not invoke upstream auto-reauth behavior.

        def reauth(channel, command=False):
            return "请在 EFB Manager 中点击登录微信。"

        cls.manager_qr_code = qr
        cls.console_qr_code = qr
        cls.master_qr_code = qr
        cls.authenticate = authenticate
        cls.exit_callback = logout_callback
        cls.reauth = reauth

    def command(self, action):
        with self.lock:
            if self.channel is None or not self.ready:
                raise RuntimeError("微信从端尚未初始化")
            if action == "login":
                if self.busy or self.online or self.gate.is_set():
                    raise RuntimeError("正在登录或已经在线")
                if not hasattr(self.channel, "chats"):
                    self.busy = True
                    self.gate.set()
                else:
                    self.busy = True
                    threading.Thread(target=self._login, daemon=True).start()
            elif action == "logout":
                if self.busy:
                    raise RuntimeError("登录过程中请使用停止 EFB 取消")
                if not self.online:
                    raise RuntimeError("微信尚未在线")
                self.channel.bot.logout()
                self.online = False
                emit("offline", at=time.time())
            else:
                raise ValueError("未知命令")

    def _login(self):
        try:
            # Avoid upstream reauth()'s cache-file assumptions.
            self.channel.authenticate("manager_qr_code")
        except Exception:
            pass  # authenticate() already published the failure.


def main():
    if os.name == "posix":
        os.setsid()
    try:
        from efb_wechat_slave import WeChatChannel
        from .wechat_compat import install_legacy_login
        if install_legacy_login():
            print("已启用 EWS 2.0.7 旧版登录兼容：UOS 请求参数及认证结果检查。", flush=True)
        from ehforwarderbot import coordinator
        from .history import History, install_recorder
        archive = os.environ.get("EFB_MANAGER_DB")
        if archive:
            install_recorder(coordinator, History(archive),
                             os.environ.get("EFB_MANAGER_PROFILE", "default"),
                             lambda: emit("archive_error", error="消息归档失败，请检查磁盘空间和数据库权限"))
        controller = LoginController()
        controller.install(WeChatChannel)

        def commands():
            for line in sys.stdin:
                try:
                    controller.command(json.loads(line)["action"])
                except Exception as exc:
                    emit("command_error", error=str(exc))

        threading.Thread(target=commands, daemon=True).start()
        entries = importlib.metadata.entry_points()
        entries = entries.select(group="console_scripts", name="ehforwarderbot") \
            if hasattr(entries, "select") else \
            [e for e in entries.get("console_scripts", []) if e.name == "ehforwarderbot"]
        entry = next(iter(entries), None)
        if entry is None:
            raise RuntimeError("当前 Python 环境未安装 ehforwarderbot")
        entry.load()()
    except Exception as exc:
        emit("fatal", error=type(exc).__name__ + ": " + str(exc))
        raise


if __name__ == "__main__":
    main()
