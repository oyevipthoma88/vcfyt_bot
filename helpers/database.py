
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

    async def delete_app_value(self, key: str):
        if self._use_mongo:
            await self._mongo.app_meta.delete_one({"key": key})
            return
        self._sql("DELETE FROM app_meta WHERE key=?", (key,))

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

    # ── Linked accounts (same string session) ────────────────

    async def get_linked_user_ids(self, user_id: int) -> list[int]:
        """Return all user IDs that share the same string session as user_id."""
        try:
            user = await self.get_user(user_id)
        except Exception:
            return [user_id]
        if not user:
            return [user_id]
        session = user.get("string_session", "")
        if not session:
            return [user_id]
        if self._use_mongo:
            cursor = self._mongo.users.find(
                {"string_session": session}, {"user_id": 1})
            rows = await cursor.to_list(None)
            ids = [int(r["user_id"]) for r in rows if r.get("user_id") is not None]
            return ids if ids else [user_id]
        rows = self._sql(
            "SELECT user_id FROM users WHERE string_session=?",
            (session,), fetch=True)
        ids = [int(r["user_id"]) for r in rows] if rows else []
        return ids if ids else [user_id]

    # ── Premium ──────────────────────────────────────────────

    async def get_premium(self, user_id: int) -> Optional[dict]:
        if self._use_mongo:
            return await self._mongo.premium.find_one({"user_id": user_id})
        rows = self._sql("SELECT * FROM premium WHERE user_id=?", (user_id,), fetch=True)
        return dict(rows[0]) if rows else None

    async def set_premium(self, user_id: int, until_iso: str, plan: str = ""):
        ts = datetime.now(timezone.utc).isoformat()
        if self._use_mongo:
            await self._mongo.premium.update_one(
                {"user_id": user_id},
                {"$set": {"premium_until": until_iso, "plan": plan, "granted_at": ts}},
                upsert=True,
            )
        else:
            self._sql("INSERT OR REPLACE INTO premium VALUES (?,?,?,?)",
                      (user_id, until_iso, plan, ts))

    async def revoke_premium(self, user_id: int) -> bool:
        if self._use_mongo:
            result = await self._mongo.premium.delete_one({"user_id": user_id})
            return bool(result.deleted_count)
        rows = self._sql("SELECT user_id FROM premium WHERE user_id=?", (user_id,), fetch=True)
        if not rows:
            return False
        self._sql("DELETE FROM premium WHERE user_id=?", (user_id,))
        return True

    # ── Usage tracking ───────────────────────────────────────

    async def get_usage(self, user_id: int, date_str: str) -> int:
        if self._use_mongo:
            doc = await self._mongo.usage.find_one(
                {"user_id": user_id, "date": date_str})
            return int(doc.get("count", 0)) if doc else 0
        rows = self._sql("SELECT count FROM usage WHERE user_id=? AND date=?",
                         (user_id, date_str), fetch=True)
        return int(rows[0]["count"]) if rows else 0

    async def increment_usage(self, user_id: int, date_str: str) -> int:
        if self._use_mongo:
            await self._mongo.usage.update_one(
                {"user_id": user_id, "date": date_str},
                {"$inc": {"count": 1}},
                upsert=True,
            )
            return await self.get_usage(user_id, date_str)
        self._sql(
            "INSERT INTO usage (user_id, date, count) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, date) DO UPDATE SET count = count + 1",
            (user_id, date_str),
        )
        return await self.get_usage(user_id, date_str)

    # ── Referrals ────────────────────────────────────────────

    async def add_referral(self, referrer_id: int, referred_id: int) -> bool:
        if referrer_id == referred_id:
            return False
        ts = datetime.now(timezone.utc).isoformat()
        if self._use_mongo:
            existing = await self._mongo.referrals.find_one(
                {"referrer_id": referrer_id, "referred_id": referred_id})
            if existing:
                return False
            await self._mongo.referrals.insert_one(
                {"referrer_id": referrer_id, "referred_id": referred_id,
                 "created_at": ts})
            return True
        existing = self._sql(
            "SELECT 1 FROM referrals WHERE referrer_id=? AND referred_id=?",
            (referrer_id, referred_id), fetch=True)
        if existing:
            return False
        self._sql("INSERT INTO referrals VALUES (?,?,?)",
                  (referrer_id, referred_id, ts))
        return True

    async def count_referrals(self, user_id: int) -> int:
        if self._use_mongo:
            return await self._mongo.referrals.count_documents(
                {"referrer_id": user_id})
        rows = self._sql("SELECT COUNT(*) as cnt FROM referrals WHERE referrer_id=?",
                         (user_id,), fetch=True)
        return int(rows[0]["cnt"]) if rows else 0

    async def referral_milestone_claimed(self, user_id: int, milestone: int) -> bool:
        if self._use_mongo:
            doc = await self._mongo.referral_milestones.find_one(
                {"user_id": user_id, "milestone": milestone})
            return doc is not None
        rows = self._sql(
            "SELECT 1 FROM referral_milestones WHERE user_id=? AND milestone=?",
            (user_id, milestone), fetch=True)
        return bool(rows)

    async def claim_referral_milestone(self, user_id: int, milestone: int):
        ts = datetime.now(timezone.utc).isoformat()
        if self._use_mongo:
            await self._mongo.referral_milestones.update_one(
                {"user_id": user_id, "milestone": milestone},
                {"$set": {"claimed_at": ts}}, upsert=True)
        else:
            self._sql("INSERT OR REPLACE INTO referral_milestones VALUES (?,?,?)",
                      (user_id, milestone, ts))

    async def referral_stats(self, user_id: int) -> dict:
        count = await self.count_referrals(user_id)
        claimed = {}
        for m in (7, 15, 25):
            claimed[m] = await self.referral_milestone_claimed(user_id, m)
        return {"count": count, "claimed": claimed}

    # ── Coupons ─────────────────────────────────────────────

    async def create_coupon(self, code: str, plan_id: str, max_uses: int = 1,
                            created_by: int = 0) -> bool:
        code = code.strip().upper()
        if not code:
            return False
        ts = datetime.now(timezone.utc).isoformat()
        if self._use_mongo:
            existing = await self._mongo.coupons.find_one({"code": code})
            if existing:
                return False
            await self._mongo.coupons.insert_one(
                {"code": code, "plan_id": plan_id, "max_uses": max_uses,
                 "used_count": 0, "created_at": ts, "created_by": created_by})
            return True
        existing = self._sql("SELECT 1 FROM coupons WHERE code=?", (code,), fetch=True)
        if existing:
            return False
        self._sql("INSERT INTO coupons VALUES (?,?,?,?,?,?)",
                  (code, plan_id, max_uses, 0, ts, created_by))
        return True

    async def get_coupon(self, code: str) -> Optional[dict]:
        code = code.strip().upper()
        if self._use_mongo:
            return await self._mongo.coupons.find_one({"code": code})
        rows = self._sql("SELECT * FROM coupons WHERE code=?", (code,), fetch=True)
        return dict(rows[0]) if rows else None

    async def use_coupon(self, code: str) -> Optional[dict]:
        """Atomically mark coupon as used, return coupon info if valid."""
        coupon = await self.get_coupon(code)
        if not coupon:
            return None
        if int(coupon.get("used_count", 0)) >= int(coupon.get("max_uses", 1)):
            return None
        if self._use_mongo:
            result = await self._mongo.coupons.find_one_and_update(
                {"code": coupon["code"],
                 "used_count": {"$lt": int(coupon.get("max_uses", 1))}},
                {"$inc": {"used_count": 1}},
                return_document=True,
            )
            return dict(result) if result else None
        else:
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
            if updated == 0:
                return None
        return coupon

    async def all_coupons(self) -> list:
        if self._use_mongo:
            return await self._mongo.coupons.find({}).to_list(None)
        rows = self._sql("SELECT * FROM coupons ORDER BY created_at DESC", fetch=True)
        return [dict(r) for r in rows] if rows else []

    async def delete_coupon(self, code: str) -> bool:
        code = code.strip().upper()
        if self._use_mongo:
            result = await self._mongo.coupons.delete_one({"code": code})
            return bool(result.deleted_count)
        rows = self._sql("SELECT 1 FROM coupons WHERE code=?", (code,), fetch=True)
        if not rows:
            return False
        self._sql("DELETE FROM coupons WHERE code=?", (code,))
        return True

    # ── Payment Requests ────────────────────────────────────

    async def create_payment_request(self, user_id: int, plan_id: str) -> str:
        request_id = uuid.uuid4().hex[:12]
        ts = datetime.now(timezone.utc).isoformat()
        doc = {"request_id": request_id, "user_id": user_id, "plan_id": plan_id,
               "status": "pending", "created_at": ts,
               "reviewed_at": None, "reviewed_by": None}
        if self._use_mongo:
            await self._mongo.payment_requests.insert_one(doc)
        else:
            self._sql(
                "INSERT INTO payment_requests (request_id, user_id, plan_id, "
                "status, created_at, reviewed_at, reviewed_by) "
                "VALUES (?,?,?,?,?,?,?)",
                (request_id, user_id, plan_id, "pending", ts, None, 0))
        return request_id

    async def get_payment_request(self, request_id: str) -> Optional[dict]:
        if self._use_mongo:
            return await self._mongo.payment_requests.find_one({"request_id": request_id})
        rows = self._sql("SELECT * FROM payment_requests WHERE request_id=?",
                         (request_id,), fetch=True)
        return dict(rows[0]) if rows else None

    async def pending_payment_requests(self) -> list:
        if self._use_mongo:
            return await self._mongo.payment_requests.find({"status": "pending"}).to_list(None)
        rows = self._sql("SELECT * FROM payment_requests WHERE status='pending' ORDER BY created_at DESC",
                         fetch=True)
        return [dict(r) for r in rows] if rows else []

    async def update_payment_request(self, request_id: str, status: str,
                                     reviewed_by: int = 0):
        ts = datetime.now(timezone.utc).isoformat()
        if self._use_mongo:
            await self._mongo.payment_requests.update_one(
                {"request_id": request_id},
                {"$set": {"status": status, "reviewed_at": ts, "reviewed_by": reviewed_by}})
        else:
            self._sql(
                "UPDATE payment_requests SET status=?, reviewed_at=?, reviewed_by=? WHERE request_id=?",
                (status, ts, reviewed_by, request_id))

db = Database()
