"""SYNC PLAY — ek recording, saari IDs, ek saath (0 offset).

Flow (private group me: aapki main ID + spare IDs + ye bot):
  1. `.synctarget <chat_id | @username | invite link>`  -> target GC save
  2. recording ko reply karke `.syncplay`  (ya `.syncplay <tag>`)
     -> main ID + har spare ID target VC me EXACT same processed audio
        bajati hai, ek hi pal me.
  3. `.syncstop` -> sab IDs ka audio band (VC me rehte hain)
     `.syncleave` -> sab IDs VC chhod dein
  4. `.syncmain off` -> main ID ko shamil na karein (phone se VC sun rahe ho
     to zaroori — Telegram ek ID ko ek hi jagah VC me rehne deta hai).

ZERO DIFFERENCE kaise:
  * audio sirf EK baar download + process hota hai -> har ID ke paas
    byte-for-byte same WAV (same awaaz, same loudness).
  * pehle saari IDs ko 1 s silence ke saath VC me join karaya jaata hai
    (join slow hota hai aur har ID ka alag time leta hai).
  * phir sab ready hone par ek asyncio barrier khulta hai aur har ID ka
    stream-switch usi event-loop tick me fire hota hai (switch local hai,
    network join nahi) -> IDs ke beech ~0 ms ka antar.
"""

import asyncio
import logging
import os
import re
import tempfile
import time

from pyrogram import Client, filters
from pyrogram.types import Message

from helpers.database import db
from helpers.vc_manager import session_manager
from plugins.ui import HAS_USER, cmd_prefix, cmd_text

logger = logging.getLogger("vcbot.sync_play")

# uid -> {"cid": int, "engines": [UserVC], "wav": str, "started": float}
_ACTIVE: dict = {}
_LOCKS: dict = {}
_SILENCE = None


def sync_engines(uid) -> list:
    ent = _ACTIVE.get(uid) if uid is not None else None
    return list(ent["engines"]) if ent else []


def _lock(uid):
    if uid not in _LOCKS:
        _LOCKS[uid] = asyncio.Lock()
    return _LOCKS[uid]


async def _silence_wav() -> str:
    global _SILENCE
    if _SILENCE and os.path.exists(_SILENCE):
        return _SILENCE
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="sync_silence_")
    os.close(fd)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "30",
        "-c:a", "pcm_s16le", path,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    _SILENCE = path
    return path


async def collect_engines(uid: int, include_main: bool = True) -> list:
    """Main ID + spare slot 1..N — unique Telegram accounts only."""
    out, seen = [], set()
    if include_main:
        main = await session_manager.get(uid)
        if main is not None:
            out.append(main)
            seen.add(getattr(main, "account_id", 0) or id(main))
    for slot in range(1, getattr(session_manager, "MAX_SPARES", 5) + 1):
        try:
            if not await session_manager.assistant_string(uid, slot):
                continue
            relay = await session_manager.get_relay(uid, slot)
        except Exception as exc:
            logger.debug("sync: spare %s unavailable: %r", slot, exc)
            continue
        aid = getattr(relay, "account_id", 0) if relay else 0
        if relay is None or (aid and aid in seen):
            continue
        seen.add(aid or id(relay))
        out.append(relay)
    return out


async def _target(uid: int):
    cid = await db.get_app_value(f"sync_gc_{uid}")
    ref = await db.get_app_value(f"sync_gc_ref_{uid}")
    if not cid:
        cid = await db.get_app_value(f"mic_chat_{uid}")
        ref = await db.get_app_value(f"mic_chat_ref_{uid}")
    try:
        return (int(cid), ref or None) if cid else (None, None)
    except (TypeError, ValueError):
        return None, None


async def _include_main(uid: int) -> bool:
    return (await db.get_app_value(f"sync_main_{uid}") or "1") != "0"


def _stream(path):
    from pytgcalls.types import AudioQuality, MediaStream
    return MediaStream(path, AudioQuality.HIGH, video_flags=MediaStream.Flags.IGNORE)


