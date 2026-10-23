"""EFB child entrypoint. Adapts the installed official channel without editing it."""
import base64
import importlib.metadata
import io
import json
import os
import sys
import threading
import time

from .qr_login import fresh_qr_login, cached_login_then_wait
from .reminder import LoginReminder, ReminderSettings
from .reminder_delivery import ReminderDelivery, ReminderSyncError
from .session_state import SessionState, fingerprint

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
        self.waiting_for_scan = False
        self.force_qr = False
        self.reminder = LoginReminder()
        self.reminder_delivery = None
        self.session_state = SessionState(os.environ["EFB_MANAGER_SESSION_STATE"]) if os.environ.get("EFB_MANAGER_SESSION_STATE") else None
        self.reminder_settings = None
        self.test_running = False
        self.last_test = float("-inf")

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
                controller.ready = True
            with controller.lock:
                controller.busy = True
            try:
                emit("logging_in")
                with fresh_qr_login() if controller.force_qr else cached_login_then_wait(controller.wait_for_scan) as authentication:
                    if controller.force_qr:
                        print("强制扫码登录：跳过缓存会话恢复和手机快捷确认，正在获取新二维码。", flush=True)
                    original_auth(channel, "manager_qr_code", first_start=first_start)
                if not channel.bot.alive:
                    raise RuntimeError("微信认证未成功")
                with controller.lock:
                    controller.online = True
                    started = controller.session_login(channel.bot, bool(authentication and authentication.get("restored")))
                emit("online", at=started)
            except Exception as exc:
                emit("login_error", error=type(exc).__name__ + ": " + str(exc))
                raise
            finally:
                with controller.lock:
                    controller.waiting_for_scan = False
                    controller.force_qr = False
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
            if action == "reminder_test":
                if not self.online or self.busy or self.test_running or not self.reminder_settings:
                    raise RuntimeError("请等待微信上线后再测试提醒")
                if not self.reminder_delivery or not hasattr(self.channel, 'chats') or getattr(self.reminder_delivery.coordinator, 'master', None) is None:
                    raise RuntimeError("EFB 尚未完成初始化")
                if time.monotonic() - self.last_test < 30:
                    raise RuntimeError("测试提醒每 30 秒最多发送一次")
                self.last_test = time.monotonic()
                self.test_running = True
                emit('reminder_test', status='running', message='正在发送测试提醒…')
                threading.Thread(target=self.test_reminder, daemon=True, name='reminder-test').start()
            elif action in ("login", "login_qr"):
                if self.waiting_for_scan:
                    if action != "login_qr":
                        raise RuntimeError("缓存恢复失败，请点击扫码登录")
                    self.waiting_for_scan = False
                    self.gate.set()
                    return
                if self.busy or self.online or self.gate.is_set():
                    raise RuntimeError("正在登录或已经在线")
                self.force_qr = action == "login_qr"
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

    def session_login(self, bot, restored):
        now = time.time()
        self.reminder.on_attempt = None
        if self.session_state:
            try:
                value = self.session_state.login(fingerprint(bot), restored, now)
                self.reminder.login(time.monotonic(), now - value['started_at'], value.get('attempted') is True)
                self.reminder.on_attempt = self.session_state.mark_attempted
                emit('session_state', origin_known=value.get('origin_known', False), error=None)
                return value['started_at']
            except Exception as exc:
                emit('session_state', origin_known=False, error='登录计时保存失败（' + type(exc).__name__ + '），本次使用内存计时。')
        self.reminder.login(time.monotonic())
        return now

    def test_reminder(self):
        with self.lock:
            try:
                if not self.online or not self.channel.bot.alive:
                    raise RuntimeError('微信已离线')
                settings = self.reminder_settings.read()
                elapsed = max(0, time.monotonic() - self.reminder.started) if self.reminder.started is not None else 0
                self.reminder.send(self.channel.bot, settings, elapsed, self._forward_reminder, test=True)
            except ReminderSyncError as exc:
                emit('reminder_test', status='partial', message='测试提醒已发送至微信，同步 EFB 失败（' + str(exc) + '）。')
            except Exception as exc:
                emit('reminder_test', status='error', message='测试提醒发送失败或结果不确定（' + type(exc).__name__ + '），请检查文件传输助手。')
            else:
                emit('reminder_test', status='sent', message='测试提醒已发送，已提交 Telegram 转发与归档，请检查实际结果。')
            finally:
                self.test_running = False

    def wait_for_scan(self):
        with self.lock:
            self.gate.clear()
            self.waiting_for_scan = True
            self.online = False
            emit("awaiting_scan")
        self.gate.wait()
        self.gate.clear()
        emit("logging_in")

    def check_reminder(self, settings):
        with self.lock:
            if self.channel is None or not self.online or self.busy:
                return
            if self.reminder_delivery and (not hasattr(self.channel, 'chats') or
                    getattr(self.reminder_delivery.coordinator, 'master', None) is None):
                return  # Wait for EFB initialization before sending to any destination.
            try:
                sent = self.reminder.tick(self.channel.bot, self.online, self.busy,
                                          settings, time.monotonic(),
                                          on_sent=self._forward_reminder if self.reminder_delivery else None)
            except ReminderSyncError as exc:
                emit("reminder_error", message="提醒已发送至微信文件传输助手，但同步到 EFB 失败（" +
                     str(exc) + "）。请检查 Telegram 和消息记录；不会再次向微信发送。")
            except Exception as exc:
                emit("reminder_error", message="登录到期提醒发送失败或结果不确定（" + type(exc).__name__ +
                     "）；请检查文件传输助手。本次登录不自动重发，以免重复提醒。")
            else:
                if sent:
                    emit("reminder_sent", message="登录到期提醒已发送至微信文件传输助手，并已提交 EFB 转发与归档。")

    def _forward_reminder(self, sent, text):
        self.reminder_delivery.forward(self.channel, sent, text)

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
        from .wechat_compat import configure_legacy_login
        if configure_legacy_login():
            print("已启用 EWS 2.0.7 旧版登录兼容：UOS 请求参数及认证结果检查。", flush=True)
        else:
            print("使用已安装微信从端的原始登录协议。", flush=True)
        from ehforwarderbot import coordinator
        from .history import History, install_recorder
        archive = os.environ.get("EFB_MANAGER_DB")
        if archive:
            install_recorder(coordinator, History(archive),
                             os.environ.get("EFB_MANAGER_PROFILE", "default"),
                             lambda detail: emit("archive_error", error=detail),
                             lambda: emit("archive_recovered"),
                             lambda component, status: emit("health", component=component, status=status, at=time.time()))
        from .health import install_wechat_health
        install_wechat_health(emit)
        try:
            from telegram.utils.request import Request
            from efb_telegram_master.slave_message import SlaveMessageProcessor
            from .telegram_retry import TelegramRetry
            TelegramRetry(emit).install(Request, SlaveMessageProcessor)
        except ImportError:
            emit('health', component='telegram', status='unsupported', error='当前 Telegram 主端版本不支持请求补偿适配')
        controller = LoginController()
        controller.reminder_delivery = ReminderDelivery(coordinator)
        controller.install(WeChatChannel)
        reminder_path = os.environ.get("EFB_MANAGER_REMINDER_SETTINGS")
        if reminder_path:
            settings = ReminderSettings(reminder_path)
            controller.reminder_settings = settings
            def reminders():
                settings_error = False
                while True:
                    time.sleep(5)
                    try:
                        policy = settings.read()
                    except Exception:
                        if not settings_error:
                            emit("reminder_error", message="登录提醒设置读取失败，请在设置页重新保存。")
                        settings_error = True
                        continue
                    settings_error = False
                    controller.check_reminder(policy)
            threading.Thread(target=reminders, daemon=True, name="login-reminder").start()


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
