"""
Voice-chat commands. Every command runs on the CALLER's own logged-in
account, so many users can use the bot at the same time.

Prefix: . or /
"""

import os

from pyrogram import Client, filters
from plugins.ui import B, edit_screen
from pyrogram.types import InlineKeyboardMarkup as K
from pyrogram.types import Message

from config import Config
from helpers.audio_processor import (
    BASS_MAX, BASS_MIN, LEVEL_MAX, LEVEL_MIN, VOLUME_MAX, VOLUME_MIN, clamp,
)
from helpers.database import db
from helpers.logger_channel import (
    get_channel, log_command, log_error, set_channel, verify_log_channel,
)
from helpers.vc_manager import AUTO_PRESET, VOL_MAX, VOL_NORMAL, session_manager

LOGIN_KB = K([
    [B(" Login karein", callback_data="menu:login")],
    [B(" Tutorial", callback_data="menu:tutorial")],
])


def _cleanup_source(path: str):
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass


def now_playing_kb(cid: int) -> K:
    """Controls shown on every active playback message."""
    return K([
        [B(" Now Playing", callback_data=f"vc:now:{cid}")],
        [B("⏸ Pause", callback_data=f"vc:pause:{cid}"),
         B("▶ Resume", callback_data=f"vc:resume:{cid}"),
         B("⏭ Skip", callback_data=f"vc:skip:{cid}")],
        [B(" Loop", callback_data=f"vc:loop:{cid}")],
        [B(" Reset Audio", callback_data=f"vc:reset:{cid}"),
         B(" Auto", callback_data=f"vc:auto:{cid}")],
        [B("⏹ Stop", callback_data=f"vc:stop:{cid}"),
         B(" Settings", callback_data="menu:settings")],
    ])


async def get_engine(msg: Message):
    """Return the caller's VC engine, or None (with a helpful reply)."""
    uvc = await session_manager.get(msg.from_user.id)
    if not uvc:
        await msg.reply_text(
            " <b>Pehle login karein.</b>\n\n"
            "Bot ke DM mein jaakar  Login   Phone se Login, "
            "ya apna string session add karein.",
            reply_markup=LOGIN_KB,
        )
    return uvc


async def target_chat(msg: Message, arg: str = None) -> int:
    """Resolve which group's VC we are talking about."""
    if arg:
        try:
            return int(arg)
        except ValueError:
            try:
                chat = await msg._client.get_chat(arg)
                return chat.id
            except Exception:
                return 0
    if msg.chat and msg.chat.id < 0:
        return msg.chat.id
    return 0


async def need_chat(msg: Message, arg: str = None) -> int:
    cid = await target_chat(msg, arg)
    if not cid:
        await msg.reply_text(
            " Voice chat sirf <b>groups</b> mein hota hai.\n"
            "Group mein command chalayein, ya group ka chat ID dein:\n"
            "<code>.play &lt;source&gt; -1001234567890</code>"
        )
        return cid
    await db.register_broadcast_chat(cid)
    return cid


async def load_state_settings(user_id: int, uvc, chat_id: int):
    """Make sure a chat state starts from the user's saved settings."""
    s = await db.get_settings(user_id)
    st = uvc.state(chat_id)
    st.apply_settings(s)
    if bool(s.get("auto")) != bool(st.auto):
        await uvc.set_auto(chat_id, bool(s.get("auto")))
    return st


# ── Tags ─────────────────────────────────────────────────────────────────────
@Client.on_message(filters.regex(r"^[./]tag\b") & (filters.group | filters.private))
async def cmd_tag(bot: Client, msg: Message):
    parts = msg.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Usage: <code>.tag &lt;name&gt;</code> (audio ko reply karke)")
        return
    reply = msg.reply_to_message
    media = None
    if reply:
        media = (reply.audio or reply.voice or reply.video or reply.document
                 or reply.video_note)
    if not media:
        await msg.reply_text(" Kisi audio/video message ko reply karke <code>.tag</code> likhein.")
        return
    name = parts[1].strip().lower()
    ftype = "audio" if (reply.audio or reply.voice) else "video"
    await db.tag_file(msg.from_user.id, name, media.file_id, ftype, reply.caption or "")
    await msg.reply_text(f" Saved as <code>{name}</code> — ab <code>.play {name}</code>")


