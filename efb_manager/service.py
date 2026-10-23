import collections
import copy
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time

from .runner import PREFIX


def redact(line):
    line = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", "[TOKEN REDACTED]", line)
    line = re.sub(r"https://login\.weixin\.qq\.com/\S+", "[QR REDACTED]", line)
    return line[-2000:]


class Service:
    def __init__(self, profile="default", history=None, reminder_path=None):
        self.reminder_path = reminder_path
        self.profile = profile
        self.history = history
        self.lock = threading.RLock()
        self.process = None
        self.logs = collections.deque(maxlen=250)
        self.state = dict(process="stopped", wechat="offline", qr=None,
                          logged_in_at=None, last_duration=0, error=None, archive_error=None)
        self.unresolved = collections.OrderedDict()
        self.last_event = 0
        self.health = {name: dict(status="unknown", last_success=None, last_error=None, last_received=None, retries=0, uncertain=0) for name in ("wechat", "telegram", "archive")}

    def snapshot(self):
        with self.lock:
            result = dict(self.state)
            started = result["logged_in_at"]
            result["online_seconds"] = max(0, int(time.time() - started)) \
                if started and result["wechat"] == "online" else 0
            result["logs"] = list(self.logs)
            result["profile"] = self.profile
            result["health"] = copy.deepcopy(self.health)
            for name, health in result["health"].items():
                if result["process"] != "running" or (name == "wechat" and result["wechat"] != "online"):
                    health["status"] = "offline"
                elif name != "archive" and health["status"] == "ok" and health["last_success"] and time.time() - health["last_success"] > 120:
                    health["status"] = "stale"
            result["health"]["telegram"]["unresolved"] = len(self.unresolved)
            if self.unresolved and result["health"]["telegram"]["status"] == "ok":
                result["health"]["telegram"]["status"] = "attention"
            return result

    def event(self, event):
        with self.lock:
            kind = event["kind"]
            self.last_event = time.monotonic()
            if kind in ("waiting", "logging_in", "scanned", "confirming", "awaiting_scan"):
                self.state.update(wechat=kind, qr=None if kind != "scanned" else self.state["qr"], error=None)
            elif kind == "qr":
                self.state.update(wechat="waiting_scan", qr=event["image"], error=None)
            elif kind == "online":
                self.state.update(wechat="online", logged_in_at=event["at"], qr=None, error=None, reminder_status=None)
            elif kind == "offline":
                self._offline()
            elif kind in ("reminder_sent", "reminder_error"):
                self.logs.append(event["message"])
                self.state["reminder_status"] = event["message"]
            elif kind == 'health':
                component = event.get('component')
                if component in self.health:
                    health = self.health[component]
                    health['status'] = event['status']
                    at = event.get('at', time.time())
                    if event['status'] in ('ok', 'received'):
                        health['last_success'] = at
                        health['last_error'] = None
                        if event['status'] == 'received':
                            health['last_received'] = at
                            health['status'] = 'ok'
                    elif event.get('error'):
                        health['last_error'] = event['error']
                    if event.get('retry'):
                        health['retries'] += 1
                        self.logs.append('Telegram 请求安全重试：' + event.get('operation', '') + '，第 ' + str(event.get('attempt', 0)) + ' 次。')
                    if event.get('recovered'):
                        self.logs.append('Telegram 请求重试成功：' + event.get('operation', '') + '。')
                    if event['status'] == 'uncertain':
                        health['uncertain'] += 1
            elif kind == 'telegram_delivery':
                key = event['message_key']
                if event['status'] in ('failed', 'retry_failed', 'uncertain'):
                    self.unresolved[key] = event['status']
                    self.unresolved.move_to_end(key)
                    while len(self.unresolved) > 4096:
                        self.unresolved.popitem(last=False)
                elif event['status'] in ('forwarded', 'retry_succeeded'):
                    self.unresolved.pop(key, None)
            elif kind == 'session_state':
                self.state['session_origin_known'] = event['origin_known']
                self.state['session_error'] = event.get('error')
            elif kind == 'reminder_test':
                self.state['reminder_test'] = dict(status=event['status'], message=event['message'])
                self.logs.append(event['message'])
            elif kind == "archive_error":
                if self.state["archive_error"] != event["error"]:
                    self.logs.append(event["error"])
                self.state["archive_error"] = event["error"]
                self.health["archive"].update(status="error", last_error=event["error"])
            elif kind == "archive_recovered":
                self.state["archive_error"] = None
                self.health["archive"].update(status="ok", last_error=None, last_success=time.time())
                self.logs.append("消息归档已恢复。")
            elif kind in ("fatal", "command_error", "login_error"):
                self.state["error"] = redact(event.get("error", "操作失败"))
                if kind != "command_error":
                    self._offline()

    def _offline(self):
        if self.state["logged_in_at"] is not None:
            self.state["last_duration"] = max(0, int(time.time() - self.state["logged_in_at"]))
        self.state.update(wechat="offline", qr=None, logged_in_at=None)

    def start(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                raise ValueError("EFB 已在运行")
            self.state.update(process="starting", error=None, wechat="initializing", qr=None, reminder_test=None)
            self.unresolved.clear()
            for health in self.health.values():
                health.update(status="unknown", last_success=None, last_received=None, last_error=None)
            env = dict(os.environ, PYTHONUNBUFFERED="1")
            if self.reminder_path:
                env["EFB_MANAGER_REMINDER_SETTINGS"] = str(self.reminder_path.resolve())
                env["EFB_MANAGER_SESSION_STATE"] = str(self.reminder_path.with_suffix(".session.json").resolve())
            if self.history:
                env.update(EFB_MANAGER_DB=str(self.history.path.resolve()), EFB_MANAGER_PROFILE=self.profile)
            try:
                self.process = subprocess.Popen(
                    [sys.executable, "-m", "efb_manager.runner", "--profile", self.profile],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", env=env)
            except OSError:
                self.state.update(process="stopped", wechat="offline")
                raise
            self.state["process"] = "running"
            self.reader = threading.Thread(target=self._read, args=(self.process,), daemon=True)
            self.reader.start()

    def _read(self, process):
        for line in process.stdout:
            if line.startswith(PREFIX):
                try:
                    self.event(json.loads(line[len(PREFIX):]))
                except (ValueError, KeyError):
                    pass
            else:
                with self.lock:
                    self.logs.append(redact(line.rstrip()))
        process.wait()
        with self.lock:
            self._offline()
            self.state["process"] = "stopped"
            if process.returncode and not self.state["error"]:
                self.state["error"] = "EFB 已退出，退出码 " + str(process.returncode)

    def command(self, action):
        with self.lock:
            if not self.process or self.process.poll() is not None:
                raise ValueError("请先启动 EFB")
            self.process.stdin.write(json.dumps(dict(action=action)) + "\n")
            self.process.stdin.flush()

    def stop(self):
        with self.lock:
            process = self.process
            if not process or process.poll() is not None:
                return
            self.state["process"] = "stopping"
            process.terminate()
        try:
            process.wait(timeout=12)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.wait(timeout=5)
        self.reader.join(timeout=5)
        # Join the reader before permitting a replacement child.
        with self.lock:
            self._offline()
            self.state["process"] = "stopped"