async def _join_all(engines, cid, ref) -> list:
    """Phase 1 — har ID ko VC me silence ke saath join karao (parallel)."""
    from plugins.vc_bridge import ensure_in_target
    from pytgcalls.exceptions import NoActiveGroupCall
    from pytgcalls.types import GroupCallConfig

    silence = await _silence_wav()

    async def _one(eng):
        try:
            await ensure_in_target(getattr(eng, "client", None), cid, ref)
            await eng._peer(cid, join_ref=ref)
            try:
                await eng.calls.play(cid, _stream(silence), GroupCallConfig(auto_start=False))
            except NoActiveGroupCall:
                if not await eng.start_voice_chat(cid):
                    raise
                await eng.calls.play(cid, _stream(silence), GroupCallConfig(auto_start=False))
            return eng, None
        except Exception as exc:
            return eng, exc

    return await asyncio.gather(*(_one(e) for e in engines))


async def _fire_all(engines, cid, wav, name) -> list:
    """Phase 2 — barrier: sab IDs ka stream-switch ek hi tick me."""
    from pytgcalls.types import GroupCallConfig
    gate = asyncio.Event()
    streams = {id(e): _stream(wav) for e in engines}   # pre-built, no work after gate

    async def _one(eng):
        await gate.wait()
        try:
            await eng.calls.play(cid, streams[id(eng)], GroupCallConfig(auto_start=False))
            st = eng.state(cid)
            st.is_playing, st.is_paused = True, False
            st.source_name = name
            st.processed_file = wav
            st.loop = False
            return eng, None
        except Exception as exc:
            return eng, exc

    tasks = [asyncio.create_task(_one(e)) for e in engines]
    await asyncio.sleep(0)          # every task is now parked on the gate
    gate.set()
    return await asyncio.gather(*tasks)


async def _process_once(uid: int, path: str) -> str:
    from helpers.audio_processor import process_audio_to_file
    s = await db.get_settings(uid) or {}
    return await process_audio_to_file(
        path,
        volume=s.get("volume"), bass=s.get("bass"), echo=s.get("echo"),
        echo_level=s.get("echo_level"), boost=s.get("boost"),
        relay_volume=s.get("relay_volume"), gain=s.get("gain"),
        treble=s.get("treble"))


async def _boost_all(engines, cid) -> None:
    """LOUD JUGAD: har ID baaki sab IDs (aur khud) ko 200% volume deti hai.
    Jo ID group admin hai uska 200% sab listeners ke liye lagta hai, isliye
    ek bhi admin ID ho to poori VC me sync awaaz +6 dB tez sunai deti hai.
    Telegram naye participant ko 100% pe reset karta hai -> retry."""
    ids = [getattr(e, "account_id", None) for e in engines]
    ids = [i for i in ids if i]
    for delay in (0.5, 2.0, 5.0):
        await asyncio.sleep(delay)
        for eng in engines:
            for aid in ids:
                try:
                    await eng.set_participant_volume(cid, aid, 20000, quiet=True)
                except Exception:
                    pass


def _name(eng) -> str:
    return str(getattr(eng, "account_name", None) or getattr(eng, "label", "ID"))


# ---------------------------------------------------------------- commands
@Client.on_message(HAS_USER & cmd_prefix(r"(synctarget|synctgt|syncgc)\b", flags=re.IGNORECASE)
                   & (filters.group | filters.private))
async def cmd_synctarget(bot: Client, msg: Message):
    uid = msg.from_user.id
    parts = cmd_text(msg).split()
    if len(parts) < 2:
        cid, _ = await _target(uid)
        await msg.reply_text(
            "🎯 <b>Sync target GC</b>: " + (f"<code>{cid}</code>" if cid else "set nahi hai")
            + "\n\nSet: <code>.synctarget -100xxxx</code> / <code>@username</code> / invite link")
        return
    from plugins.vc_bridge import resolve_group
    cid, jref = await resolve_group(msg, parts[1], uid, "tgt")
    if not cid:
        await msg.reply_text("❌ Group nahi mila. Chat ID / @username / invite link check karein.")
        return
    await db.set_app_value(f"sync_gc_{uid}", str(cid))
    await db.set_app_value(f"sync_gc_ref_{uid}", jref or "")
    engines = await collect_engines(uid, await _include_main(uid))
    await msg.reply_text(
        f"✅ <b>Sync target set:</b> <code>{cid}</code>\n"
        f"👥 IDs ready: <b>{len(engines)}</b>\n\n"
        "Ab kisi bhi recording ko reply karke <code>.syncplay</code> bhejo — "
        "saari IDs ek saath, same awaaz me bajayengi.")


