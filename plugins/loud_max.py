"""v77 LOUD MAX controls.

  .maxloud / .tez      -> phone-max mode + all play settings max + har logged-in
                          ID (main + spares) ko VC me 200% + admin try
  .loudmode            -> current mode dikhao
  .micmode loudest|clean|warm -> live mic ka chain
  .loudmode phone|bass -> phone = phone speaker pe sabse tez + clear (default)
                          bass  = purana heavy-bass chain (headphones)
"""
import logging
import re

from pyrogram import Client, filters
from pyrogram.types import Message

from helpers import playback_v77 as v77
from helpers import live_mic_v79 as v79
from helpers.database import db
from helpers.vc_manager import session_manager
from plugins.ui import HAS_USER, cmd_prefix, cmd_text

logger = logging.getLogger("vcbot.loud_max")

MAX_PLAY = dict(volume=1000, boost=10, gain=500, treble=100)


@Client.on_message(HAS_USER & cmd_prefix(r"loudmode\b", flags=re.IGNORECASE)
                   & (filters.group | filters.private))
async def cmd_loudmode(bot: Client, msg: Message):
    parts = cmd_text(msg).split()
    if len(parts) < 2:
        await msg.reply_text(
            f"🔊 <b>Loud mode:</b> <code>{v77.get_mode()}</code>\n\n"
            "<code>.loudmode loudest</code> — sabse tez, phone speaker ke liye (default)\n"
            "<code>.loudmode phone</code> — tez + thoda zyada saaf\n"
            "<code>.loudmode bass</code> — heavy bass (headphones ke liye)")
        return
    try:
        mode = v77.set_mode(parts[1])
    except ValueError:
        await msg.reply_text("❌ Sirf <code>loudest</code>, <code>phone</code> ya <code>bass</code>.")
        return
    await db.set_app_value("play_loud_mode", mode)
    from plugins.start import apply_settings_live
    await apply_settings_live(msg.from_user.id)
    await msg.reply_text(f"✅ Loud mode: <b>{mode}</b> (agla/chalu audio isi se bajega)")


@Client.on_message(HAS_USER & cmd_prefix(r"(maxloud|tez|loudmax)\b", flags=re.IGNORECASE)
                   & (filters.group | filters.private))
async def cmd_maxloud(bot: Client, msg: Message):
    await msg.reply_text("\n".join(await do_maxloud(msg.from_user.id)))


async def do_maxloud(uid: int) -> list:
    """.tez / panel ⚡ TEZ button — dono yahi chalate hai."""
    v77.set_mode("loudest")
    await db.set_app_value("play_loud_mode", "loudest")
    v79.set_mode("loudest")
    await db.set_app_value("live_mic_loud_mode", "loudest")
    await v79.restart_live_sessions()
    await db.save_settings(uid, **MAX_PLAY)
    from plugins.start import apply_settings_live
    await apply_settings_live(uid)

    uvc = session_manager.users.get(uid)
    cid = None
    if uvc:
        cid = next((c for c, st in uvc.chats.items() if st.is_playing or st.mic_enabled), None)
    lines = ["⚡ <b>MAX LOUD ON</b>", "• LOUDEST awaaz (file + live mic) + saari settings max"]
    if not cid:
        lines.append("• Abhi koi VC me play nahi ho raha — play karke dobara <code>.tez</code> bhejo "
                     "taaki sab IDs ko 200% mile.")
        return lines

    from plugins.sync_play import collect_engines
    engines = await collect_engines(uid, True)
    ids = [getattr(e, "account_id", None) for e in engines if getattr(e, "account_id", None)]
    admin_ok = 0
    for eng in engines:
        aid = getattr(eng, "account_id", None)
        promote = getattr(uvc, "_auto_promote_voice_admin", None)
        if aid and promote:
            try:
                await promote(cid, aid)
            except Exception as exc:
                logger.debug("promote %s failed: %r", aid, exc)
    for eng in engines:
        for aid in ids:
            try:
                if await eng.set_participant_volume(cid, aid, 20000, quiet=True):
                    admin_ok += 1
            except Exception:
                pass
    lines.append(f"• IDs: <b>{len(engines)}</b>, 200% laga: <b>{admin_ok}</b> baar")
    if not admin_ok:
        lines.append("⚠️ Koi ID group admin (Manage Voice Chats) nahi hai — Telegram sabke liye "
                     "100% se upar nahi jaane deta. Ek ID ko admin banao.")
    if len(engines) > 1:
        lines.append(f"💡 <code>.syncplay</code> se saari {len(engines)} IDs ek saath bajengi = aur ghana/tez.")
    return lines


@Client.on_message(HAS_USER & cmd_prefix(r"micmode\b", flags=re.IGNORECASE)
                   & (filters.group | filters.private))
async def cmd_micmode(bot: Client, msg: Message):
    parts = cmd_text(msg).split()
    if len(parts) < 2:
        await msg.reply_text(
            f"🎙 <b>Live mic mode:</b> <code>{v79.get_mode()}</code>\n\n"
            "<code>.micmode loudest</code> — live mic sabse tez, phone ke liye (default)\n"
            "<code>.micmode clean</code> — no saturation, safe peak\n"
            "<code>.micmode warm</code> — softer, natural voice")
        return
    try:
        mode = v79.set_mode(parts[1])
    except ValueError:
        await msg.reply_text("❌ Sirf <code>loudest</code>, <code>clean</code> ya <code>warm</code>.")
        return
    await db.set_app_value("live_mic_loud_mode", mode)
    n = await v79.restart_live_sessions()
    await msg.reply_text(f"✅ Live mic mode: <b>{mode}</b>" + (f" ({n} chalu mic update)" if n else ""))
