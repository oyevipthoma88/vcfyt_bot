"""Live Mic = VC Bridge. `.mic on/off` (aur `.bridge`) — private VC me bolo,
spare ID target VC me loud bolegi. Browser mic page aur RTMP hata diye gaye."""

import re

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.types import InlineKeyboardMarkup as K

from helpers.database import db
from helpers.vc_manager import session_manager
from plugins.ui import HAS_USER, cmd_text, cmd_prefix, B, edit_screen, safe_answer
from plugins.vc_commands import get_engine, mic_notify, target_chat

HELP = (
    "🎤 <b>Live Mic (VC Bridge)</b> — Telegram VC se live aawaz\n\n"
    "<b>Setup (ek baar):</b>\n"
    "1. Ek private group banao, main ID + spare ID dono add karo\n"
    "2. Target group me bhi main ID + spare ID dono ho\n"
    "3. Dono groups me VC start karo\n"
    "4. <code>.mic src -100PRIVATE</code> — private group set\n"
    "5. <code>.mic chat -100TARGET</code> — target group set\n\n"
    "<b>Use:</b>\n"
    "• Main ID se PRIVATE VC join karo, mic ON\n"
    "• <code>.mic on</code> — clean preset (sabse tez + saaf, default)\n"
    "• <code>.mic on bass</code> / <code>echo</code> / <code>full</code> — extra effect (echo awaaz thodi door karta hai)\n"
    "• <code>.mic on -100TARGET</code> — kisi aur target ke liye\n"
    "• <code>.mic off</code> — band (spare target VC me baithi rahegi)\n"
    "• <code>.mic leave</code> — band + spare dono VC se bahar\n"
    "• <code>.mic status</code>\n"
    "• <code>.mic loud</code> — 🔥 LOUD panel: <b>🚀 Drive</b> se awaaz badhao (0–20, 11+ par thodi phategi)\n"
    "• <code>.mic loud fight</code> — ek command me sabse tez fight mode\n\n"
    "<i><code>.bridge ...</code> bhi same kaam karta hai. Main ID target VC join na kare — "
    "target VC me spare ID khud join hoke aapki aawaz bolegi.</i>"
)


def _bar(n: int, top: int = 10) -> str:
    k = max(0, min(10, round(n * 10 / top)))
    return "█" * k + "░" * (10 - k)


def loud_text(c: dict, live: bool) -> str:
    from helpers.vc_bridge import drive_db
    zone = "🔥 FIGHT zone (tez + thodi phati)" if c["drive"] > 10 else "✅ saaf + tez"
    return (
        "🔥 <b>Live Mic LOUD Control</b>\n\n"
        f"Status: {'🟢 LIVE — turant lagega' if live else '⚪ Bridge OFF — agle .mic on par lagega'}\n\n"
        f"🚀 <b>Drive (VOLUME):</b> <code>{_bar(c['drive'], 20)} {c['drive']}/20</code> (+{drive_db(c['drive'])} dB) — {zone}\n"
        f"🎵 <b>Bass punch:</b> <code>{_bar(c['bass'])} {c['bass']}/10</code>\n"
        f"📢 <b>Presence:</b> <code>{_bar(c['presence'])} {c['presence']}/10</code> (kaan me tez lagne wali range)\n"
        f"✂️ <b>Clip:</b> <b>{'HARD (max tez, phategi)' if c['clip'] == 'hard' else 'SOFT (thodi smooth)'}</b>\n\n"
        "<b>Awaaz badhane ka control = 🚀 Drive.</b>\n"
        "• 0–6: saaf awaaz (default 6)\n"
        "• 7–10: bohot tez\n"
        "• 11–20: FIGHT — sabse tez, awaaz thodi phategi (saamne wala barabar ho tab)\n"
        "Ek tap me: <b>💥 MAX</b> (14) ya <b>⚔️ FIGHT</b> (20). Zyada phate to Drive kam karo ya Clip SOFT. "
        "Bass 0 rakho — shabd zyada tez aate hain.\n\n"
        "Text se: <code>.mic loud 0-20</code> • <code>.mic loud safe|loud|max|fight</code> • "
        "<code>.mic loud bass 0-10</code> • <code>.mic loud presence 0-10</code> • "
        "<code>.mic loud clip hard|soft</code>"
    )


