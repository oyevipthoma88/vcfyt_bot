import asyncio
import json
import logging
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

logger = logging.getLogger("vcbot.database")


class Database:

    def __init__(self):
        self._mongo = None
        self._sqlite_path = "bot_data.db"
        self._use_mongo = bool(Config.MONGO_URI) and MONGO_AVAILABLE
        self._purging = False
        self._init_sqlite()

    def _is_quota_error(self, exc: Exception) -> bool:
        s = str(exc).lower()
        return "space quota" in s or "code': 8000" in s or "atlaserror" in s or "writes are blocked" in s

    async def connect(self):
        if self._use_mongo:
            try:
                client = motor.AsyncIOMotorClient(Config.MONGO_URI, serverSelectionTimeoutMS=5000)
                self._mongo = client["vc_bot"]
                print("[DB] Connected to MongoDB")
                await self._check_initial_quota()
            except Exception as e:
                logger.error("[DB] MongoDB connect error: %s; falling back to SQLite", e)
                self._use_mongo = False
                self._init_sqlite()
                print("[DB] Using local SQLite fallback")
        else:
            self._init_sqlite()
            print("[DB] Using local SQLite")

    async def _check_initial_quota(self):
        """On startup, verify if MongoDB cluster is write-blocked by Atlas M0 512MB quota."""
        if not self._use_mongo or self._mongo is None:
            return
        try:
            test_key = "__quota_probe__"
            await self._mongo.app_meta.update_one(
                {"key": test_key}, {"$set": {"value": "ok"}}, upsert=True
            )
            await self._mongo.app_meta.delete_one({"key": test_key})
        except Exception as e:
            if self._is_quota_error(e):
                logger.warning("[DB] Atlas quota exceeded (512MB limit)! Auto-triggering emergency purge...")
                await self.emergency_purge_non_critical()
            else:
                logger.warning("[DB] Initial write probe warning: %s", e)

    async def emergency_purge_non_critical(self) -> dict:
        """Drop non-critical collections to immediately reclaim space on Atlas M0."""
        return await self.clean_database()

    async def clean_database(self) -> dict:
        """Purge non-critical collections to reclaim quota and space."""
        if not self._use_mongo or self._mongo is None:
            return {"status": "sqlite_only", "dropped": []}
        if self._purging:
            return {"status": "in_progress", "dropped": []}
        self._purging = True
        dropped = []
        try:
            non_critical = ["audio_archive", "redeem_logs", "usage", "broadcast_chats"]
            for col_name in non_critical:
                try:
                    await self._mongo[col_name].drop()
                    dropped.append(col_name)
                    logger.warning("[DB] Dropped bloated non-critical collection '%s' to reclaim quota", col_name)
                except Exception as e:
                    logger.warning("[DB] Could not drop collection '%s': %r", col_name, e)
            
            try:
                await self._mongo.app_meta.delete_many({"key": {"$regex": "autocore|auto_core"}})
                dropped.append("app_meta:autocore")
            except Exception:
                pass
        except Exception as e:
            logger.error("[DB] Error during database clean: %r", e)
        finally:
            self._purging = False
        return {"status": "ok", "dropped": dropped}

    async def _safe_write(self, mongo_fn, sqlite_fn=None):
        """Execute a Mongo write with quota error detection, emergency cleanup, and SQLite fallback."""
        if self._use_mongo and self._mongo is not None:
            try:
                res = mongo_fn() if callable(mongo_fn) else mongo_fn
                if asyncio.iscoroutine(res):
                    return await res
                return res
            except Exception as e:
                if self._is_quota_error(e):
                    logger.warning("[DB] MongoDB quota full! Triggering auto-purge & using SQLite fallback")
                    asyncio.create_task(self.emergency_purge_non_critical())
                else:
                    logger.warning("[DB] Mongo write error (%s): %s", type(e).__name__, e)
                if sqlite_fn is not None:
                    try:
                        return sqlite_fn()
                    except Exception as sqle:
                        logger.warning("[DB] SQLite fallback write failed: %s", sqle)
                return None
        if sqlite_fn is not None:
            return sqlite_fn()
        return None

    async def _safe_read(self, mongo_fn, sqlite_fn=None):
        """Execute a Mongo read with automatic fallback to SQLite on error or miss."""
        if self._use_mongo and self._mongo is not None:
            try:
                res = mongo_fn() if callable(mongo_fn) else mongo_fn
                if asyncio.iscoroutine(res):
                    val = await res
                else:
                    val = res
                if val is not None:
                    return val
            except Exception as e:
                logger.warning("[DB] Mongo read failed: %s", e)
        if sqlite_fn is not None:
            try:
                return sqlite_fn()
            except Exception as sqle:
                logger.warning("[DB] SQLite fallback read failed: %s", sqle)
        return None

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
            CREATE TABLE IF NOT EXISTS premium (
                user_id       INTEGER PRIMARY KEY,
                premium_until TEXT,
                plan          TEXT,
                granted_at    TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS usage (
                user_id  INTEGER,
                date     TEXT,
                count    INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, date)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                referrer_id INTEGER,
                referred_id INTEGER,
                created_at  TEXT,
                PRIMARY KEY (referrer_id, referred_id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS referral_milestones (
                user_id    INTEGER,
                milestone  INTEGER,
                claimed_at TEXT,
                PRIMARY KEY (user_id, milestone)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS coupons (
                code       TEXT PRIMARY KEY,
                plan_id    TEXT NOT NULL,
                max_uses   INTEGER DEFAULT 1,
                used_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                created_by INTEGER
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS redeem_logs (
                code TEXT, plan_id TEXT, user_id INTEGER,
                username TEXT, name TEXT, redeemed_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS payment_requests (
                request_id TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                plan_id    TEXT NOT NULL,
                status     TEXT DEFAULT 'pending',
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                reviewed_by INTEGER
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
                live_volume INTEGER,
                loud INTEGER DEFAULT 120,
                crunch INTEGER DEFAULT 0,
                pregain INTEGER DEFAULT 80,
                turbo INTEGER DEFAULT 12,
                clarity INTEGER DEFAULT 14
            )
        """)

        for statement in (
            "ALTER TABLE settings ADD COLUMN auto INTEGER DEFAULT 0",
            "ALTER TABLE settings ADD COLUMN relay_volume INTEGER",
            "ALTER TABLE settings ADD COLUMN gain INTEGER",
            "ALTER TABLE settings ADD COLUMN treble INTEGER",
            "ALTER TABLE settings ADD COLUMN voice TEXT DEFAULT 'normal'",
            "ALTER TABLE settings ADD COLUMN live_volume INTEGER",
            "ALTER TABLE settings ADD COLUMN loud INTEGER DEFAULT 120",
            "ALTER TABLE settings ADD COLUMN crunch INTEGER DEFAULT 0",
            "ALTER TABLE settings ADD COLUMN pregain INTEGER DEFAULT 80",
            "ALTER TABLE settings ADD COLUMN turbo INTEGER DEFAULT 12",
            "ALTER TABLE settings ADD COLUMN clarity INTEGER DEFAULT 14",
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
        async def _m():
            row = await self._mongo.app_meta.find_one({"key": key})
            return row.get("value") if row else None
        def _s():
            rows = self._sql("SELECT value FROM app_meta WHERE key=?", (key,), fetch=True)
            return str(rows[0]["value"]) if rows else None
        return await self._safe_read(_m, _s)

    async def set_app_value(self, key: str, value: str):
        async def _m():
            await self._mongo.app_meta.update_one(
                {"key": key}, {"$set": {"value": value}}, upsert=True,
            )
        def _s():
            self._sql("INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)", (key, value))
        await self._safe_write(_m, _s)

    async def delete_app_value(self, key: str):
        async def _m():
            await self._mongo.app_meta.delete_one({"key": key})
        def _s():
            self._sql("DELETE FROM app_meta WHERE key=?", (key,))
        await self._safe_write(_m, _s)

    async def add_user(self, user_id: int, username: str, first_name: str,
                       string_session: str = "", extra: dict = None):
        ts = datetime.now(timezone.utc).isoformat()
        async def _m():
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
        def _s():
            existing = self._sql("SELECT 1 FROM users WHERE user_id=?", (user_id,), fetch=True)
            if existing:
                self._sql(
                    "UPDATE users SET username=?, first_name=?, extra_json=? WHERE user_id=?",
                    (username, first_name, json.dumps(extra or {}), user_id),
                )
                if string_session:
                    self._sql("UPDATE users SET string_session=? WHERE user_id=?", (string_session, user_id))
            else:
                self._sql(
                    "INSERT INTO users VALUES (?,?,?,?,?,?)",
                    (user_id, username, first_name, string_session, ts, json.dumps(extra or {})),
                )
        await self._safe_write(_m, _s)

    async def get_user(self, user_id: int) -> Optional[dict]:
        async def _m():
            return await self._mongo.users.find_one({"user_id": user_id})
        def _s():
            rows = self._sql("SELECT * FROM users WHERE user_id=?", (user_id,), fetch=True)
            return dict(rows[0]) if rows else None
        return await self._safe_read(_m, _s)

    async def update_string(self, user_id: int, string_session: str):
        async def _m():
            await self._mongo.users.update_one(
                {"user_id": user_id},
                {"$set": {"string_session": string_session}}, upsert=True,
            )
        def _s():
            self._sql("UPDATE users SET string_session=? WHERE user_id=?", (string_session, user_id))
        await self._safe_write(_m, _s)

    async def clear_string(self, user_id: int):
        await self.update_string(user_id, "")

    async def all_users(self) -> list:
        async def _m():
            return await self._mongo.users.find({}).to_list(None)
        def _s():
            rows = self._sql("SELECT * FROM users", fetch=True)
            return [dict(r) for r in rows] if rows else []
        res = await self._safe_read(_m, _s)
        return res if res is not None else []

    async def register_broadcast_chat(self, chat_id: int, title: str = ""):
        chat_id = int(chat_id)
        if chat_id >= 0:
            return
        ts = datetime.now(timezone.utc).isoformat()
        async def _m():
            await self._mongo.broadcast_chats.update_one(
                {"chat_id": chat_id},
                {"$set": {"title": title or "", "updated_at": ts}},
                upsert=True,
            )
        def _s():
            self._sql(
                "INSERT OR REPLACE INTO broadcast_chats (chat_id, title, updated_at) VALUES (?, ?, ?)",
                (chat_id, title or "", ts),
            )
        await self._safe_write(_m, _s)

    async def all_broadcast_chats(self) -> list[int]:
        async def _m():
            rows = await self._mongo.broadcast_chats.find({}, {"chat_id": 1}).to_list(None)
            return [int(row["chat_id"]) for row in rows if row.get("chat_id") is not None]
        def _s():
            rows = self._sql("SELECT chat_id FROM broadcast_chats", fetch=True)
            return [int(row["chat_id"]) for row in rows] if rows else []
        res = await self._safe_read(_m, _s)
        return res if res is not None else []

    async def get_archived_audio(self, source_file_id: str) -> Optional[dict]:
        if not source_file_id:
            return None
        async def _m():
            return await self._mongo.audio_archive.find_one({"source_file_id": source_file_id})
        def _s():
            rows = self._sql("SELECT * FROM audio_archive WHERE source_file_id=?", (source_file_id,), fetch=True)
            return dict(rows[0]) if rows else None
        return await self._safe_read(_m, _s)

    async def save_archived_audio(self, source_file_id: str, archive_file_id: str,
                                  title: str = "", file_type: str = "audio"):
        ts = datetime.now(timezone.utc).isoformat()
        async def _m():
            await self._mongo.audio_archive.update_one(
                {"source_file_id": source_file_id},
                {"$set": {"archive_file_id": archive_file_id, "title": title,
                          "file_type": file_type, "created_at": ts}}, upsert=True,
            )
        def _s():
            self._sql("INSERT OR REPLACE INTO audio_archive VALUES (?,?,?,?,?)",
                      (source_file_id, archive_file_id, title, file_type, ts))
        await self._safe_write(_m, _s)

    async def tag_file(self, user_id: int, tag_name: str, file_id: str,
                       file_type: str, caption: str = ""):
        ts = datetime.now(timezone.utc).isoformat()
        async def _m():
            await self._mongo.tagged.update_one(
                {"user_id": user_id, "tag_name": tag_name},
                {"$set": {"file_id": file_id, "file_type": file_type,
                          "caption": caption, "tagged_at": ts}},
                upsert=True,
            )
        def _s():
            self._sql("INSERT OR REPLACE INTO tagged VALUES (?,?,?,?,?,?)",
                      (user_id, tag_name, file_id, file_type, caption, ts))
        await self._safe_write(_m, _s)

    async def get_tag(self, user_id: int, tag_name: str) -> Optional[dict]:
        async def _m():
            return await self._mongo.tagged.find_one({"user_id": user_id, "tag_name": tag_name})
        def _s():
            rows = self._sql("SELECT * FROM tagged WHERE user_id=? AND tag_name=?",
                             (user_id, tag_name), fetch=True)
            return dict(rows[0]) if rows else None
        return await self._safe_read(_m, _s)

    async def list_tags(self, user_id: int) -> list:
        async def _m():
            return await self._mongo.tagged.find({"user_id": user_id}).to_list(None)
        def _s():
            rows = self._sql("SELECT * FROM tagged WHERE user_id=?", (user_id,), fetch=True)
            return [dict(r) for r in rows] if rows else []
        res = await self._safe_read(_m, _s)
        return res if res is not None else []

    async def delete_tag(self, user_id: int, tag_name: str):
        async def _m():
            await self._mongo.tagged.delete_one({"user_id": user_id, "tag_name": tag_name})
        def _s():
            self._sql("DELETE FROM tagged WHERE user_id=? AND tag_name=?", (user_id, tag_name))
        await self._safe_write(_m, _s)

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
        async def _m():
            await self._mongo.shared_audio.insert_one(document)
            return audio_id
        def _s():
            self._sql(
                "INSERT INTO shared_audio (audio_id,owner_id,title,file_id,file_type,caption,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (audio_id, owner_id, title, file_id, file_type, caption or "", ts),
            )
            return audio_id
        res = await self._safe_write(_m, _s)
        return res or audio_id

    async def list_user_audio(self, owner_id: int) -> list:
        async def _m():
            return await self._mongo.shared_audio.find(
                {"owner_id": owner_id}).sort("created_at", -1).to_list(None)
        def _s():
            rows = self._sql(
                "SELECT * FROM shared_audio WHERE owner_id=? ORDER BY audio_id DESC",
                (owner_id,), fetch=True)
            return [dict(r) for r in rows] if rows else []
        res = await self._safe_read(_m, _s)
        return res if res is not None else []

    async def list_owner_audio(self, owner_id: int) -> list:
        return await self.list_user_audio(owner_id)

    async def list_bot_audio(self, owner_id: int) -> list:
        return await self.list_owner_audio(owner_id)

    async def list_available_audio(self, user_id: int, owner_id: int) -> list:
        ids = {int(user_id), int(owner_id)}
        async def _m():
            return await self._mongo.shared_audio.find(
                {"owner_id": {"$in": list(ids)}}
            ).sort("created_at", -1).to_list(None)
        def _s():
            placeholders = ",".join("?" for _ in ids)
            rows = self._sql(
                f"SELECT * FROM shared_audio WHERE owner_id IN ({placeholders}) ORDER BY audio_id DESC",
                tuple(ids), fetch=True)
            return [dict(r) for r in rows] if rows else []
        res = await self._safe_read(_m, _s)
        return res if res is not None else []

    async def get_audio(self, audio_id: str) -> Optional[dict]:
        async def _m():
            return await self._mongo.shared_audio.find_one({"audio_id": str(audio_id)})
        def _s():
            rows = self._sql("SELECT * FROM shared_audio WHERE audio_id=?",
                             (str(audio_id),), fetch=True)
            return dict(rows[0]) if rows else None
        return await self._safe_read(_m, _s)

    async def delete_audio(self, owner_id: int, audio_id: str) -> bool:
        async def _m():
            result = await self._mongo.shared_audio.delete_one(
                {"audio_id": str(audio_id), "owner_id": owner_id})
            return bool(result.deleted_count)
        def _s():
            rows = self._sql(
                "SELECT audio_id FROM shared_audio WHERE audio_id=? AND owner_id=?",
                (str(audio_id), owner_id), fetch=True)
            if not rows:
                return False
            self._sql("DELETE FROM shared_audio WHERE audio_id=? AND owner_id=?",
                      (str(audio_id), owner_id))
            return True
        res = await self._safe_write(_m, _s)
        return bool(res)

    async def get_settings(self, user_id: int) -> dict:
        defaults = {
            "volume": Config.DEFAULT_VOLUME, "bass": Config.DEFAULT_BASS,
            "echo": 1 if Config.DEFAULT_ECHO else 0,
            "echo_level": Config.DEFAULT_ECHO_LEVEL, "boost": Config.DEFAULT_BOOST,
            "auto": 1 if Config.AUTO_MODE_DEFAULT else 0,
            "relay_volume": Config.RELAY_DEFAULT_VOLUME,
            "gain": Config.RELAY_DEFAULT_GAIN,
            "treble": Config.RELAY_DEFAULT_TREBLE,
            "voice": "normal",
            "live_volume": Config.LIVE_BOOST_DEFAULT,
            "loud": 120, "crunch": 0,
            "pregain": 80, "turbo": 12, "clarity": 14,
        }
        async def _m():
            return await self._mongo.settings.find_one({"user_id": user_id})
        def _s():
            rows = self._sql("SELECT * FROM settings WHERE user_id=?", (user_id,), fetch=True)
            return dict(rows[0]) if rows else None
        doc = await self._safe_read(_m, _s)
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
        async def _m():
            await self._mongo.settings.update_one(
                {"user_id": user_id}, {"$set": current}, upsert=True)
        def _s():
            self._sql("INSERT OR IGNORE INTO settings (user_id) VALUES (?)", (user_id,))
            self._sql(
                "UPDATE settings SET volume=?, bass=?, echo=?, echo_level=?, "
                "boost=?, auto=?, relay_volume=?, gain=?, treble=?, voice=?, live_volume=?, "
                "loud=?, crunch=?, pregain=?, turbo=?, clarity=? "
                "WHERE user_id=?",
                (current["volume"], current["bass"], int(current["echo"]),
                 current["echo_level"], current["boost"],
                 int(current.get("auto") or 0), current["relay_volume"],
                 current["gain"], current["treble"], current["voice"],
                 current["live_volume"],
                 int(current.get("loud") or 120), int(current.get("crunch") or 0),
                 int(current.get("pregain") or 80), int(current.get("turbo") or 12),
                 int(current.get("clarity") or 14), user_id),
            )
        await self._safe_write(_m, _s)

    async def get_linked_user_ids(self, user_id: int) -> list[int]:
        try:
            user = await self.get_user(user_id)
        except Exception:
            return [user_id]
        if not user:
            return [user_id]
        session = user.get("string_session", "")
        if not session:
            return [user_id]
        async def _m():
            cursor = self._mongo.users.find({"string_session": session}, {"user_id": 1})
            rows = await cursor.to_list(None)
            return [int(r["user_id"]) for r in rows if r.get("user_id") is not None]
        def _s():
            rows = self._sql("SELECT user_id FROM users WHERE string_session=?", (session,), fetch=True)
            return [int(r["user_id"]) for r in rows] if rows else []
        ids = await self._safe_read(_m, _s)
        return ids if ids else [user_id]

    async def get_premium(self, user_id: int) -> Optional[dict]:
        async def _m():
            return await self._mongo.premium.find_one({"user_id": user_id})
        def _s():
            rows = self._sql("SELECT * FROM premium WHERE user_id=?", (user_id,), fetch=True)
            return dict(rows[0]) if rows else None
        return await self._safe_read(_m, _s)

    async def set_premium(self, user_id: int, until_iso: str, plan: str = ""):
        ts = datetime.now(timezone.utc).isoformat()
        async def _m():
            await self._mongo.premium.update_one(
                {"user_id": user_id},
                {"$set": {"premium_until": until_iso, "plan": plan, "granted_at": ts}},
                upsert=True,
            )
        def _s():
            self._sql("INSERT OR REPLACE INTO premium VALUES (?,?,?,?)", (user_id, until_iso, plan, ts))
        await self._safe_write(_m, _s)

    async def revoke_premium(self, user_id: int) -> bool:
        async def _m():
            result = await self._mongo.premium.delete_one({"user_id": user_id})
            return bool(result.deleted_count)
        def _s():
            rows = self._sql("SELECT user_id FROM premium WHERE user_id=?", (user_id,), fetch=True)
            if not rows:
                return False
            self._sql("DELETE FROM premium WHERE user_id=?", (user_id,))
            return True
        res = await self._safe_write(_m, _s)
        return bool(res)

    async def get_usage(self, user_id: int, date_str: str) -> int:
        async def _m():
            doc = await self._mongo.usage.find_one({"user_id": user_id, "date": date_str})
            return int(doc.get("count", 0)) if doc else 0
        def _s():
            rows = self._sql("SELECT count FROM usage WHERE user_id=? AND date=?", (user_id, date_str), fetch=True)
            return int(rows[0]["count"]) if rows else 0
        res = await self._safe_read(_m, _s)
        return int(res or 0)

    async def increment_usage(self, user_id: int, date_str: str) -> int:
        async def _m():
            await self._mongo.usage.update_one(
                {"user_id": user_id, "date": date_str},
                {"$inc": {"count": 1}},
                upsert=True,
            )
        def _s():
            self._sql(
                "INSERT INTO usage (user_id, date, count) VALUES (?, ?, 1) "
                "ON CONFLICT(user_id, date) DO UPDATE SET count = count + 1",
                (user_id, date_str),
            )
        await self._safe_write(_m, _s)
        return await self.get_usage(user_id, date_str)

    async def add_referral(self, referrer_id: int, referred_id: int) -> bool:
        if referrer_id == referred_id:
            return False
        ts = datetime.now(timezone.utc).isoformat()
        async def _m():
            existing = await self._mongo.referrals.find_one(
                {"referrer_id": referrer_id, "referred_id": referred_id})
            if existing:
                return False
            await self._mongo.referrals.insert_one(
                {"referrer_id": referrer_id, "referred_id": referred_id, "created_at": ts})
            return True
        def _s():
            existing = self._sql(
                "SELECT 1 FROM referrals WHERE referrer_id=? AND referred_id=?",
                (referrer_id, referred_id), fetch=True)
            if existing:
                return False
            self._sql("INSERT INTO referrals VALUES (?,?,?)", (referrer_id, referred_id, ts))
            return True
        res = await self._safe_write(_m, _s)
        return bool(res)

    async def count_referrals(self, user_id: int) -> int:
        async def _m():
            return await self._mongo.referrals.count_documents({"referrer_id": user_id})
        def _s():
            rows = self._sql("SELECT COUNT(*) as cnt FROM referrals WHERE referrer_id=?", (user_id,), fetch=True)
            return int(rows[0]["cnt"]) if rows else 0
        res = await self._safe_read(_m, _s)
        return int(res or 0)

    async def referral_milestone_claimed(self, user_id: int, milestone: int) -> bool:
        async def _m():
            doc = await self._mongo.referral_milestones.find_one(
                {"user_id": user_id, "milestone": milestone})
            return doc is not None
        def _s():
            rows = self._sql(
                "SELECT 1 FROM referral_milestones WHERE user_id=? AND milestone=?",
                (user_id, milestone), fetch=True)
            return bool(rows)
        res = await self._safe_read(_m, _s)
        return bool(res)

    async def claim_referral_milestone(self, user_id: int, milestone: int):
        ts = datetime.now(timezone.utc).isoformat()
        async def _m():
            await self._mongo.referral_milestones.update_one(
                {"user_id": user_id, "milestone": milestone},
                {"$set": {"claimed_at": ts}}, upsert=True)
        def _s():
            self._sql("INSERT OR REPLACE INTO referral_milestones VALUES (?,?,?)", (user_id, milestone, ts))
        await self._safe_write(_m, _s)

    async def referral_stats(self, user_id: int) -> dict:
        count = await self.count_referrals(user_id)
        claimed = {}
        for m in (7, 15, 25):
            claimed[m] = await self.referral_milestone_claimed(user_id, m)
        return {"count": count, "claimed": claimed}

    async def create_coupon(self, code: str, plan_id: str, max_uses: int = 1,
                            created_by: int = 0) -> bool:
        code = code.strip().upper()
        if not code:
            return False
        ts = datetime.now(timezone.utc).isoformat()
        async def _m():
            existing = await self._mongo.coupons.find_one({"code": code})
            if existing:
                return False
            await self._mongo.coupons.insert_one(
                {"code": code, "plan_id": plan_id, "max_uses": max_uses,
                 "used_count": 0, "created_at": ts, "created_by": created_by})
            return True
        def _s():
            existing = self._sql("SELECT 1 FROM coupons WHERE code=?", (code,), fetch=True)
            if existing:
                return False
            self._sql("INSERT INTO coupons VALUES (?,?,?,?,?,?)", (code, plan_id, max_uses, 0, ts, created_by))
            return True
        res = await self._safe_write(_m, _s)
        return bool(res)

    async def get_coupon(self, code: str) -> Optional[dict]:
        code = code.strip().upper()
        async def _m():
            return await self._mongo.coupons.find_one({"code": code})
        def _s():
            rows = self._sql("SELECT * FROM coupons WHERE code=?", (code,), fetch=True)
            return dict(rows[0]) if rows else None
        return await self._safe_read(_m, _s)

    async def use_coupon(self, code: str) -> Optional[dict]:
        coupon = await self.get_coupon(code)
        if not coupon:
            return None
        if int(coupon.get("used_count", 0)) >= int(coupon.get("max_uses", 1)):
            return None
        async def _m():
            result = await self._mongo.coupons.find_one_and_update(
                {"code": coupon["code"],
                 "used_count": {"$lt": int(coupon.get("max_uses", 1))}},
                {"$inc": {"used_count": 1}},
                return_document=True,
            )
            return dict(result) if result else None
        def _s():
            conn = sqlite3.connect(self._sqlite_path)
            c = conn.cursor()
            c.execute(
                "UPDATE coupons SET used_count = used_count + 1 "
                "WHERE code=? AND used_count < max_uses",
                (coupon["code"],),
            )
            updated = c.rowcount
            conn.commit()
            conn.close()
            return coupon if updated > 0 else None
        return await self._safe_write(_m, _s)

    async def refund_coupon(self, code: str) -> None:
        code = (code or "").strip().upper()
        if not code:
            return
        async def _m():
            await self._mongo.coupons.find_one_and_update(
                {"code": code, "used_count": {"$gt": 0}},
                {"$inc": {"used_count": -1}},
            )
        def _s():
            self._sql("UPDATE coupons SET used_count = used_count - 1 WHERE code=? AND used_count > 0", (code,))
        await self._safe_write(_m, _s)

    async def log_redeem(self, code, plan_id, user_id, username="", name=""):
        ts = datetime.now(timezone.utc).isoformat()
        doc = {"code": code, "plan_id": plan_id, "user_id": user_id,
               "username": username or "", "name": name or "", "redeemed_at": ts}
        async def _m():
            await self._mongo.redeem_logs.insert_one(doc)
        def _s():
            self._sql("INSERT INTO redeem_logs VALUES (?,?,?,?,?,?)",
                      (code, plan_id, user_id, username or "", name or "", ts))
        await self._safe_write(_m, _s)

    async def redeem_logs(self, code: str = None, limit: int = 50) -> list:
        async def _m():
            q = {"code": code.upper()} if code else {}
            return await self._mongo.redeem_logs.find(q).sort("redeemed_at", -1).to_list(limit)
        def _s():
            if code:
                rows = self._sql("SELECT * FROM redeem_logs WHERE code=? ORDER BY redeemed_at DESC LIMIT ?",
                                 (code.upper(), limit), fetch=True)
            else:
                rows = self._sql("SELECT * FROM redeem_logs ORDER BY redeemed_at DESC LIMIT ?",
                                 (limit,), fetch=True)
            return [dict(r) for r in rows] if rows else []
        res = await self._safe_read(_m, _s)
        return res if res is not None else []

    async def all_coupons(self) -> list:
        async def _m():
            return await self._mongo.coupons.find({}).to_list(None)
        def _s():
            rows = self._sql("SELECT * FROM coupons ORDER BY created_at DESC", fetch=True)
            return [dict(r) for r in rows] if rows else []
        res = await self._safe_read(_m, _s)
        return res if res is not None else []

    async def delete_coupon(self, code: str) -> bool:
        code = code.strip().upper()
        async def _m():
            result = await self._mongo.coupons.delete_one({"code": code})
            return bool(result.deleted_count)
        def _s():
            rows = self._sql("SELECT 1 FROM coupons WHERE code=?", (code,), fetch=True)
            if not rows:
                return False
            self._sql("DELETE FROM coupons WHERE code=?", (code,))
            return True
        res = await self._safe_write(_m, _s)
        return bool(res)

    async def create_payment_request(self, user_id: int, plan_id: str) -> str:
        request_id = uuid.uuid4().hex[:12]
        ts = datetime.now(timezone.utc).isoformat()
        doc = {"request_id": request_id, "user_id": user_id, "plan_id": plan_id,
               "status": "pending", "created_at": ts,
               "reviewed_at": None, "reviewed_by": None}
        async def _m():
            await self._mongo.payment_requests.insert_one(doc)
            return request_id
        def _s():
            self._sql(
                "INSERT INTO payment_requests (request_id, user_id, plan_id, status, created_at, reviewed_at, reviewed_by) "
                "VALUES (?,?,?,?,?,?,?)",
                (request_id, user_id, plan_id, "pending", ts, None, 0))
            return request_id
        res = await self._safe_write(_m, _s)
        return res or request_id

    async def get_payment_request(self, request_id: str) -> Optional[dict]:
        async def _m():
            return await self._mongo.payment_requests.find_one({"request_id": request_id})
        def _s():
            rows = self._sql("SELECT * FROM payment_requests WHERE request_id=?", (request_id,), fetch=True)
            return dict(rows[0]) if rows else None
        return await self._safe_read(_m, _s)

    async def pending_payment_requests(self) -> list:
        async def _m():
            return await self._mongo.payment_requests.find({"status": "pending"}).to_list(None)
        def _s():
            rows = self._sql("SELECT * FROM payment_requests WHERE status='pending' ORDER BY created_at DESC", fetch=True)
            return [dict(r) for r in rows] if rows else []
        res = await self._safe_read(_m, _s)
        return res if res is not None else []

    async def update_payment_request(self, request_id: str, status: str,
                                     reviewed_by: int = 0):
        ts = datetime.now(timezone.utc).isoformat()
        async def _m():
            await self._mongo.payment_requests.update_one(
                {"request_id": request_id},
                {"$set": {"status": status, "reviewed_at": ts, "reviewed_by": reviewed_by}})
        def _s():
            self._sql(
                "UPDATE payment_requests SET status=?, reviewed_at=?, reviewed_by=? WHERE request_id=?",
                (status, ts, reviewed_by, request_id))
        await self._safe_write(_m, _s)


db = Database()
