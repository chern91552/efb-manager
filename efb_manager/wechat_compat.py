"""Scoped adapter for the legacy EWS 2.0.7 login implementation.

UOS QR parameters and headers follow the official EWS master implementation:
https://github.com/ehForwarderBot/efb-wechat-slave/tree/master/efb_wechat_slave/vendor/itchat
Upstream EWS contributors; AGPL-3.0-or-later (see LICENSE).
This adapter requires a genuine successful WeChat authentication response.
"""
import importlib
import importlib.metadata
import inspect
import re
import secrets
import time
from urllib.parse import urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from .uos_constants import CLIENT_VERSION, EXTSPAM


class WeChatLoginError(RuntimeError):
    pass


def install_legacy_login(version=None, module=None, core_class=None):
    version = version or importlib.metadata.version("efb-wechat-slave")
    if version != "2.0.7":
        return False
    module = module or importlib.import_module("efb_wechat_slave.vendor.itchat.components.login")
    source = inspect.getsource(module.process_login_info)
    if "client-version" in source and "extspam" in source:
        return False  # Do not overwrite an existing UOS patch.
    if core_class is None:
        core_class = importlib.import_module("efb_wechat_slave.vendor.itchat.core").Core
    config = module.config

    def get_uuid(core):
        try:
            response = core.s.get(config.BASE_URL + "/jslogin", params={
                "appid": "wx782c26e4c19acffb", "fun": "new",
                "redirect_uri": "https://wx.qq.com/cgi-bin/mmwebwx-bin/webwxnewloginpage?mod=desktop",
                "lang": "zh_CN"}, headers={"User-Agent": core.user_agent}, timeout=(10, 60))
        except Exception as exc:
            raise WeChatLoginError("获取微信二维码失败（" + type(exc).__name__ + "），请检查服务器网络") from None
        match = re.search(r'window.QRLogin.code\s*=\s*(\d+);\s*window.QRLogin.uuid\s*=\s*"([^"\s]+)"', response.text)
        if match and match.group(1) == "200":
            core.uuid = match.group(2)
            return core.uuid
        raise WeChatLoginError("微信未返回有效二维码，请稍后重新发起登录")

    def process(core, content):
        match = re.search(r'window.redirect_uri="([^"\s]+)";', content)
        if not match:
            raise WeChatLoginError("微信未返回登录确认地址，请重新扫码")
        redirect = match.group(1)
        parsed = urlsplit(redirect)
        if (parsed.scheme != "https" or parsed.username or parsed.password or
                not re.fullmatch(r"wx\d*\.qq\.com|web\d*\.wechat\.com", parsed.hostname or "") or
                not parsed.path.startswith("/cgi-bin/mmwebwx-bin/")):
            raise WeChatLoginError("微信返回了不受支持的登录确认地址")
        headers = {"User-Agent": core.user_agent,
                   "client-version": getattr(config, "UOS_PATCH_CLIENT_VERSION", CLIENT_VERSION),
                   "extspam": getattr(config, "UOS_PATCH_EXTSPAM", EXTSPAM),
                   "referer": "https://wx.qq.com/?&lang=zh_CN&target=t"}
        try:
            response = core.s.get(redirect, headers=headers, allow_redirects=False, timeout=(10, 60))
        except Exception as exc:
            raise WeChatLoginError("微信登录确认请求失败（" + type(exc).__name__ + "），请检查服务器网络") from None
        if response.status_code != 200:
            raise WeChatLoginError("微信登录接口返回 HTTP " + str(response.status_code) + "，尚未完成认证")
        text = response.text.lstrip("\ufeff \r\n\t")
        if text.lower().startswith(("<!doctype html", "<html")):
            raise WeChatLoginError("微信登录返回 HTML 网页而非认证结果，可能是网页版登录限制或接口兼容问题；请确认账号能登录微信网页版")
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            raise WeChatLoginError("微信登录返回了无法解析的认证结果，请稍后重试；未保存响应正文或登录票据") from None
        if root.tag != "error":
            raise WeChatLoginError("微信未返回预期的认证结果")
        ret = root.findtext("ret", "").strip()
        if ret != "0":
            code = ret if re.fullmatch(r"-?\d{1,10}", ret) else "未知"
            raise WeChatLoginError("微信拒绝登录（错误码 " + code + "），请检查账号网页版登录状态")
        cookies = core.s.cookies.get_dict()
        sid = root.findtext("wxsid") or cookies.get("wxsid", "")
        uin = root.findtext("wxuin") or cookies.get("wxuin", "")
        ticket = root.findtext("pass_ticket", "")
        if not all((sid, uin, ticket)):
            raise WeChatLoginError("微信认证结果缺少必要登录凭据，尚未登录成功")
        skey = root.findtext("skey", "")
        device = "e" + str(secrets.randbelow(10 ** 15)).zfill(15)
        base_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rsplit("/", 1)[0], "", ""))
        core.loginInfo.update(url=base_url,
            fileUrl="https://file." + parsed.hostname + "/cgi-bin/mmwebwx-bin",
            syncUrl="https://webpush." + parsed.hostname + "/cgi-bin/mmwebwx-bin",
            deviceid=device, logintime=int(time.time() * 1000),
            skey=skey, wxsid=sid, wxuin=uin, pass_ticket=ticket,
            BaseRequest={"Skey": skey, "Sid": sid, "Uin": uin, "DeviceID": device})
        return True

    module.get_QRuuid = get_uuid
    # load_components(Core) binds this method when the vendor package is imported.
    core_class.get_QRuuid = get_uuid
    module.process_login_info = process
    return True