@Client.on_message(filters.regex(r"^[./]untag\b") & (filters.group | filters.private))
async def cmd_untag(bot: Client, msg: Message):
    parts = msg.text.strip().split(maxsplit=1)
    if len(parts) < 2:
        await msg.reply_text("Usage: <code>.untag &lt;name&gt;</code>")
        return
    name = parts[1].strip().lower()
    if not await db.get_tag(msg.from_user.id, name):
        await msg.reply_text(f" Tag <code>{name}</code> nahi mila.")
        return
    await db.delete_tag(msg.from_user.id, name)
    await msg.reply_text(f" <code>{name}</code> delete ho gaya.")


@Client.on_message(filters.regex(r"^[./]tags\b") & (filters.group | filters.private))
async def cmd_tags(bot: Client, msg: Message):
    tags = await db.list_tags(msg.from_user.id)
    if not tags:
        await msg.reply_text(" Koi tag nahi. <code>.tag &lt;name&gt;</code> se save karein.")
        return
    lines = [f"• <code>{t['tag_name']}</code> — {t['file_type']}" for t in tags]
    await msg.reply_text(" <b>Your Tags</b>\n" + "\n".join(lines))


# ── Source resolution ────────────────────────────────────────────────────────
async def resolve_source(bot: Client, msg: Message, arg: str):
    """Return (path, name) or (None, None) after replying with the error."""
    reply = msg.reply_to_message
    if reply:
        media = (reply.audio or reply.voice or reply.video or reply.document
                 or reply.video_note)
        if media:
            stat = await msg.reply_text("⬇ Media download ho raha hai…")
            try:
                path = await reply.download()
            except Exception as e:
                await stat.edit_text(f" Download fail: <code>{e}</code>")
                await log_error("resolve_source_reply", e)
                return None, None
            await stat.delete()
            return path, getattr(media, "file_name", None) or "Reply media"

    if arg:
        # Saved Telegram tag only. External URL/search downloads are disabled.
        tag = await db.get_tag(msg.from_user.id, arg.split()[0].lower())
        if tag:
            stat = await msg.reply_text("⬇ Tagged file download ho rahi hai…")
            try:
                path = await bot.download_media(tag["file_id"])
            except Exception as e:
                await stat.edit_text(f" Download fail: <code>{e}</code>")
                return None, None
            await stat.delete()
            return path, arg
        await msg.reply_text(
            " Sirf Telegram audio/video reply ya saved tag use karein. "
            "External links aur search playback supported nahi hai."
        )
        return None, None

    await msg.reply_text(
        "<b>Usage</b>\n"
        "• audio/video reply + <code>.play</code>\n"
        "• audio/video reply + <code>.play &lt;chat_id&gt;</code>\n"
        "• <code>.play &lt;tag&gt;</code>\n"
        "• <code>.play &lt;tag&gt; &lt;group_chat_id&gt;</code>"
    )
    return None, None


def _split_args(msg: Message):
    """Return (source_arg, chat_id_arg). Source may be multi-word (search)."""
    parts = msg.text.strip().split()
    words, cid = [], None
    for p in parts[1:]:
        try:
            if int(p) < 0:
                cid = p
                continue
        except ValueError:
            pass
        words.append(p)
    return (" ".join(words) or None), cid


# ── .play / .padd ────────────────────────────────────────────────────────────
async def _play(bot: Client, msg: Message, enqueue: bool):
    await log_command(msg.from_user.id, msg.from_user.username, msg.chat.id,
                      ".padd" if enqueue else ".play")
    uvc = await get_engine(msg)
    if not uvc:
        return
    source_arg, cid_arg = _split_args(msg)
    cid = await need_chat(msg, cid_arg)
    if not cid:
        return

    path, name = await resolve_source(bot, msg, source_arg)
    if not path or not os.path.exists(path):
        return

    try:
        chat = await bot.get_chat(cid)
        title = chat.title or str(cid)
    except Exception:
        title = str(cid)

    await db.register_broadcast_chat(cid, title)
    st = await load_state_settings(msg.from_user.id, uvc, cid)
    stat = await msg.reply_text(" Audio process ho raha hai…")
    try:
        status = await uvc.play(cid, path, name, title, enqueue=enqueue)
    except Exception as e:
        _cleanup_source(path)
        await stat.edit_text(f" Error: <code>{e}</code>")
        await log_error("cmd_play", e)
        return

    await stat.edit_text(
        f"{' <b>Queued!</b>' if status == 'queued' else '▶ <b>Playing!</b>'}\n\n"
        f" <b>Source:</b> {name}\n"
        f" <b>Chat:</b> {title}\n"
        f" <b>Account:</b> {uvc.account_name}\n"
        f" <b>Volume:</b> {st.relay_volume}/1000 |  <b>Bass:</b> +{st.bass} dB\n"
        f" <b>Boost:</b> {st.boost}/10 |  <b>Echo:</b> "
        f"{'On' if st.echo else 'Off'} {st.echo_level}/10",
        reply_markup=now_playing_kb(cid),
    )


