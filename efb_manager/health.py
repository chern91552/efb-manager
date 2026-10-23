"""Observe successful WeChat sync checks without reading message bodies."""
import functools
import importlib
import time


def install_wechat_health(emit, module=None, core_class=None):
    module = module or importlib.import_module('efb_wechat_slave.vendor.itchat.components.login')
    core_class = core_class or importlib.import_module('efb_wechat_slave.vendor.itchat.core').Core
    original = module.sync_check
    last = [0.0, None]

    @functools.wraps(original)
    def sync(core, *args, **kwargs):
        try:
            result = original(core, *args, **kwargs)
        except Exception as exc:
            status, error = 'error', type(exc).__name__
            report(status, error)
            raise
        else:
            report('ok' if result is not None else 'error', None if result is not None else '会话同步失败')
            return result

    def report(status, error):
        now = time.time()
        if status != last[1] or now - last[0] >= 30:
            emit('health', component='wechat', status=status, at=now, error=error)
            last[:] = [now, status]

    module.sync_check = sync
    core_class.sync_check = sync
