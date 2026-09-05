"""
Persistent storage — MongoDB when MONGO_URI is set, otherwise local SQLite.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

try:
    import motor.motor_asyncio as motor
    MONGO_AVAILABLE = True
except ImportError:
    MONGO_AVAILABLE = False

from config import Config

class Database:
    """Collections/tables: users, tagged, settings, shared_audio, broadcast_chats."""

    def __init__(self):
        self._mongo = None
        self._sqlite_path = "bot_data.db"
        self._use_mongo = bool(Config.MONGO_URI) and MONGO_AVAILABLE

    async def connect(self):
        if self._use_mongo:
            client = motor.AsyncIOMotorClient(Config.MONGO_URI)
            self._mongo = client["vc_bot"]
            print("[DB] Connected to MongoDB")
        else:
            self._init_sqlite()
            print("[DB] Using local SQLite")

    def _init_sqlite(self):
        conn = sqlite3.connect(self._sqlite_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id        INTEGER PRIMARY KEY,
                username       TEXT,
                first_name     TEXT,
                string_session TEXT,
                joined_at      TEXT,
                extra_json     TEXT DEFAULT '{}'
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS tagged (
                user_id   INTEGER,
                tag_name  TEXT,
                file_id   TEXT,
                file_type TEXT,
                caption   TEXT,
                tagged_at TEXT,
                PRIMARY KEY (user_id, tag_name)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS shared_audio (
                audio_id   TEXT PRIMARY KEY,
                owner_id   INTEGER NOT NULL,
                title      TEXT NOT NULL,
                file_id    TEXT NOT NULL,
                file_type  TEXT NOT NULL,
                caption    TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_chats (
                chat_id    INTEGER PRIMARY KEY,
                title      TEXT DEFAULT '',
                updated_at TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS audio_archive (
                source_file_id TEXT PRIMARY KEY,
                archive_file_id TEXT NOT NULL,
                title TEXT DEFAULT '',
                file_type TEXT DEFAULT 'audio',
                created_at TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                user_id    INTEGER PRIMARY KEY,
                volume     INTEGER,
                bass       INTEGER,
                echo       INTEGER,
                echo_level INTEGER,
                boost      INTEGER,
                auto       INTEGER DEFAULT 0,
                relay_volume INTEGER,
                gain       INTEGER,
                treble     INTEGER,
                voice      TEXT DEFAULT 'normal',
                live_volume INTEGER
            )
        """)

        for statement in (
            "ALTER TABLE settings ADD COLUMN auto INTEGER DEFAULT 0",
            "ALTER TABLE settings ADD COLUMN relay_volume INTEGER",
            "ALTER TABLE settings ADD COLUMN gain INTEGER",
            "ALTER TABLE settings ADD COLUMN treble INTEGER",
            "ALTER TABLE settings ADD COLUMN voice TEXT DEFAULT 'normal'",
            "ALTER TABLE settings ADD COLUMN live_volume INTEGER",
        ):
            try:
                c.execute(statement)
            except sqlite3.OperationalError:
                pass
        conn.commit()
        conn.close()

    def _sql(self, query: str, params=(), fetch=False):
        conn = sqlite3.connect(self._sqlite_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(query, params)
        result = c.fetchall() if fetch else None
        conn.commit()
        conn.close()
        return result

    async def get_app_value(self, key: str) -> Optional[str]:
        if self._use_mongo:
            row = await self._mongo.app_meta.find_one({"key": key})
            return row.get("value") if row else None
        rows = self._sql("SELECT value FROM app_meta WHERE key=?", (key,), fetch=True)
        return str(rows[0]["value"]) if rows else None

    async def set_app_value(self, key: str, value: str):
        if self._use_mongo:
            await self._mongo.app_meta.update_one(
                {"key": key}, {"$set": {"value": value}}, upsert=True,
            )
            return
        self._sql("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)",
                  (key, value))

    async def add_user(self, user_id: int, username: str, first_name: str,
                       string_session: str = "", extra: dict = None):
        ts = datetime.now(timezone.utc).isoformat()
        if self._use_mongo:
            await self._mongo.users.update_one(
                {"user_id": user_id},
                {"$set": {"username": username, "first_name": first_name,
                          "extra": extra or {}},
                 "$setOnInsert": {"joined_at": ts}},
                upsert=True,
            )
            if string_session:
                await self._mongo.users.update_one(
                    {"user_id": user_id},
                    {"$set": {"string_session": string_session}},
                )
            return
        existing = await self.get_user(user_id)
        if existing:
            self._sql(
                "UPDATE users SET username=?, first_name=?, extra_json=? WHERE user_id=?",
                (username, first_name, json.dumps(extra or {}), user_id),
            )
            if string_session:
                await self.update_string(user_id, string_session)
        else:
            self._sql(
                "INSERT INTO users VALUES (?,?,?,?,?,?)",
                (user_id, username, first_name, string_session, ts,
                 json.dumps(extra or {})),
            )

    async def get_user(self, user_id: int) -> Optional[dict]:
        if self._use_mongo:
            return await self._mongo.users.find_one({"user_id": user_id})
        rows = self._sql("SELECT * FROM users WHERE user_id=?", (user_id,), fetch=True)
        return dict(rows[0]) if rows else None

    async def update_string(self, user_id: int, string_session: str):
        if self._use_mongo:
            await self._mongo.users.update_one(
                {"user_id": user_id},
                {"$set": {"string_session": string_session}}, upsert=True,
            )
        else:
            self._sql("UPDATE users SET string_session=? WHERE user_id=?",
                      (string_session, user_id))

    async def clear_string(self, user_id: int):
        await self.update_string(user_id, "")

    async def all_users(self) -> list:
        if self._use_mongo:
            return await self._mongo.users.find({}).to_list(None)
        rows = self._sql("SELECT * FROM users", fetch=True)
        return [dict(r) for r in rows] if rows else []

    async def register_broadcast_chat(self, chat_id: int, title: str = ""):
        """Persist a group used by VC commands for future broadcasts."""
        chat_id = int(chat_id)
        if chat_id >= 0:
            return
        ts = datetime.now(timezone.utc).isoformat()
        if self._use_mongo:
            await self._mongo.broadcast_chats.update_one(
                {"chat_id": chat_id},
                {"$set": {"title": title or "", "updated_at": ts}},
                upsert=True,
            )
            return
        self._sql(
            "INSERT OR REPLACE INTO broadcast_chats (chat_id, title, updated_at) VALUES (?, ?, ?)",
            (chat_id, title or "", ts),
        )

    async def all_broadcast_chats(self) -> list[int]:
        if self._use_mongo:
            rows = await self._mongo.broadcast_chats.find({}, {"chat_id": 1}).to_list(None)
            return [int(row["chat_id"]) for row in rows if row.get("chat_id") is not None]
        rows = self._sql("SELECT chat_id FROM broadcast_chats", fetch=True)
        return [int(row["chat_id"]) for row in rows] if rows else []

    async def get_archived_audio(self, source_file_id: str) -> Optional[dict]:
        if not source_file_id:
            return None
        if self._use_mongo:
            return await self._mongo.audio_archive.find_one(
                {"source_file_id": source_file_id}
            )
        rows = self._sql("SELECT * FROM audio_archive WHERE source_file_id=?",
                         (source_file_id,), fetch=True)
        return dict(rows[0]) if rows else None

    async def save_archived_audio(self, source_file_id: str, archive_file_id: str,
                                  title: str = "", file_type: str = "audio"):
        ts = datetime.now(timezone.utc).isoformat()
        if self._use_mongo:
            await self._mongo.audio_archive.update_one(
                {"source_file_id": source_file_id},
                {"$set": {"archive_file_id": archive_file_id, "title": title,
                          "file_type": file_type, "created_at": ts}}, upsert=True,
            )
            return
        self._sql("INSERT OR REPLACE INTO audio_archive VALUES (?,?,?,?,?)",
                  (source_file_id, archive_file_id, title, file_type, ts))

    async def tag_file(self, user_id: int, tag_name: str, file_id: str,
                       file_type: str, caption: str = ""):
        ts = datetime.now(timezone.utc).isoformat()
        if self._use_mongo:
            await self._mongo.tagged.update_one(
                {"user_id": user_id, "tag_name": tag_name},
                {"$set": {"file_id": file_id, "file_type": file_type,
                          "caption": caption, "tagged_at": ts}},
                upsert=True,
            )
        else:
            self._sql("INSERT OR REPLACE INTO tagged VALUES (?,?,?,?,?,?)",
                      (user_id, tag_name, file_id, file_type, caption, ts))

    async def get_tag(self, user_id: int, tag_name: str) -> Optional[dict]:
        if self._use_mongo:
            return await self._mongo.tagged.find_one(
                {"user_id": user_id, "tag_name": tag_name})
        rows = self._sql("SELECT * FROM tagged WHERE user_id=? AND tag_name=?",
                         (user_id, tag_name), fetch=True)
        return dict(rows[0]) if rows else None

    async def list_tags(self, user_id: int) -> list:
        if self._use_mongo:
            return await self._mongo.tagged.find({"user_id": user_id}).to_list(None)
        rows = self._sql("SELECT * FROM tagged WHERE user_id=?", (user_id,), fetch=True)
        return [dict(r) for r in rows] if rows else []

    async def delete_tag(self, user_id: int, tag_name: str):
        if self._use_mongo:
            await self._mongo.tagged.delete_one(
                {"user_id": user_id, "tag_name": tag_name})
        else:
            self._sql("DELETE FROM tagged WHERE user_id=? AND tag_name=?",
                      (user_id, tag_name))

    async def add_audio(self, owner_id: int, title: str, file_id: str,
                        file_type: str, caption: str = "") -> str:
        audio_id = uuid.uuid4().hex[:16]
        ts = datetime.now(timezone.utc).isoformat()
        title = title.strip()[:100] or "Untitled audio"
        document = {
            "audio_id": audio_id, "owner_id": owner_id, "title": title,
            "file_id": file_id, "file_type": file_type,
            "caption": caption or "", "created_at": ts,
        }
        if self._use_mongo:
            await self._mongo.shared_audio.insert_one(document)
            return audio_id
        self._sql(
            "INSERT INTO shared_audio (audio_id,owner_id,title,file_id,file_type,caption,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (audio_id, owner_id, title, file_id, file_type, caption or "", ts),
        )
        return audio_id

    async def list_user_audio(self, owner_id: int) -> list:
        if self._use_mongo:
            return await self._mongo.shared_audio.find(
                {"owner_id": owner_id}).sort("created_at", -1).to_list(None)
        rows = self._sql(
            "SELECT * FROM shared_audio WHERE owner_id=? ORDER BY audio_id DESC",
            (owner_id,), fetch=True)
        return [dict(r) for r in rows] if rows else []

    async def list_owner_audio(self, owner_id: int) -> list:
        if self._use_mongo:
            return await self._mongo.shared_audio.find(
                {"owner_id": owner_id}).sort("created_at", -1).to_list(None)
        rows = self._sql(
            "SELECT * FROM shared_audio WHERE owner_id=? ORDER BY audio_id DESC",
            (owner_id,), fetch=True)
        return [dict(r) for r in rows] if rows else []

    async def list_bot_audio(self, owner_id: int) -> list:
        return await self.list_owner_audio(owner_id)

    async def list_available_audio(self, user_id: int, owner_id: int) -> list:
        """Return the caller's private audio plus the owner's shared audio."""
        ids = {int(user_id), int(owner_id)}
        if self._use_mongo:
            return await self._mongo.shared_audio.find(
                {"owner_id": {"$in": list(ids)}}
            ).sort("created_at", -1).to_list(None)
        placeholders = ",".join("?" for _ in ids)
        rows = self._sql(
            f"SELECT * FROM shared_audio WHERE owner_id IN ({placeholders}) ORDER BY audio_id DESC",
            tuple(ids), fetch=True)
        return [dict(r) for r in rows] if rows else []

    async def get_audio(self, audio_id: str) -> Optional[dict]:
        if self._use_mongo:
            return await self._mongo.shared_audio.find_one({"audio_id": audio_id})
        rows = self._sql("SELECT * FROM shared_audio WHERE audio_id=?",
                         (str(audio_id),), fetch=True)
        return dict(rows[0]) if rows else None

    async def delete_audio(self, owner_id: int, audio_id: str) -> bool:
        if self._use_mongo:
            result = await self._mongo.shared_audio.delete_one(
                {"audio_id": str(audio_id), "owner_id": owner_id})
            return bool(result.deleted_count)
        rows = self._sql(
            "SELECT audio_id FROM shared_audio WHERE audio_id=? AND owner_id=?",
            (str(audio_id), owner_id), fetch=True)
        if not rows:
            return False
        self._sql("DELETE FROM shared_audio WHERE audio_id=? AND owner_id=?",
                  (str(audio_id), owner_id))
        return True

    async def get_settings(self, user_id: int) -> dict:
        defaults = {
            "volume": Config.DEFAULT_VOLUME, "bass": Config.DEFAULT_BASS,
            "echo": 1 if Config.DEFAULT_ECHO else 0,
            "echo_level": Config.DEFAULT_ECHO_LEVEL,             "boost": Config.DEFAULT_BOOST,
            "auto": 1 if Config.AUTO_MODE_DEFAULT else 0,
            "relay_volume": Config.RELAY_DEFAULT_VOLUME,
            "gain": Config.RELAY_DEFAULT_GAIN,
            "treble": Config.RELAY_DEFAULT_TREBLE,
            "voice": "normal",
            "live_volume": Config.LIVE_BOOST_DEFAULT,

        }
        if self._use_mongo:
            doc = await self._mongo.settings.find_one({"user_id": user_id})
        else:
            rows = self._sql("SELECT * FROM settings WHERE user_id=?",
                             (user_id,), fetch=True)
            doc = dict(rows[0]) if rows else None
        if doc:
            for k in defaults:
                if doc.get(k) is not None:
                    defaults[k] = doc[k]

            if defaults.get("boost") == 9 and defaults.get("treble") == 62:
                defaults["boost"] = Config.DEFAULT_BOOST
                defaults["treble"] = Config.RELAY_DEFAULT_TREBLE
        return defaults

    async def save_settings(self, user_id: int, **kwargs):
        current = await self.get_settings(user_id)
        current.update({k: v for k, v in kwargs.items() if v is not None})
        if self._use_mongo:
            await self._mongo.settings.update_one(
                {"user_id": user_id}, {"$set": current}, upsert=True)
        else:
            self._sql("INSERT OR IGNORE INTO settings (user_id) VALUES (?)", (user_id,))
            self._sql(
                "UPDATE settings SET volume=?, bass=?, echo=?, echo_level=?, "
                "boost=?, auto=?, relay_volume=?, gain=?, treble=?, voice=?, live_volume=? "
                "WHERE user_id=?",
                (current["volume"], current["bass"], int(current["echo"]),
                 current["echo_level"], current["boost"],
                 int(current.get("auto") or 0), current["relay_volume"],
                 current["gain"], current["treble"], current["voice"],
                 current["live_volume"], user_id),
            )

db = Database()
