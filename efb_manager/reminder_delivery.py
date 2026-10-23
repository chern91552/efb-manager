"""Mirror successful reminder sends into EFB without sending to WeChat again."""
from collections import OrderedDict
import json
import threading


class ReminderSyncError(RuntimeError):
    """WeChat accepted the message, but EFB synchronization failed."""


class ReminderDelivery:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.original = coordinator.send_message
        self.lock = threading.Lock()
        self.recent = OrderedDict()
        self.managed = OrderedDict()
        coordinator.send_message = self.observe

    @staticmethod
    def key(message):
        chat = getattr(message, 'chat', None)
        channel = str(getattr(chat, 'module_id', ''))
        target = str(getattr(getattr(message, 'deliver_to', None), 'channel_id', ''))
        if channel.split('#')[0] != 'blueset.wechat' or target.split('#')[0] == 'blueset.wechat':
            return None
        if getattr(message, 'edit', False):
            return None
        return channel, str(chat.uid), str(message.uid)

    @staticmethod
    def remember(cache, key):
        cache[key] = True
        cache.move_to_end(key)
        while len(cache) > 4096:
            cache.popitem(last=False)

    def observe(self, message):
        key = self.key(message)
        if key is not None:
            with self.lock:
                if key in self.managed:
                    return message
                self.remember(self.recent, key)
        return self.original(message)

    def build_message(self, channel, sent, text):
        from ehforwarderbot import Message, MsgType
        from ehforwarderbot.types import MessageID
        if getattr(self.coordinator, 'master', None) is None:
            raise RuntimeError('Master channel is not ready')
        msg_id = getattr(sent, 'id', None)
        if msg_id is None:
            raise RuntimeError('WeChat did not return a message ID')
        chat = channel.chats.wxpy_chat_to_efb_chat(channel.bot.file_helper)
        if chat.self is None:
            raise RuntimeError('File helper has no self member')
        return Message(chat=chat, author=chat.self, type=MsgType.Text, text=text,
                       uid=MessageID(json.dumps([[str(msg_id)]])), deliver_to=self.coordinator.master)

    def forward(self, channel, sent, text):
        try:
            message = self.build_message(channel, sent, text)
            key = self.key(message)
            if key is None:
                raise RuntimeError('Invalid reminder destination')
            with self.lock:
                already_forwarded = key in self.recent or key in self.managed
                self.remember(self.managed, key)
            # Echo may reach EFB before send() returns. Whichever arrives first wins.
            if not already_forwarded:
                # original includes the archive wrapper; destination is the master only.
                self.original(message)
        except Exception as exc:
            raise ReminderSyncError(type(exc).__name__) from exc
