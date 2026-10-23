"""Per-profile reminder settings and one attempt per authenticated session."""
import json
import math
import os
from pathlib import Path
import tempfile
import threading


def validate_settings(data):
    enabled, hours = data.get('enabled'), data.get('hours')
    if not isinstance(enabled, bool):
        raise ValueError('请选择是否启用提醒')
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not math.isfinite(hours) or not 0.01 <= hours <= 8760:
        raise ValueError('提醒时长须为 0.01 到 8760 小时')
    return {'enabled': enabled, 'hours': hours}


class ReminderSettings:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def read(self):
        try:
            return validate_settings(json.loads(self.path.read_text()))
        except FileNotFoundError:
            return {'enabled': False, 'hours': 24}

    def update(self, data):
        result = validate_settings(data)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=self.path.parent, prefix='.reminder-')
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump(result, f)
                os.replace(name, self.path)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        return result


class LoginReminder:
    def __init__(self):
        self.started = None
        self.attempted = False

    def login(self, now):
        self.started = now
        self.attempted = False

    def tick(self, bot, online, busy, settings, now):
        if (not settings['enabled'] or not online or busy or self.started is None
                or self.attempted or not bot.alive):
            return False
        elapsed = max(0, now - self.started)
        if elapsed < settings['hours'] * 3600:
            return False
        # A timeout may follow successful delivery; never blindly send twice.
        self.attempted = True
        bot.file_helper.send(
            '【EFB Manager 登录到期提醒】\n'
            f'本次微信会话在线时长已达到设定的 {settings["hours"]:g} 小时'
            f'（当前约 {elapsed / 3600:.2f} 小时）。\n'
            '请按需打开 EFB Manager 检查或重新登录。此提醒不代表微信已失效，不会自动退出。')
        return True