@Client.on_message(HAS_USER & cmd_prefix(r"(syncplay|allplay|splay)\b", flags=re.IGNORECASE)
                   & (filters.group | filters.private))
async def cmd_syncplay(bot: Client, msg: Message):
    uid = msg.from_user.id
    from plugins.vc_commands import _check_usage_access, resolve_source, _cleanup_source
    if not await _check_usage_access(msg):
        return
    cid, ref = await _target(uid)
    if not cid:
        await msg.reply_text("❌ Pehle target set karo: <code>.synctarget -100xxxx</code>")
        return
    lk = _lock(uid)
    if lk.locked():
        await msg.reply_text("⏳ Pichla sync abhi chal raha hai…")
        return
    async with lk:
        parts = cmd_text(msg).split()
        arg = " ".join(parts[1:]) or None
        path, name, _ = await resolve_source(bot, msg, arg)
        if not path or not os.path.exists(path):
            return
        stat = await msg.reply_text("🔄 Sync: audio ek baar process ho raha hai…")
        try:
            wav = await _process_once(uid, path)
        except Exception as exc:
            _cleanup_source(path)
            await stat.edit_text(f"❌ Process fail: <code>{str(exc)[:300]}</code>")
            return
        engines = await collect_engines(uid, await _include_main(uid))
        if not engines:
            await stat.edit_text("❌ Koi logged-in ID nahi mili. Pehle /login + spare IDs add karo.")
            return
        if not await db.get_app_value(f"sync_auto_gc_{uid}"):
            async def _bg_group():
                try:
                    gcid, inv, j, f = await auto_sync_group(bot, uid)
                    await msg.reply_text(
                        f"🎁 <b>{SYNC_GROUP_TITLE}</b> auto ban gaya ({len(j)} IDs andar).\n"
                        f"🔗 Join link: {inv}\n<i>Aage se commands yahin chalao.</i>",
                        disable_web_page_preview=True)
                except Exception as exc:
                    logger.debug("auto sync group failed: %r", exc)
            asyncio.create_task(_bg_group())
        await stat.edit_text(f"🔗 Sync: {len(engines)} IDs VC join kar rahi hain…")
        joined = await _join_all(engines, cid, ref)
        ready = [e for e, err in joined if err is None]
        failed = [(e, err) for e, err in joined if err is not None]
        if not ready:
            await stat.edit_text("❌ Koi ID VC join nahi kar payi:\n<code>"
                                 + str(failed[0][1])[:300] + "</code>")
            return
        # let every WebRTC connection settle before the barrier opens
        await asyncio.sleep(1.2)
        t0 = time.perf_counter()
        fired = await _fire_all(ready, cid, wav, name or "Sync audio")
        spread_ms = (time.perf_counter() - t0) * 1000
        ok = [e for e, err in fired if err is None]
        failed += [(e, err) for e, err in fired if err is not None]
        _ACTIVE[uid] = {"cid": cid, "engines": ok, "wav": wav, "started": time.time()}
        await _ghost_apply(uid, cid)
        asyncio.create_task(_boost_all(ok, cid))
        _cleanup_source(path)
        lines = [f"✅ <b>SYNC PLAY</b> — {len(ok)} IDs ek saath",
                 f"🎯 GC: <code>{cid}</code>",
                 f"🎵 {name}",
                 f"⏱ Fire spread: <code>{spread_ms:.1f} ms</code>", ""]
        lines += [f"🟢 {_name(e)}" for e in ok]
        lines += [f"🔴 {_name(e)} — <code>{str(err)[:80]}</code>" for e, err in failed]
        lines.append("\nBand: <code>.syncstop</code>  |  VC chhodo: <code>.syncleave</code>")
        await stat.edit_text("\n".join(lines))


@Client.on_message(HAS_USER & cmd_prefix(r"(syncstop)\b", flags=re.IGNORECASE)
                   & (filters.group | filters.private))