def loud_kb() -> K:
    return K([
        [B("🚀 Drive −1", callback_data="brl:drive:-1", style="primary"),
         B("Drive +1 🚀", callback_data="brl:drive:1", style="success")],
        [B("⏬ Drive −3", callback_data="brl:drive:-3", style="primary"),
         B("Drive +3 ⏫", callback_data="brl:drive:3", style="success")],
        [B("🎵 Bass −1", callback_data="brl:bass:-1", style="primary"),
         B("Bass +1 🎵", callback_data="brl:bass:1", style="primary")],
        [B("📢 Presence −1", callback_data="brl:presence:-1", style="primary"),
         B("Presence +1 📢", callback_data="brl:presence:1", style="primary")],
        [B("✂️ Clip HARD/SOFT", callback_data="brl:clip:x", style="primary")],
        [B("🛡 Safe", callback_data="brl:p:safe", style="primary"),
         B("🔊 Loud", callback_data="brl:p:loud", style="success"),
         B("💥 MAX", callback_data="brl:p:max", style="danger"),
         B("⚔️ FIGHT", callback_data="brl:p:fight", style="danger")],
    ])


async def _loud_change(uid: int, key: str, val: str) -> dict:
    from helpers import vc_bridge
    c = await vc_bridge.load_loud(uid)
    if key == "p" and val in vc_bridge.LOUD_PRESETS:
        c = dict(vc_bridge.LOUD_PRESETS[val])
    elif key == "clip":
        c["clip"] = val if val in ("hard", "soft") else ("soft" if c["clip"] == "hard" else "hard")
    elif key in vc_bridge.LOUD_LIMITS:
        c[key] = int(c[key]) + int(val)
    elif key == "set" and key:
        pass
    return await vc_bridge.save_loud(uid, c)


async def loud_command(msg: Message, args):
    from helpers import vc_bridge
    uid = msg.from_user.id
    c = await vc_bridge.load_loud(uid)
    a = [x.lower() for x in args]
    try:
        if a:
            if a[0] in vc_bridge.LOUD_PRESETS:
                c = dict(vc_bridge.LOUD_PRESETS[a[0]])
            elif a[0].isdigit():
                c["drive"] = int(a[0])
            elif a[0] in ("drive", "bass", "presence") and len(a) > 1:
                c[a[0]] = int(a[1])
            elif a[0] == "clip" and len(a) > 1 and a[1] in ("hard", "soft"):
                c["clip"] = a[1]
            c = await vc_bridge.save_loud(uid, c)
    except ValueError:
        pass
    live = vc_bridge.get_bridge(uid) is not None
    await mic_notify(msg, loud_text(c, live), reply_markup=loud_kb())


@Client.on_callback_query(filters.regex(r"^brl:"))
async def cb_loud(bot: Client, cq):
    from helpers import vc_bridge
    _, key, val = (cq.data.split(":") + ["", ""])[:3]
    uid = cq.from_user.id
    c = await _loud_change(uid, key, val)
    live = vc_bridge.get_bridge(uid) is not None
    await edit_screen(cq.message, loud_text(c, live), reply_markup=loud_kb())
    await safe_answer(cq, f"Drive {c['drive']} · Bass {c['bass']} · Presence {c['presence']} · {c['clip'].upper()}")


@Client.on_message(HAS_USER & cmd_prefix(r"bridge\b", flags=re.IGNORECASE) & (filters.group | filters.private))
async def cmd_bridge(bot: Client, msg: Message):
    await run_bridge(msg, cmd_text(msg).split())