@Client.on_message(filters.regex(r"^[./]play(?!force)\b") & (filters.group | filters.private))
async def cmd_play(bot: Client, msg: Message):
    await _play(bot, msg, enqueue=False)


# ── .playforce / .fplay ─────────────────────────────────────────────────────
@Client.on_message(filters.regex(r"^[./](playforce|fplay)\b") & (filters.group | filters.private))
async def cmd_playforce(bot: Client, msg: Message):
    """Queue clear + jo chal raha hai use hata kar turant yeh chalao."""
    await log_command(msg.from_user.id, msg.from_user.username, msg.chat.id,
                      ".playforce")
    uvc = await get_engine(msg)
    if not uvc:
        return
    source_arg, cid_arg = _split_args(msg)
    cid = await need_chat(msg, cid_arg)
    if not cid:
        return
    path, name = await resolve_source(bot, msg, source_arg)
    if not path or not os.path.exists(path):
        return
    try:
        chat = await bot.get_chat(cid)
        title = chat.title or str(cid)
    except Exception:
        title = str(cid)

    st = await load_state_settings(msg.from_user.id, uvc, cid)
    stat = await msg.reply_text(" <b>FORCE PLAY</b> — process ho raha hai…")
    try:
        await uvc.force_play(cid, path, name, title)
    except Exception as e:
        _cleanup_source(path)
        await stat.edit_text(f" Error: <code>{e}</code>")
        await log_error("cmd_playforce", e)
        return
    await stat.edit_text(
        f" <b>Force playing!</b>\n\n"
        f" <b>Source:</b> {name}\n"
        f" <b>Chat:</b> {title}\n"
        f" <b>Volume:</b> {st.relay_volume}/1000 |  <b>Bass:</b> +{st.bass} dB\n"
        f" <b>Boost:</b> {st.boost}/10 |  <b>Echo:</b> "
        f"{'On' if st.echo else 'Off'} {st.echo_level}/10",
        reply_markup=now_playing_kb(cid),
    )


# ── .loop ───────────────────────────────────────────────────────────────────
@Client.on_message(filters.regex(r"^[./]loop\b") & (filters.group | filters.private))
async def cmd_loop(bot: Client, msg: Message):
    """.loop | .loop off | .loop 5  — current track repeat."""
    await log_command(msg.from_user.id, msg.from_user.username, msg.chat.id, ".loop")
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = msg.text.strip().split()
    arg = parts[1].lower() if len(parts) > 1 else "on"
    cid_arg = None
    for p in parts[1:]:
        if p.startswith("-") and p[1:].isdigit():
            cid_arg = p
    cid = await need_chat(msg, cid_arg)
    if not cid:
        return
    st = uvc.chats.get(cid)
    if not st:
        await msg.reply_text(" Is chat me kuch chal nahi raha. Pehle <code>.play</code> karein.")
        return
    if arg in ("off", "0", "no", "band", "stop"):
        st.loop, st.loop_left = False, -1
        await msg.reply_text(" <b>Loop OFF</b>")
        return
    count = -1
    if arg.isdigit():
        count = max(1, int(arg))
    st.loop, st.loop_left = True, count
    await msg.reply_text(
        " <b>Loop ON</b> — " + ("infinite (jab tak <code>.loop off</code> na karein)"
                                  if count < 0 else f"{count} baar aur")
    )


@Client.on_message(filters.regex(r"^[./]padd\b") & (filters.group | filters.private))
async def cmd_padd(bot: Client, msg: Message):
    await _play(bot, msg, enqueue=True)