async def cmd_syncstop(bot: Client, msg: Message):
    uid = msg.from_user.id
    leave = cmd_text(msg).split()[0].lstrip("./!").lower() == "syncleave"
    ent = _ACTIVE.pop(uid, None)
    cid = ent["cid"] if ent else (await _target(uid))[0]
    engines = ent["engines"] if ent else await collect_engines(uid, await _include_main(uid))
    if not cid or not engines:
        await msg.reply_text("⚠️ Koi sync play active nahi hai.")
        return

    async def _one(eng):
        try:
            if leave:
                await eng.leave(cid, reason="Sync leave")
            else:
                await eng.calls.play(cid, _stream(await _silence_wav()))
                st = eng.state(cid)
                st.is_playing = False
        except Exception as exc:
            logger.debug("sync stop failed: %r", exc)

    await asyncio.gather(*(_one(e) for e in engines))
    await msg.reply_text(("👋 Sab IDs VC se nikal gayi." if leave
                          else "⏹ Sync audio band (IDs VC me hi hain)."))


@Client.on_message(HAS_USER & cmd_prefix(r"(syncmain|syncids)\b", flags=re.IGNORECASE)
                   & (filters.group | filters.private))
async def cmd_syncmain(bot: Client, msg: Message):
    uid = msg.from_user.id
    parts = cmd_text(msg).split()
    if parts[0].lstrip("./!").lower() == "syncmain" and len(parts) > 1:
        on = parts[1].lower() in ("on", "1", "yes", "haan")
        await db.set_app_value(f"sync_main_{uid}", "1" if on else "0")
    inc = await _include_main(uid)
    engines = await collect_engines(uid, inc)
    cid, _ = await _target(uid)
    await msg.reply_text(
        "👥 <b>Sync IDs</b>\n"
        + "\n".join(f"{i}. {_name(e)}" for i, e in enumerate(engines, 1)) +
        f"\n\nMain ID shamil: <b>{'ON' if inc else 'OFF'}</b> (<code>.syncmain on|off</code>)\n"
        f"Target: <code>{cid or 'set nahi'}</code>\n"
        "Spare IDs add: <code>.spare</code>")


SYNC_GROUP_TITLE = "Apex Multiple ID Aura🥵🎤"


async def auto_sync_group(bot, uid: int):
    """Private control group: main ID banata hai, saari spare IDs + bot ko
    andar laata hai, aur user ko direct join link deta hai.
    Returns (chat_id, invite_link, joined_names, failed_names)."""
    engines = await collect_engines(uid, True)
    if not engines:
        raise RuntimeError("Koi logged-in ID nahi mili. Pehle /login karo.")
    owner = engines[0]
    oc = getattr(owner, "client", None)
    if oc is None:
        raise RuntimeError("Main ID connected nahi hai.")
    cid = None
    saved = await db.get_app_value(f"sync_auto_gc_{uid}")
    if saved:
        try:
            await oc.get_chat(int(saved))
            cid = int(saved)
        except Exception:
            cid = None
    if cid is None:
        chat = await oc.create_supergroup(SYNC_GROUP_TITLE, "Apex multi-ID sync control room")
        cid = chat.id
        await db.set_app_value(f"sync_auto_gc_{uid}", str(cid))
    try:
        await oc.set_chat_title(cid, SYNC_GROUP_TITLE)
    except Exception:
        pass
    try:
        invite = (await oc.create_chat_invite_link(cid, creates_join_request=False)).invite_link
    except Exception:
        invite = await oc.export_chat_invite_link(cid)

    joined, failed = [_name(owner)], []

    async def _join(eng):
        c = getattr(eng, "client", None)
        if c is None:
            return eng, False
        try:
            await c.get_chat_member(cid, "me")
            return eng, True
        except Exception:
            pass
        try:
            await c.join_chat(invite)
            return eng, True
        except Exception as exc:
            if "USER_ALREADY_PARTICIPANT" in str(exc):
                return eng, True
            logger.debug("sync group join failed: %r", exc)
            return eng, False

    for eng, ok in await asyncio.gather(*(_join(e) for e in engines[1:])):
        (joined if ok else failed).append(_name(eng))

    # Bot ko bhi add karo (commands isi group me chalenge) + admin.
    try:
        me = bot.me or await bot.get_me()
        try:
            await oc.add_chat_members(cid, me.username or me.id)
        except Exception as exc:
            if "USER_ALREADY_PARTICIPANT" not in str(exc):
                logger.debug("sync group bot add failed: %r", exc)
        try:
            from pyrogram.types import ChatPrivileges
            await oc.promote_chat_member(cid, me.id, ChatPrivileges(
                can_manage_chat=True, can_delete_messages=True,
                can_manage_video_chats=True, can_invite_users=True))
        except Exception:
            pass
    except Exception:
        pass
    return cid, invite, joined, failed


