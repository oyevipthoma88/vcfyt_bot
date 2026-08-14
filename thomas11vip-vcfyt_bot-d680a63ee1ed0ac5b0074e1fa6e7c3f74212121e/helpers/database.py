"""
Persistent storage — MongoDB when MONGO_URI is set, otherwise local SQLite.
"""

import json
import sqlite3
from datetime import datetime
from typing import Optional

try:
    import motor.motor_asyncio as motor
    MONGO_AVAILABLE = True
except ImportError:
    MONGO_AVAILABLE = False

from config import Config


class Database:
    """Collections/tables: users, tagged, settings."""

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

    # ── SQLite bootstrap ─────────────────────────────────────────────────────
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
            CREATE TABLE IF NOT EXISTS settings (
                user_id    INTEGER PRIMARY KEY,
                volume     INTEGER,
                bass       INTEGER,
                echo       INTEGER,
                echo_level INTEGER,
                boost      INTEGER,
                auto       INTEGER DEFAULT 0
            )
        """)
        # Migration for databases created before AUTO mode existed.
        try:
            c.execute("ALTER TABLE settings ADD COLUMN auto INTEGER DEFAULT 0")
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

    # ── USERS ────────────────────────────────────────────────────────────────
    async def add_user(self, user_id: int, username: str, first_name: str,
                       string_session: str = "", extra: dict = None):
        ts = datetime.utcnow().isoformat()
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

    # ── TAGGED FILES ─────────────────────────────────────────────────────────
    async def tag_file(self, user_id: int, tag_name: str, file_id: str,
                       file_type: str, caption: str = ""):
        ts = datetime.utcnow().isoformat()
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

    # ── PER-USER AUDIO SETTINGS ──────────────────────────────────────────────
    async def get_settings(self, user_id: int) -> dict:
        defaults = {
            "volume": Config.DEFAULT_VOLUME, "bass": Config.DEFAULT_BASS,
            "echo": 1 if Config.DEFAULT_ECHO else 0,
            "echo_level": Config.DEFAULT_ECHO_LEVEL, "boost": Config.DEFAULT_BOOST,
            "auto": 1 if Config.AUTO_MODE_DEFAULT else 0,
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
                "boost=?, auto=? WHERE user_id=?",
                (current["volume"], current["bass"], int(current["echo"]),
                 current["echo_level"], current["boost"],
                 int(current.get("auto") or 0), user_id),
            )


db = Database()