# ── transport controls ───────────────────────────────────────────────────────
async def _transport(msg: Message, action: str):
    """Run a transport action against the caller's selected VC."""
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = msg.text.strip().split()
    cid = await need_chat(msg, parts[1] if len(parts) > 1 else None)
    if not cid:
        return
    if action == "pause":
        ok = await uvc.pause(cid)
        await msg.reply_text("⏸ Paused." if ok else " Kuch play nahi ho raha.")
    elif action == "resume":
        ok = await uvc.resume(cid)
        await msg.reply_text("▶ Resumed." if ok else " Pause nahi tha.")
    elif action == "skip":
        ok = await uvc.skip(cid)
        await msg.reply_text("⏭ Skipped." if ok else " Active VC nahi.")
    elif action == "stop":
        if cid not in uvc.chats:
            await msg.reply_text(" Is VC mein bot ka active session nahi hai.")
            return
        try:
            await uvc.leave(cid, reason="Manual stop")
        except Exception as exc:
            await log_error("transport_stop", exc)
            await msg.reply_text(f" Stop fail hua: <code>{exc}</code>")
            return
        await msg.reply_text("⏹ <b>Stopped</b> — bot ne VC playback session chhod diya.")


@Client.on_message(filters.regex(r"^[./]pause\b") & (filters.group | filters.private))
async def cmd_pause(bot, msg):
    await _transport(msg, "pause")


@Client.on_message(filters.regex(r"^[./]resume\b") & (filters.group | filters.private))
async def cmd_resume(bot, msg):
    await _transport(msg, "resume")


@Client.on_message(filters.regex(r"^[./]skip\b") & (filters.group | filters.private))
async def cmd_skip(bot, msg):
    await _transport(msg, "skip")


@Client.on_message(filters.regex(r"^[./](stop|end|leave)\b") & (filters.group | filters.private))
async def cmd_stop(bot, msg):
    await _transport(msg, "stop")


@Client.on_message(filters.regex(r"^[./]queue\b") & (filters.group | filters.private))
async def cmd_queue(bot: Client, msg: Message):
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = msg.text.strip().split()
    cid = await need_chat(msg, parts[1] if len(parts) > 1 else None)
    if not cid:
        return
    st = uvc.chats.get(cid)
    if not st:
        await msg.reply_text(" Is chat mein koi active VC session nahi.")
        return
    lines = [f"{i+1}. {n}" for i, (_, n) in enumerate(st.queue)] or ["— empty —"]
    await msg.reply_text(
        f" <b>Now:</b> {st.source_name}\n <b>Queue:</b>\n" + "\n".join(lines))


@Client.on_message(filters.regex(r"^[./]vcinfo\b") & (filters.group | filters.private))
async def cmd_vcinfo(bot: Client, msg: Message):
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = msg.text.strip().split()
    cid = await need_chat(msg, parts[1] if len(parts) > 1 else None)
    if not cid:
        return
    st = uvc.chats.get(cid)
    if not st:
        await msg.reply_text(" Koi active VC session nahi.")
        return
    state = "⏸ Paused" if st.is_paused else "▶ Playing"
    await msg.reply_text(
        f" <b>VC Info</b> — <code>{cid}</code>\n\n"
        f"├ <b>Account:</b> {uvc.account_name} (<code>{uvc.account_id}</code>)\n"
        f"├ <b>Status:</b> {state}\n"
        f"├ <b>Now:</b> {st.source_name}\n"
        f"├ <b>Volume:</b> {st.volume}x\n"
        f"├ <b>Bass:</b> +{st.bass} dB\n"
        f"├ <b>Boost:</b> {st.boost}/10\n"
        f"├ <b>Echo:</b> {'On' if st.echo else 'Off'} ({st.echo_level}/10)\n"
        f"└ <b>Queue:</b> {len(st.queue)}"
    )