@Client.on_message(HAS_USER & cmd_prefix(r"(syncgroup|allgroup|autogroup|playallgroup)\b", flags=re.IGNORECASE)
                   & (filters.group | filters.private))
async def cmd_syncgroup(bot: Client, msg: Message):
    uid = msg.from_user.id
    stat = await msg.reply_text("🔄 <b>Apex Multiple ID Aura</b> group ban raha hai, saari IDs add ho rahi hain…")
    try:
        cid, invite, joined, failed = await auto_sync_group(bot, uid)
    except Exception as exc:
        await stat.edit_text(f"❌ Auto group nahi bana:\n<code>{str(exc)[:300]}</code>\n\n"
                             "Khud group banake IDs add karo, phir wahan <code>.syncplay</code> chalao.")
        return
    lines = [f"✅ <b>{SYNC_GROUP_TITLE}</b> ready!", "",
             f"🆔 Group: <code>{cid}</code>",
             f"🔗 <b>Join link:</b> {invite}", "",
             f"👥 Andar IDs ({len(joined)}): " + ", ".join(joined)]
    if failed:
        lines.append(f"⚠️ Add nahi hui ({len(failed)}): " + ", ".join(failed)
                     + "\n   → ye link un IDs se khud open karke join kar lo.")
    lines += ["", "👉 Link se aap bhi join karo (aur koi extra ID ho to wo bhi).",
              "Phir: <code>.synctarget -100xxxx</code> → recording reply karke <code>.syncplay</code>"]
    await stat.edit_text("\n".join(lines), disable_web_page_preview=True)


SYNC_HELP = (
    "🔗 <b>SYNC PLAY</b> — saari IDs, ek awaaz, 0 delay\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "1️⃣ <code>.syncgroup</code> — bot khud group banayega + saari IDs add + link dega\n"
    "2️⃣ <code>.synctarget -100xxxx</code> — fight GC set karo\n"
    "3️⃣ Recording reply karke <code>.syncplay</code>\n"
    "   → har ID same pal me same audio bajayegi\n"
    "4️⃣ <code>.syncstop</code> band  |  <code>.syncleave</code> VC chhodo\n"
    "   <code>.syncleave 2</code> sirf ID #2 nikle  |  <code>.synconly 1</code> sirf #1 rahe\n"
    "👻 <code>.syncghost on</code> — sab IDs ka mic OFF dikhe, awaaz chale\n\n"
    "🎤 Live mic bhi sab spare IDs se same awaaz me jaata hai: <code>.mic on</code>\n"
    "👤 Phone se main ID se VC sun rahe ho to: <code>.syncmain off</code>\n"
    "➕ Spare IDs: <code>.spare</code> (max 5)"
)


@Client.on_callback_query(filters.regex(r"^sync:menu$"))
async def cb_sync_menu(bot: Client, cq):
    from plugins.ui import B, edit_screen, safe_answer
    from pyrogram.types import InlineKeyboardMarkup as K
    uid = cq.from_user.id
    cid, _ = await _target(uid)
    engines = await collect_engines(uid, await _include_main(uid))
    text = (SYNC_HELP + f"\n\n🎯 Target: <code>{cid or 'set nahi'}</code>\n"
            f"👥 IDs ready: <b>{len(engines)}</b>")
    kb = K([[B("➕ Spare IDs", callback_data="mic:panel", style="primary"),
             B("🏠 Home", callback_data="menu:home", style="primary")]])
    await safe_answer(cq)
    await edit_screen(cq.message, text, reply_markup=kb)


