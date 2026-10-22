"""Text-only message archive. Never reads media streams or paths."""
import functools
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid


PLACEHOLDERS = {
    "Image": "[图片]", "Sticker": "[表情]", "Animation": "[动画]",
    "Voice": "[语音]", "Audio": "[音频]", "Video": "[视频]",
    "File": "[文件]", "Link": "[链接]", "Location": "[位置]",
    "Unsupported": "[不支持的消息]", "Status": "[状态消息]",
}


class History:
    def __init__(self, path):
        self.path = Path(path)
        self.maintenance_lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY,
                    profile TEXT NOT NULL, channel TEXT NOT NULL,
                    chat_id TEXT NOT NULL, chat_name TEXT NOT NULL,
                    chat_type TEXT NOT NULL, sender TEXT NOT NULL,
                    direction TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
                    message_id TEXT NOT NULL, captured_at REAL NOT NULL,
                    delivery TEXT NOT NULL,
                    UNIQUE(profile, channel, chat_id, direction, message_id)
                );
                CREATE INDEX IF NOT EXISTS messages_chat
                    ON messages(profile, channel, chat_id, id DESC);
                CREATE INDEX IF NOT EXISTS messages_oldest ON messages(captured_at, id);
                CREATE TABLE IF NOT EXISTS archive_config (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    record_private INTEGER NOT NULL DEFAULT 1,
                    record_group INTEGER NOT NULL DEFAULT 1,
                    record_public INTEGER NOT NULL DEFAULT 0,
                    max_mb INTEGER NOT NULL DEFAULT 100,
                    total_removed INTEGER NOT NULL DEFAULT 0,
                    last_removed INTEGER NOT NULL DEFAULT 0,
                    cleaned_at REAL
                );
                INSERT OR IGNORE INTO archive_config(id) VALUES(1);
            """)
        if os.name == "posix":
            self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, item):
        # Enforce the policy again at the persistence boundary.
        body = item["body"] if item["kind"] == "Text" else PLACEHOLDERS.get(item["kind"], "[其他消息]")
        values = {**item, "body": body}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            policy = db.execute("SELECT * FROM archive_config WHERE id=1").fetchone()
            key = {"Public": "record_public", "Group": "record_group"}.get(item["chat_type"], "record_private")
            if not policy[key]:
                return False
            db.execute("""INSERT INTO messages
                (profile,channel,chat_id,chat_name,chat_type,sender,direction,kind,body,message_id,captured_at,delivery)
                VALUES (:profile,:channel,:chat_id,:chat_name,:chat_type,:sender,:direction,:kind,:body,:message_id,:captured_at,:delivery)
                ON CONFLICT(profile,channel,chat_id,direction,message_id) DO UPDATE SET
                chat_name=excluded.chat_name, sender=excluded.sender,
                kind=excluded.kind, body=excluded.body, delivery=excluded.delivery
            """, values)
        return True

    def disk_bytes(self):
        total = 0
        for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
            try:
                total += path.stat().st_size
            except FileNotFoundError:
                pass
        return total

    def settings(self):
        with self.connect() as db:
            result = dict(db.execute("SELECT * FROM archive_config WHERE id=1").fetchone())
        for key in ("record_private", "record_group", "record_public"):
            result[key] = bool(result[key])
        result.pop("id")
        result["disk_bytes"] = self.disk_bytes()
        return result

    def update_settings(self, body):
        flags = ("record_private", "record_group", "record_public")
        if any(type(body.get(key)) is not bool for key in flags):
            raise ValueError("归档开关必须为布尔值")
        size = body.get("max_mb")
        if type(size) is not int or not 1 <= size <= 10240:
            raise ValueError("消息库上限需为 1 到 10240 MiB 的整数")
        with self.connect() as db:
            db.execute("UPDATE archive_config SET record_private=?,record_group=?,record_public=?,max_mb=? WHERE id=1",
                       [int(body[k]) for k in flags] + [size])
        return self.settings()

    def enforce_limit(self):
        """Periodically trim oldest records and physically compact SQLite files."""
        with self.maintenance_lock:
            limit = self.settings()["max_mb"] * 1024 * 1024
            if self.disk_bytes() <= limit:
                return 0
            with self.connect() as db:
                # Flush/truncate WAL first: temporary journal growth is not live history.
                if db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
                    return 0  # An active reader/writer will be retried next cycle.
                if self.disk_bytes() <= limit:
                    return 0
                page_size = db.execute("PRAGMA page_size").fetchone()[0]
                def live_bytes():
                    return (db.execute("PRAGMA page_count").fetchone()[0] -
                            db.execute("PRAGMA freelist_count").fetchone()[0]) * page_size
                removed = 0
                # Retain a margin so normal writes do not require constant compaction.
                if live_bytes() > limit - 32768:
                    while live_bytes() > int(limit * .85):
                        count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                        if not count:
                            break
                        batch = max(1, count // 10)
                        removed += db.execute("DELETE FROM messages WHERE id IN (SELECT id FROM messages ORDER BY captured_at,id LIMIT ?)", (batch,)).rowcount
                        db.commit()
                db.execute("UPDATE archive_config SET last_removed=?,total_removed=total_removed+?,cleaned_at=? WHERE id=1",
                           (removed, removed, time.time()))
                db.commit()
                db.execute("VACUUM")
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return removed

    @staticmethod
    def key(channel, chat_id):
        return json.dumps([channel, chat_id], ensure_ascii=False, separators=(",", ":"))

    def conversations(self, profile, query=""):
        with self.connect() as db:
            rows = db.execute("""WITH latest AS (
                SELECT channel, chat_id, MAX(id) AS last_id, COUNT(*) AS count
                FROM messages WHERE profile=? GROUP BY channel, chat_id
            ) SELECT m.*, latest.count FROM latest
              JOIN messages m ON m.id=latest.last_id ORDER BY m.id DESC""", (profile,)).fetchall()
        return [dict(key=self.key(r["channel"], r["chat_id"]), name=r["chat_name"],
                     chat_type=r["chat_type"], count=r["count"], preview=r["body"][:120],
                     last_at=r["captured_at"], last_id=r["id"])
                for r in rows if query.casefold() in r["chat_name"].casefold()]

    def messages(self, profile, key, query="", kind="all", before=None, limit=50):
        channel, chat_id = json.loads(key)
        if not isinstance(channel, str) or not isinstance(chat_id, str):
            raise ValueError("会话参数无效")
        clauses, params = ["profile=?", "channel=?", "chat_id=?"], [profile, channel, chat_id]
        if query:
            clauses.append("kind='Text' AND instr(lower(body),lower(?))>0")
            params.append(query)
        if kind == "text":
            clauses.append("kind='Text'")
        elif kind == "other":
            clauses.append("kind<>'Text'")
        elif kind != "all":
            raise ValueError("类型参数无效")
        if before is not None:
            clauses.append("id<?")
            params.append(int(before))
        limit = max(1, min(int(limit), 100))
        with self.connect() as db:
            rows = db.execute("SELECT id,sender,direction,kind,body,captured_at,delivery FROM messages WHERE " +
                              " AND ".join(clauses) + " ORDER BY id DESC LIMIT ?", params + [limit + 1]).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        return dict(items=[dict(r) for r in reversed(rows)],
                    next_before=rows[-1]["id"] if more else None)

    def stats(self, profile):
        with self.connect() as db:
            count = db.execute("SELECT COUNT(*) FROM messages WHERE profile=?", (profile,)).fetchone()[0]
            chats = db.execute("SELECT COUNT(*) FROM (SELECT 1 FROM messages WHERE profile=? GROUP BY channel,chat_id)", (profile,)).fetchone()[0]
        return dict(messages=count, conversations=chats)


def capture(message, profile):
    """Extract only safe scalar fields, before upstream can mutate the message."""
    chat = getattr(message, "chat", None)
    channel = str(getattr(chat, "module_id", "") or getattr(getattr(chat, "channel", None), "channel_id", ""))
    if channel.split("#")[0] != "blueset.wechat" or str(getattr(chat, "uid", "")).startswith("__"):
        return None
    kind = getattr(getattr(message, "type", None), "name", "Unsupported")
    if kind == "Status":
        return None
    author = getattr(message, "author", None)
    classes = {c.__name__ for c in type(chat).__mro__}
    chat_type = "Group" if "GroupChat" in classes else "Private" if "PrivateChat" in classes else "Unknown"
    vendor = getattr(chat, "vendor_specific", {}) or {}
    # Official EWS marks the self-only private chat as is_mp too. Keep it private.
    self_chat = getattr(chat, "self", None)
    other = getattr(chat, "other", None)
    if vendor.get("is_mp") and not (self_chat is not None and other is self_chat):
        chat_type = "Public"
    target = str(getattr(getattr(message, "deliver_to", None), "channel_id", ""))
    is_self = any(c.__name__ == "SelfChatMember" for c in type(author).__mro__)
    return dict(profile=profile, channel=channel, chat_id=str(chat.uid),
                chat_name=str(getattr(chat, "display_name", None) or getattr(chat, "name", "未知会话")),
                chat_type=chat_type,
                sender=str(getattr(author, "display_name", None) or getattr(author, "name", "未知发送者")),
                direction="out" if target.split("#")[0] == "blueset.wechat" or is_self else "in",
                kind=kind, body=str(getattr(message, "text", "") or "") if kind == "Text" else PLACEHOLDERS.get(kind, "[其他消息]"),
                message_id=str(getattr(message, "uid", "") or uuid.uuid4()),
                captured_at=time.time(), delivery="pending")


def install_recorder(coordinator, history, profile, on_error):
    original = coordinator.send_message

    @functools.wraps(original)
    def send(message):
        item = None
        try:
            item = capture(message, profile)
            if item:
                history.save(item)
        except Exception:
            on_error()
        try:
            result = original(message)
        except BaseException:
            if item:
                item["delivery"] = "failed"
                safe_save(item)
            raise
        if item:
            item["delivery"] = "forwarded" if result is not None else "no_result"
            safe_save(item)
        return result

    def safe_save(item):
        try:
            history.save(item)
        except Exception:
            on_error()

    coordinator.send_message = send