# ── effects ──────────────────────────────────────────────────────────────────
async def _apply_and_reply(msg: Message, label: str, **changes):
    from plugins.start import apply_settings_live
    uid = msg.from_user.id
    await db.save_settings(uid, **changes)
    s = await db.get_settings(uid)
    applied = await apply_settings_live(uid)
    await msg.reply_text(
        f"{label}\n\n"
        f" Vol <code>{s['volume']}/1000</code> |  Gain <code>{s['gain']}/150</code> | "
        f" Bass <code>{s['bass']}</code> |  Boost <code>{s['boost']}/10</code> |  Echo "
        f"<code>{'On' if s['echo'] else 'Off'} {s['echo_level']}/10</code>\n"
        + (f" {applied} live VC par apply hua." if applied else
           " Saved — agli play par lagega."),
        reply_markup=K([[B(" Settings Panel", callback_data="menu:settings")]]),
    )


def _num_arg(msg: Message):
    parts = msg.text.strip().split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


@Client.on_message(filters.regex(r"^[./]vol\b") & (filters.group | filters.private))
async def cmd_vol(bot, msg: Message):
    n = _num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>.vol &lt;0-1000&gt;</code>")
        return
    n = clamp(n, VOLUME_MIN, VOLUME_MAX)
    await _apply_and_reply(msg, f" Volume set: <b>{n}/1000</b>", volume=n, relay_volume=n)


@Client.on_message(filters.regex(r"^[./]bass\b") & (filters.group | filters.private))
async def cmd_bass(bot, msg: Message):
    n = _num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>/bass &lt;0-100&gt;</code>")
        return
    await _apply_and_reply(msg, f" Bass set: <b>+{clamp(n, BASS_MIN, BASS_MAX)} dB</b>",
                           bass=clamp(n, BASS_MIN, BASS_MAX))


@Client.on_message(filters.regex(r"^[./]boost\b") & (filters.group | filters.private))
async def cmd_boost(bot, msg: Message):
    n = _num_arg(msg)
    if n is None:
        await msg.reply_text(
            "Usage: <code>.boost &lt;0-10&gt;</code> (audio loudness)\n"
            "Live mic ke liye: <code>.myboost</code>")
        return
    await _apply_and_reply(msg, f" Boost set: <b>{clamp(n, LEVEL_MIN, LEVEL_MAX)}/10</b>",
                           boost=clamp(n, LEVEL_MIN, LEVEL_MAX))


@Client.on_message(filters.regex(r"^[./]echolvl\b") & (filters.group | filters.private))
async def cmd_echolvl(bot, msg: Message):
    n = _num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>.echolvl &lt;0-10&gt;</code>")
        return
    lvl = clamp(n, LEVEL_MIN, LEVEL_MAX)
    await _apply_and_reply(msg, f" Echo level: <b>{lvl}/10</b>",
                           echo_level=lvl, echo=1 if lvl else 0)


@Client.on_message(filters.regex(r"^[./]echo\b") & (filters.group | filters.private))
async def cmd_echo(bot, msg: Message):
    parts = msg.text.strip().split()
    if len(parts) < 2 or parts[1].lower() not in ("on", "off"):
        await msg.reply_text("Usage: <code>.echo on|off</code>")
        return
    on = parts[1].lower() == "on"
    await _apply_and_reply(msg, f" Echo: <b>{'ON' if on else 'OFF'}</b>",
                           echo=1 if on else 0)


@Client.on_message(filters.regex(r"^[./](max|ultra)\b") & (filters.group | filters.private))
async def cmd_max(bot, msg: Message):
    # Max mode stays voice-clear: excessive bass/echo makes a track feel
    # quieter even when the waveform is technically louder.
    await _apply_and_reply(msg, " <b>MAXIMUM LOUD MODE</b> — sab knobs max par.",
                           auto=1, **AUTO_PRESET)


@Client.on_message(filters.regex(r"^[./]reset\b") & (filters.group | filters.private))
async def cmd_reset(bot, msg: Message):
    await _apply_and_reply(msg, " <b>Defaults restored</b>",
                           volume=Config.DEFAULT_VOLUME,
                           relay_volume=Config.RELAY_DEFAULT_VOLUME,
                           gain=Config.RELAY_DEFAULT_GAIN,
                           bass=Config.DEFAULT_BASS,
                           treble=Config.RELAY_DEFAULT_TREBLE,
                           voice="normal", boost=Config.DEFAULT_BOOST,
                           echo=1 if Config.DEFAULT_ECHO else 0,
                           echo_level=Config.DEFAULT_ECHO_LEVEL, auto=0)