async def run_bridge(msg: Message, parts):
    """Shared by `.bridge` and `.mic` — parts[1] is the action."""
    from helpers import vc_bridge
    from helpers.live_mic import reset_mic_state

    uid = msg.from_user.id
    action = parts[1].lower() if len(parts) > 1 else "help"
    if msg.chat and msg.chat.id < 0:
        try:
            await msg.delete()
        except Exception:
            pass

    if action == "help":
        await mic_notify(msg, HELP)
        return

    if action in {"loud", "power", "boost"}:
        await loud_command(msg, parts[2:])
        return

    if action in {"src", "source"}:
        if len(parts) < 3:
            await mic_notify(msg, "Private group do: <code>.mic src -100xxxxxxxx</code>")
            return
        cid, _ = await target_chat(msg, parts[2])
        if not cid:
            await mic_notify(msg, "❌ Private group nahi mila. Chat ID (-100…) do.")
            return
        await db.set_app_value(f"bridge_src_{uid}", str(cid))
        await mic_notify(msg, f"✅ Private (source) group set: <code>{cid}</code>")
        return

    if action == "status":
        b = vc_bridge.get_bridge(uid)
        src = await db.get_app_value(f"bridge_src_{uid}")
        tgt = await db.get_app_value(f"mic_chat_{uid}")
        if not b:
            await mic_notify(msg, f"🎤 Live Mic (Bridge) OFF\nSource: <code>{src or '—'}</code>\n"
                                  f"Target: <code>{tgt or '—'}</code>")
            return
        s = b.status()
        await mic_notify(msg, (
            f"🎤 Live Mic (Bridge) {'🟢 LIVE' if s['live'] else '🟡 starting'}\n"
            f"Source: <code>{s['source']}</code>\nTarget: <code>{s['target']}</code>\n"
            f"Preset: <b>{s['preset']}</b>\nAudio: {s['received_kb']} KB · {s['uptime_s']}s\n"
            f"🔥 Loud: drive {s['loud']['drive']}/20 · bass {s['loud']['bass']} · "
            f"presence {s['loud']['presence']} · {s['loud']['clip']}"))
        return

    if action in {"off", "stop", "leave"}:
        ok = await vc_bridge.stop_bridge(uid, leave_target=(action == "leave"))
        uvc = await session_manager.get(uid)
        if uvc:
            try:
                await reset_mic_state(uvc, uid)
            except Exception:
                pass
        await mic_notify(msg, "🛑 Bridge band." if ok else "Bridge chal nahi raha tha.")
        return

    if action != "on":
        await mic_notify(msg, HELP)
        return

    uvc = await get_engine(msg)
    if not uvc:
        return
    relay = await session_manager.get_relay(uid)
    if relay is None:
        await mic_notify(msg, "❌ Spare ID set nahi hai. Live Mic panel → 👥 Spare Mic Account se login karo.")
        return
    preset = vc_bridge.DEFAULT_PRESET
    tgt_arg = None
    for p in parts[2:]:
        if p.lower() in vc_bridge.PRESETS:
            preset = p.lower()
        elif p.startswith("-100") or p.startswith("@") or "t.me/" in p:
            tgt_arg = p
    src = await db.get_app_value(f"bridge_src_{uid}")
    if not src:
        await mic_notify(msg, "Pehle private group set karo: <code>.mic src -100xxxxxxxx</code>")
        return
    tgt_arg = tgt_arg or await db.get_app_value(f"mic_chat_{uid}")
    if not tgt_arg:
        await mic_notify(msg, "Target group set karo: <code>.mic chat -100xxxxxxxx</code>")
        return
    tgt, _ = await target_chat(msg, str(tgt_arg))
    src_id = int(src)
    if not tgt:
        await mic_notify(msg, "❌ Target group nahi mila.")
        return
    if tgt == src_id:
        await mic_notify(msg, "❌ Private aur target group alag hone chahiye.")
        return
    await db.set_app_value(f"mic_chat_{uid}", str(tgt))
    await reset_mic_state(uvc, uid)

    note = await mic_notify(msg, (
        f"⏳ Bridge start ho raha hai (preset <b>{preset}</b>)…\n"
        "Ab main ID se PRIVATE VC me mic ON karke bolo (30 sec ke andar)."))
    try:
        settings = await db.get_settings(uid)
        await vc_bridge.start_bridge(uid, uvc, relay, src_id, tgt, settings, preset)
    except Exception as exc:
        text = f"❌ Bridge start nahi hua:\n<code>{str(exc)[:400]}</code>"
    else:
        text = (f"🟢 <b>Bridge LIVE</b>\nPrivate <code>{src_id}</code> → Target <code>{tgt}</code>\n"
                f"Preset: <b>{preset}</b> · Spare volume 200\n"
                "🔥 Aawaz control: <code>.mic loud</code>\n"
                "Band: <code>.mic off</code>")
    try:
        if note:
            await note.edit_text(text)
            return
    except Exception:
        pass
    await mic_notify(msg, text)
