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