# ── live mic boost ───────────────────────────────────────────────────────────
@Client.on_message(filters.regex(r"^[./](myboost|livegain|livevolume)\b") & (filters.group | filters.private))
async def cmd_myboost(bot: Client, msg: Message):
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = msg.text.strip().split()
    vol = None
    cid_arg = None
    for p in parts[1:]:
        try:
            v = int(p)
        except ValueError:
            continue
        if v < 0:
            cid_arg = p
        else:
            vol = max(VOL_NORMAL, min(VOL_MAX, v))   # never below 100%
    cid = await need_chat(msg, cid_arg)
    if not cid:
        return
    st = uvc.state(cid)
    if vol is None:
        vol = st.live_volume
    st.live_volume = vol
    await db.save_settings(msg.from_user.id, live_volume=vol)
    ok = await uvc.set_participant_volume(cid, uvc.account_id, vol)
    await msg.reply_text(
        f" <b>Live mic boost {'lag gaya' if ok else 'fail'}</b>\n"
        f" <code>{uvc.account_id}</code> → {vol} ({round(vol/100)}%)\n"
        "Ab is session ke VC join/reconnect par bhi ye live gain re-apply hoga.\n\n"
        + ("Ab VC mein bolte hi aapki aavaj max loud jayegi."
           if ok else "VC on hai? Aapka account VC mein hai? Check karein.")
    )


# ── inline transport buttons ─────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^vc:"))
async def cb_vc(bot, cq):
    _, action, cid = cq.data.split(":")
    cid = int(cid)
    uvc = session_manager.users.get(cq.from_user.id)
    if not uvc:
        await cq.answer("Pehle login karein.", show_alert=True)
        return
    if action == "now":
        st = uvc.chats.get(cid)
        if not st or not st.is_playing:
            await cq.answer("Kuch play nahi ho raha", show_alert=True)
            return
        await edit_screen(cq.message,
            f" <b>NOW PLAYING</b>\n\n"
            f" <b>Track:</b> {st.source_name}\n"
            f" <b>Volume:</b> {st.relay_volume}/1000\n"
            f" <b>Gain:</b> {st.gain}/150\n"
            f" <b>Boost:</b> {st.boost}/10\n"
            f" <b>Bass:</b> {st.bass} |  <b>Treble:</b> {st.treble}\n"
            f" <b>Auto:</b> {'ON' if st.auto else 'OFF'}",
            reply_markup=now_playing_kb(cid),
        )
        await cq.answer("Now Playing updated")
    elif action == "reset":
        await cq.answer(" Reset apply ho raha hai…")
        from plugins.start import DEFAULT_SETTINGS, apply_settings_live
        await db.save_settings(cq.from_user.id, **DEFAULT_SETTINGS)
        await apply_settings_live(cq.from_user.id)
    elif action == "auto":
        await cq.answer(" Auto apply ho raha hai…")
        from plugins.start import apply_settings_live
        s = await db.get_settings(cq.from_user.id)
        on = not bool(s.get("auto"))
        await db.save_settings(cq.from_user.id, auto=1 if on else 0,
                               **(AUTO_PRESET if on else {}))
        await apply_settings_live(cq.from_user.id)
    elif action == "pause":
        await cq.answer("⏸ Paused" if await uvc.pause(cid) else "Kuch chal nahi raha")
    elif action == "resume":
        await cq.answer("▶ Resumed" if await uvc.resume(cid) else "Paused nahi tha")
    elif action == "skip":
        await uvc.skip(cid)
        await cq.answer("⏭ Skipped")
    elif action == "stop":
        await uvc.leave(cid, reason="Stopped from button")
        await cq.answer("⏹ Stopped")
    elif action == "loop":
        st = uvc.chats.get(cid)
        if not st:
            await cq.answer("Kuch chal nahi raha", show_alert=True)
        else:
            st.loop = not st.loop
            st.loop_left = -1
            await cq.answer(" Loop " + ("ON" if st.loop else "OFF"), show_alert=True)


# ── AUTO MODE ────────────────────────────────────────────────────────────────
AUTO_KB = K([[B(" Settings Panel", callback_data="menu:settings")]])


