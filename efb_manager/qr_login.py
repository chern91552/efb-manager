"""Skip cached-session restore and phone push confirmation for one authentication."""
from contextlib import contextmanager
import importlib


@contextmanager
def fresh_qr_login(module=None, core_class=None):
    module = module or importlib.import_module("efb_wechat_slave.vendor.itchat.components.login")
    core_class = core_class or importlib.import_module("efb_wechat_slave.vendor.itchat.core").Core
    push = module.push_login
    restore = core_class.load_login_status
    try:
        # Keep hotReload enabled: upstream can save the newly authenticated session.
        # No cache file is removed, renamed or read by this context manager.
        module.push_login = lambda core: False
        core_class.load_login_status = lambda core, *args, **kwargs: False
        yield
    finally:
        module.push_login = push
        core_class.load_login_status = restore


@contextmanager
def cached_login_then_wait(wait_for_scan, module=None, core_class=None):
    """Try upstream cache restore, but require explicit consent before QR login."""
    module = module or importlib.import_module('efb_wechat_slave.vendor.itchat.components.login')
    core_class = core_class or importlib.import_module('efb_wechat_slave.vendor.itchat.core').Core
    login, restore, push = core_class.login, core_class.load_login_status, module.push_login

    def restore_cached(core, *args, **kwargs):
        try:
            return restore(core, *args, **kwargs)
        except Exception as exc:
            print('缓存会话恢复失败（' + type(exc).__name__ + '），等待手动扫码。', flush=True)
            return False

    def login_after_click(core, *args, **kwargs):
        wait_for_scan()
        return login(core, *args, **kwargs)

    try:
        core_class.load_login_status = restore_cached
        core_class.login = login_after_click
        module.push_login = lambda core: False
        yield
    finally:
        core_class.login = login
        core_class.load_login_status = restore
        module.push_login = push
