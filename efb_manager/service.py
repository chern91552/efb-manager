import collections
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
        self.last_event = 0

    def snapshot(self):
        with self.lock:
            result = dict(self.state)
            started = result["logged_in_at"]
            result["online_seconds"] = max(0, int(time.time() - started)) \
                if started and result["wechat"] == "online" else 0
            result["logs"] = list(self.logs)
            result["profile"] = self.profile
            return result

    def event(self, event):
        with self.lock:
            kind = event["kind"]
            self.last_event = time.monotonic()
            if kind in ("waiting", "logging_in", "scanned", "confirming"):
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
            elif kind == "archive_error":
                self.state["archive_error"] = event["error"]
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
            self.state.update(process="starting", error=None, wechat="initializing", qr=None)
            env = dict(os.environ, PYTHONUNBUFFERED="1")
            if self.reminder_path:
                env["EFB_MANAGER_REMINDER_SETTINGS"] = str(self.reminder_path.resolve())
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