@Client.on_message(filters.regex(r"^[./]auto\b") & (filters.group | filters.private))
async def cmd_auto(bot: Client, msg: Message):
    """
    .auto        → auto mode ON (max loudness preset + volume keeper)
    .auto off    → auto mode OFF
    """
    from plugins.start import apply_settings_live
    await log_command(msg.from_user.id, msg.from_user.username, msg.chat.id, ".auto")
    parts = msg.text.strip().split()
    on = not any(p.lower() in ("off", "0", "no", "band") for p in parts[1:])

    uvc = await get_engine(msg)
    if not uvc:
        return

    # Save — every next .play picks it up automatically.
    await db.save_settings(msg.from_user.id, auto=1 if on else 0,
                           **(AUTO_PRESET if on else {}))
    applied = await apply_settings_live(msg.from_user.id)

    if not on:
        await msg.reply_text(" <b>AUTO MODE OFF</b> — manual control wapas.",
                             reply_markup=AUTO_KB)
        return

    await msg.reply_text(
        " <b>AUTO MODE ON</b> — ab sab automatic hai \n\n"
        f" Volume <code>{VOLUME_MAX}/1000</code> (max)\n"
        f" Bass <code>+{AUTO_PRESET['bass']}</code> (voice-safe max)\n"
        f" Treble <code>{AUTO_PRESET['treble']}/100</code> + Gain <code>150/150</code>\n"
        f" Boost <code>{LEVEL_MAX}/10</code> (max)\n"
        f" Echo <code>OFF</code> (clear voice)\n"
        f" Live mic <code>{VOL_MAX}</code> (200% — Telegram max)\n"
        f" Volume keeper: har {Config.KEEPER_INTERVAL}s par volume wapas "
        f"max par pin ho jayega (reset/reconnect ke baad bhi)\n\n"
        + (f" {applied} live VC par turant apply ho gaya." if applied else
           " Save ho gaya — <code>.play</code> karte hi khud lag jayega.")
        + "\n\n<i>Note: 200% Telegram ka server-side hard cap hai; usse aage "
          "loudness FFmpeg chain (dynaudnorm + compressor + volume + limiter) "
          "se aati hai, jo AUTO me poori max par hai.</i>",
        reply_markup=AUTO_KB,
    )