async def _ghost_apply(uid: int, cid: int) -> None:
    """Ghost ON ho to har sync ID ka mic icon OFF (muted) dikhe, awaaz chalti rahe."""
    try:
        from plugins.ghost_mic import is_ghost_chat, _safe_mute, _engine
        if is_ghost_chat(cid):
            await _safe_mute(_engine(uid), cid, True, uid)
    except Exception as exc:
        logger.debug("sync ghost apply failed: %r", exc)


async def _list_for(uid: int):
    ent = _ACTIVE.get(uid)
    if ent:
        return ent["cid"], ent["engines"]
    return (await _target(uid))[0], await collect_engines(uid, await _include_main(uid))


def _pick(engines, nums):
    out = []
    for n in nums:
        try:
            i = int(n) - 1
        except ValueError:
            continue
        if 0 <= i < len(engines):
            out.append(engines[i])
    return out


@Client.on_message(HAS_USER & cmd_prefix(r"(syncleave|synconly|synckick)\b", flags=re.IGNORECASE)
                   & (filters.group | filters.private))
async def cmd_syncleave(bot: Client, msg: Message):
    """.syncleave        -> sab IDs VC chhodein
       .syncleave 2 3    -> sirf ID #2, #3 nikle (baaki bajti rahein)
       .synconly 1       -> sirf ID #1 rahe, baaki sab nikal jayein"""
    uid = msg.from_user.id
    parts = cmd_text(msg).split()
    name = parts[0].lstrip("./!").lower()
    nums = parts[1:]
    cid, engines = await _list_for(uid)
    if not cid or not engines:
        await msg.reply_text("⚠️ Koi sync ID active nahi hai.")
        return
    if name == "synconly":
        keep = _pick(engines, nums)
        if not keep:
            await msg.reply_text("Usage: <code>.synconly 1</code>  (number <code>.syncids</code> me dekho)")
            return
        go = [e for e in engines if e not in keep]
    else:
        go = _pick(engines, nums) if nums else list(engines)
        keep = [e for e in engines if e not in go]
        if nums and not go:
            await msg.reply_text("❌ Galat number. <code>.syncids</code> me list dekho.")
            return

    async def _one(eng):
        try:
            await eng.leave(cid, reason="Sync leave")
        except Exception as exc:
            logger.debug("sync leave failed: %r", exc)

    await asyncio.gather(*(_one(e) for e in go))
    if keep:
        _ACTIVE[uid] = {**(_ACTIVE.get(uid) or {}), "cid": cid, "engines": keep}
    else:
        _ACTIVE.pop(uid, None)
    lines = ["👋 VC se nikli:"] + [f"• {_name(e)}" for e in go]
    if keep:
        lines += ["", "🟢 Abhi bhi VC me:"] + [f"{i}. {_name(e)}" for i, e in enumerate(keep, 1)]
    await msg.reply_text("\n".join(lines))


@Client.on_message(HAS_USER & cmd_prefix(r"syncghost\b", flags=re.IGNORECASE)
                   & (filters.group | filters.private))
async def cmd_syncghost(bot: Client, msg: Message):
    """.syncghost on|off — TARGET GC me saari sync IDs ka mic OFF dikhe, awaaz chalti rahe.
    (.ghostmic private group me chalane se private group pakad leta tha.)"""
    uid = msg.from_user.id
    parts = cmd_text(msg).split()
    on = len(parts) < 2 or parts[1].lower() in ("on", "1", "haan")
    from plugins.ghost_mic import _start, _stop, _engine
    uvc = _engine(uid)
    cid, engines = await _list_for(uid)
    if not uvc or not cid:
        await msg.reply_text("❌ Pehle login + <code>.synctarget</code> set karo.")
        return
    if on:
        await _start(uid, uvc, cid, "hold")
        await msg.reply_text(f"👻 Sync Ghost ON — {len(engines)} IDs ka mic OFF dikhega, "
                             "awaaz bajti rahegi.\nOff: <code>.syncghost off</code>")
    else:
        await _stop(uid, uvc, cid)
        await msg.reply_text("❌ Sync Ghost OFF — mic icon normal.")