# ── Log channel diagnostics (owner) ──────────────────────────────────────────
@Client.on_message(filters.regex(r"^[./]logtest\b") & filters.private)
async def cmd_logtest(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    problem = await verify_log_channel()
    if problem:
        await msg.reply_text(f" <b>Log channel kaam nahi kar raha</b>\n\n{problem}")
    else:
        await msg.reply_text(
            f" <b>Log channel OK</b> — test message bhej diya.\n"
            f"Channel: <code>{get_channel()}</code>"
        )


@Client.on_message(filters.regex(r"^[./]setlog\b") & filters.private)
async def cmd_setlog(bot: Client, msg: Message):
    if not Config.is_owner(msg.from_user.id):
        return
    parts = msg.text.strip().split()
    if len(parts) < 2:
        await msg.reply_text(
            f"Usage: <code>.setlog -100xxxxxxxxxx</code>\n"
            f"Abhi: <code>{get_channel()}</code>"
        )
        return
    try:
        set_channel(int(parts[1]))
    except ValueError:
        await msg.reply_text(" Channel ID number honi chahiye (<code>-100…</code>).")
        return
    problem = await verify_log_channel()
    await msg.reply_text(
        f"{' ' + problem if problem else ' Log channel set + verified'}\n"
        f"Channel: <code>{get_channel()}</code>"
    )


# ── Relay audio controls ──────────────────────────────────────────────────────
def _relay_num_arg(msg: Message):
    parts = msg.text.strip().split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


@Client.on_message(filters.regex(r"^[./]volume\b") & (filters.group | filters.private))
async def cmd_volume(bot: Client, msg: Message):
    n = _relay_num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>/volume &lt;0-1000&gt;</code>")
        return
    n = max(0, min(VOLUME_MAX, n))
    await _apply_and_reply(msg, f" Playback volume: <b>{n}/1000</b>", relay_volume=n, volume=n)


@Client.on_message(filters.regex(r"^[./]gain\b") & (filters.group | filters.private))
async def cmd_gain(bot: Client, msg: Message):
    n = _relay_num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>/gain &lt;0-150&gt;</code>")
        return
    n = max(0, min(150, n))
    await _apply_and_reply(msg, f" Gain: <b>{n}/150</b>", gain=n)


@Client.on_message(filters.regex(r"^[./]treble\b") & (filters.group | filters.private))
async def cmd_treble(bot: Client, msg: Message):
    n = _relay_num_arg(msg)
    if n is None:
        await msg.reply_text("Usage: <code>/treble &lt;0-100&gt;</code>")
        return
    n = max(0, min(100, n))
    await _apply_and_reply(msg, f" Treble: <b>{n}/100</b>", treble=n)


@Client.on_message(filters.regex(r"^[./]voice\b") & (filters.group | filters.private))
async def cmd_voice(bot: Client, msg: Message):
    parts = msg.text.strip().split()
    if len(parts) < 2 or parts[1].lower() not in {"female", "male", "normal"}:
        await msg.reply_text(
            "Usage: <code>/voice female|male|normal</code>\n\n"
            "female: sharp/bright | male: heavy/bassy | normal: balanced"
        )
        return
    profile = parts[1].lower()
    values = {
        "female": {"bass": 5, "treble": 70},
        "male": {"bass": 60, "treble": 15},
        "normal": {"bass": Config.RELAY_DEFAULT_BASS, "treble": Config.RELAY_DEFAULT_TREBLE},
    }[profile]
    await _apply_and_reply(
        msg,
        f" Voice profile: <b>{profile}</b>",
        voice=profile, bass=values["bass"], treble=values["treble"],
    )


@Client.on_message(filters.regex(r"^[./]relaystatus\b") & (filters.group | filters.private))
async def cmd_relaystatus(bot: Client, msg: Message):
    s = await db.get_settings(msg.from_user.id)
    await msg.reply_text(
        " <b>VC Audio Relay Settings</b>\n\n"
        f"├ Volume: <code>{s.get('relay_volume', Config.RELAY_DEFAULT_VOLUME)}/1000</code>\n"
        f"├ Gain: <code>{s.get('gain', Config.RELAY_DEFAULT_GAIN)}/150</code>\n"
        f"├ Bass: <code>{s.get('bass', Config.RELAY_DEFAULT_BASS)}/100</code>\n"
        f"├ Treble: <code>{s.get('treble', Config.RELAY_DEFAULT_TREBLE)}/100</code>\n"
        f"├ Live mic: <code>{s.get('live_volume', Config.LIVE_BOOST_DEFAULT)}/20000</code>\n"
        f"└ Voice: <code>{s.get('voice', 'normal')}</code>"
    )





@Client.on_message(filters.regex(r"^[./]mic\b") & (filters.group | filters.private))
async def cmd_mic(bot: Client, msg: Message):
    uvc = await get_engine(msg)
    if not uvc:
        return
    parts = msg.text.strip().split()
    action = parts[1].lower() if len(parts) > 1 else "devices"
    if action in {"devices", "list"}:
        devices = uvc.microphone_devices()
        if not devices:
            await msg.reply_text(
                " Server par microphone/virtual microphone nahi mila."
            )
            return
        lines = [f"{i}. <code>{d.metadata}</code> — {d.title}"
                 for i, d in enumerate(devices, 1)]
        await msg.reply_text(" <b>Available microphone inputs</b>\n" + "\n".join(lines))
        return
    if action not in {"on", "start"}:
        await msg.reply_text(
            "Usage: <code>/mic devices</code>\n"
            "<code>/mic on [device-name] [-100xxxxxxxx]</code>"
        )
        return
    cid_arg = None
    device_parts = []
    for value in parts[2:]:
        try:
            number = int(value)
        except ValueError:
            device_parts.append(value)
            continue
        if number < 0:
            cid_arg = value
        else:
            device_parts.append(value)
    cid = await need_chat(msg, cid_arg)
    if not cid:
        return
    try:
        title = await uvc.play_microphone(cid, " ".join(device_parts))
        await msg.reply_text(
            f" <b>Live microphone ON</b>\n"
            f"Input: <code>{title}</code>\n"
            f"Gain: <code>{uvc.state(cid).live_volume}/20000</code>\n\n"
            "Note: mic playback stream ko replace karta hai; `/play` se audio stream wapas chala sakte hain."
        )
    except Exception as exc:
        await msg.reply_text(f" <b>Microphone start nahi hua:</b> <code>{exc}</code>")
