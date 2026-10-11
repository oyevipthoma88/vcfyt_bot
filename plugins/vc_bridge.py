"""Live Mic = VC Bridge. `.mic on/off` (aur `.bridge`) — private VC me bolo,
spare ID target VC me loud bolegi. Browser mic page aur RTMP hata diye gaye."""

import re

from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.types import InlineKeyboardMarkup as K

from helpers.database import db
from helpers.vc_manager import session_manager
from plugins.ui import HAS_USER, cmd_text, cmd_prefix, B, edit_screen, safe_answer
from plugins.vc_commands import get_engine, mic_notify, target_chat, is_chat_ref

HELP = (
    "🎤 <b>Live Mic — apni aawaz kisi bhi VC me, LOUD + clear</b>\n\n"
    "<b>⚡ 1-tap setup:</b> <code>.mic setup</code>\n"
    "Bot khud aapke liye private <b>Mic Room</b> group banata hai, spare ID add\n"
    "karta hai aur VC chalu karta hai. Kuch copy-paste nahi.\n\n"
    "<b>▶ Use (3 step):</b>\n"
    "1. <code>.mic chat -100TARGET</code> — jis group me aawaz jaani hai (ek baar)\n"
    "2. Mic Room ki VC join karo (main ID), mic ON\n"
    "3. <code>.mic on</code> — bolo, target VC me tez aawaz jayegi\n\n"
    "<b>🎛 Control:</b>\n"
    "• <code>.mic loud</code> — panel (Drive 0–30, presets, bass, presence)\n"
    "• <code>.mic loud fight</code> / <code>ultra</code> — ek command me max\n"
    "• <code>.mic on bass|echo|full</code> — effect • <code>.mic on -100xxx</code> — dusra target\n"
    "• <code>.mic status</code> — health check (spare, admin boost, audio)\n"
    "• <code>.mic off</code> — band • <code>.mic leave</code> — band + spare VC se bahar\n"
    "• <code>.mic src -100xxx</code> — apna khud ka private group (optional)\n\n"
    "<b>🚀 Asli 200% boost:</b> spare ID target group me sirf \"Manage video chats\"\n"
    "admin ban jaaye to uska 200% volume <b>sabke liye</b> lagta hai (+6 dB, bina\n"
    "phate). Main ID ya bot admin ho to ye apne aap hota hai.\n\n"
    "<i>Spare ID nahi hai? Owner ki spare pool se apne aap milti hai — warna\n"
    "<code>.micaccount</code> se phone/OTP login.</i>"
)


def _bar(n: int, top: int = 10) -> str:
    k = max(0, min(10, round(n * 10 / top)))
    return "█" * k + "░" * (10 - k)


def loud_text(c: dict, live: bool, boost_all=None) -> str:
    from helpers.vc_bridge import drive_db
    d = c["drive"]
    zone = ("☢️ ULTRA (4-stage clip, sabse tez, phategi)" if d > 20 else
            "🔥 FIGHT zone (bohot tez + thodi phati)" if d > 15 else
            "✅ saaf + tez")
    return (
        "🎤 <b>Live Mic Control Panel</b>\n\n"
        f"Status: {'🟢 LIVE — har badlaav turant lagega' if live else '⚪ OFF — 🟢 Mic ON dabao'}\n"
        + ("" if boost_all is None else
           ("🚀 <b>200% boost: SABKE LIYE ON</b> (spare = VC admin)\n" if boost_all else
            "⚠️ 200% boost sirf spare ko — group me spare ko \"Manage video chats\" admin do\n"))
        + "\n"
        f"🚀 <b>Volume (Drive):</b> <code>{_bar(d, 30)} {d}/30</code> (+{drive_db(d)} dB)\n"
        f"      {zone}\n"
        f"📢 <b>Presence:</b> <code>{_bar(c['presence'])} {c['presence']}/10</code>\n"
        f"🎵 <b>Bass:</b> <code>{_bar(c['bass'])} {c['bass']}/10</code>\n"
        f"✂️ <b>Clip:</b> {'HARD (max tez)' if c['clip'] == 'hard' else 'SOFT (smooth)'}\n\n"
        "<b>Ek tap presets:</b> 🛡 Safe 6 • 🔊 Loud 18 • 💥 MAX 20 • ⚔️ FIGHT 26 • ☢️ ULTRA 30\n"
        "Aawaz zyada phate to Volume kam karo ya Clip SOFT. Bass 0 = shabd sabse tez.\n\n"
        "Text: <code>.mic loud 0-30</code> • <code>.mic loud safe|loud|max|fight|ultra</code>"
    )


def loud_kb() -> K:
    return K([
        [B("🟢 Mic ON", callback_data="brc:on", style="success"),
         B("🔴 Mic OFF", callback_data="brc:off", style="danger")],
        [B("🔉 −5", callback_data="brl:drive:-5", style="primary"),
         B("🔉 −1", callback_data="brl:drive:-1", style="primary"),
         B("🔊 +1", callback_data="brl:drive:1", style="success"),
         B("🔊 +5", callback_data="brl:drive:5", style="success")],
        [B("🛡 Safe", callback_data="brl:p:safe", style="primary"),
         B("🔊 Loud", callback_data="brl:p:loud", style="success"),
         B("💥 MAX", callback_data="brl:p:max", style="danger")],
        [B("⚔️ FIGHT", callback_data="brl:p:fight", style="danger"),
         B("☢️ ULTRA", callback_data="brl:p:ultra", style="danger")],
        [B("📢 −", callback_data="brl:presence:-1", style="primary"),
         B("📢 Presence +", callback_data="brl:presence:1", style="primary"),
         B("🎵 −", callback_data="brl:bass:-1", style="primary"),
         B("🎵 Bass +", callback_data="brl:bass:1", style="primary")],
        [B("✂️ Clip HARD/SOFT", callback_data="brl:clip:x", style="primary"),
         B("♻️ Reset", callback_data="brl:reset:x", style="primary")],
        [B("📊 Status", callback_data="brc:status", style="primary"),
         B("🔄 Refresh", callback_data="brc:loud", style="primary"),
         B("🚪 Leave", callback_data="brc:leave", style="danger")],
        [B("⚡ Auto Setup", callback_data="brc:setup", style="success"),
         B("🚀 200% Fix", callback_data="brc:boost", style="success"),
         B("❓ Help", callback_data="brc:help", style="primary")],
    ])


class _CbMsg:
    """Callback ko Message jaisa bana deta hai, taaki panel ke buttons wahi
    run_bridge logic chalaayein jo `.mic on/off/status` chalata hai."""

    def __init__(self, cq):
        self._client = cq._client if hasattr(cq, "_client") else cq.message._client
        self.from_user = cq.from_user
        self.chat = cq.message.chat
        self.reply_to_message = None

    async def reply_text(self, text, **kw):
        return await mic_notify(self, text, **kw)

    async def delete(self):
        return None


@Client.on_callback_query(filters.regex(r"^brc:"))
async def cb_mic_control(bot: Client, cq):
    action = cq.data.split(":", 1)[1]
    if action not in {"on", "off", "status", "leave", "loud", "setup", "boost", "help"}:
        return await safe_answer(cq, "?")
    await safe_answer(cq, {"on": "Mic ON ho raha hai…", "off": "Mic band…",
                           "leave": "VC se bahar…", "status": "Status…",
                           "loud": "Refresh", "setup": "Mic Room ban raha hai…",
                           "boost": "200% boost check…", "help": "Help"}[action])
    if action == "loud":
        from helpers import vc_bridge
        c = await vc_bridge.load_loud(cq.from_user.id)
        live = vc_bridge.get_bridge(cq.from_user.id) is not None
        await edit_screen(cq.message, loud_text(c, live), reply_markup=loud_kb())
        return
    await run_bridge(_CbMsg(cq), ["mic", action])


async def _loud_change(uid: int, key: str, val: str) -> dict:
    from helpers import vc_bridge
    c = await vc_bridge.load_loud(uid)
    if key == "p" and val in vc_bridge.LOUD_PRESETS:
        c = dict(vc_bridge.LOUD_PRESETS[val])
    elif key == "clip":
        c["clip"] = val if val in ("hard", "soft") else ("soft" if c["clip"] == "hard" else "hard")
    elif key in vc_bridge.LOUD_LIMITS:
        try:
            c[key] = int(c[key]) + int(val)
        except ValueError:
            pass
    elif key == "reset":
        c = dict(vc_bridge.LOUD_DEFAULT)
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
    b = vc_bridge.get_bridge(uid)
    await mic_notify(msg, loud_text(c, b is not None, b.boost_all if b else None),
                     reply_markup=loud_kb())


@Client.on_callback_query(filters.regex(r"^brl:"))
async def cb_loud(bot: Client, cq):
    from helpers import vc_bridge
    _, key, val = (cq.data.split(":") + ["", ""])[:3]
    uid = cq.from_user.id
    c = await _loud_change(uid, key, val)
    b = vc_bridge.get_bridge(uid)
    await edit_screen(cq.message, loud_text(c, b is not None, b.boost_all if b else None),
                      reply_markup=loud_kb())
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

    if action in {"loud", "power", "boost", "panel", "control", "ctrl"}:
        await loud_command(msg, parts[2:])
        return

    if action in {"src", "source"}:
        if len(parts) < 3:
            await mic_notify(msg, "Private group do: <code>.mic src -100xxxxxxxx</code>")
            return
        cid, _ = await target_chat(msg, parts[2])
        if not cid:
            await mic_notify(msg, "❌ Private group nahi mila. Chat ID (-100 ke saath ya bina) / @username / link do.")
            return
        await db.set_app_value(f"bridge_src_{uid}", str(cid))
        await mic_notify(msg, f"✅ Private (source) group set: <code>{cid}</code>")
        return

    if action == "status":
        await mic_notify(msg, await status_text(uid))
        return

    if action in {"setup", "room", "auto"}:
        await setup_command(msg, uid)
        return

    if action in {"boost", "fix", "admin"}:
        await boost_command(msg, uid)
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
        await mic_notify(msg, NO_SPARE)
        return
    preset = vc_bridge.DEFAULT_PRESET
    tgt_arg = None
    for p in parts[2:]:
        if p.lower() in vc_bridge.PRESETS:
            preset = p.lower()
        elif is_chat_ref(p):
            tgt_arg = p
    src = await db.get_app_value(f"bridge_src_{uid}")
    if not src:
        # Private group nahi hai -> khud bana do (1-tap setup).
        from helpers import mic_tools
        try:
            src = await mic_tools.auto_room(uvc, relay, uid)
        except Exception as exc:
            await mic_notify(msg, "❌ Mic Room auto nahi bana: <code>"
                             f"{str(exc)[:200]}</code>\nKhud banao aur <code>.mic src -100xxx</code> do.")
            return
        await mic_notify(msg, room_ready_text(src), reply_markup=room_kb(src))
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
        "Ab main ID se <b>Mic Room</b> ki VC me mic ON karke bolo (30 sec ke andar)."),
        reply_markup=room_kb(src_id))
    try:
        settings = await db.get_settings(uid)
        await vc_bridge.start_bridge(uid, uvc, relay, src_id, tgt, settings, preset)
    except Exception as exc:
        text = f"❌ Bridge start nahi hua:\n<code>{str(exc)[:400]}</code>"
        try:
            if note:
                await note.edit_text(text)
                return
        except Exception:
            pass
        await mic_notify(msg, text)
        return
    # Mic ON hote hi control panel auto-show: LIVE status + saare buttons.
    c = await vc_bridge.load_loud(uid)
    b = vc_bridge.get_bridge(uid)
    panel = loud_text(c, True, b.boost_all if b else None)
    try:
        if note:
            await note.edit_text(panel, reply_markup=loud_kb())
            return
    except Exception:
        pass
    await mic_notify(msg, panel, reply_markup=loud_kb())


NO_SPARE = (
    "❌ <b>Spare ID nahi mili.</b>\n\n"
    "Telegram ek account ko ek hi VC me rehne deta hai, isliye aawaz bolne ke\n"
    "liye ek doosri (spare) ID chahiye.\n\n"
    "• Bot DM: <code>.micaccount</code> → 📱 Phone se login (sirf number + OTP)\n"
    "• Ya owner <code>ASSISTANT_SESSIONS</code> me spare pool set kare — phir\n"
    "  kisi user ko doosra login nahi karna padta."
)


def room_ready_text(src) -> str:
    return ("✅ <b>Mic Room ready!</b> <code>" + str(src) + "</code>\n"
            "Spare ID add ho gayi, VC chalu hai. Niche button se room kholo,\n"
            "VC join karo aur mic ON karo.")


def room_kb(src) -> K:
    from helpers.mic_tools import room_link
    return K([[B("📲 Mic Room kholo", url=room_link(int(src)))]])


async def status_text(uid: int) -> str:
    """Ek nazar me health check: kya theek hai, kya missing hai."""
    from helpers import vc_bridge
    src = await db.get_app_value(f"bridge_src_{uid}")
    tgt = await db.get_app_value(f"mic_chat_{uid}")
    source = await session_manager.assistant_source(uid)
    spare = {"own": "✅ apni spare", "pool": "✅ owner pool se (auto)"}.get(source, "❌ nahi — .micaccount")
    b = vc_bridge.get_bridge(uid)
    lines = ["🩺 <b>Live Mic — Status</b>\n",
             f"Main ID: {'✅ login' if session_manager.users.get(uid) else '❌ login karo'}",
             f"Spare ID: {spare}",
             f"Mic Room: {'<code>' + str(src) + '</code>' if src else '⚪ .mic setup'}",
             f"Target: {'<code>' + str(tgt) + '</code>' if tgt else '⚪ .mic chat -100xxx'}"]
    if not b:
        lines.append("\nMic: ⚪ OFF — <code>.mic on</code>")
        return "\n".join(lines)
    s = b.status()
    lines += [
        f"\nMic: {'🟢 LIVE' if s['live'] else '🟡 starting (Mic Room me bolo)'} · {s['uptime_s']}s",
        f"Audio aaya: {s['received_kb']} KB {'✅' if s['received_kb'] else '❌ Mic Room me mic ON?'}",
        f"Preset: <b>{s['preset']}</b>",
        f"🔥 Drive {s['loud']['drive']}/30 · bass {s['loud']['bass']} · presence "
        f"{s['loud']['presence']} · {s['loud']['clip']}",
        ("🚀 200% boost: SABKE LIYE ✅" if s.get("boost_all") else
         "⚠️ 200% boost sirf spare ko — <code>.mic boost</code> try karo"),
    ]
    return "\n".join(lines)


async def setup_command(msg, uid: int) -> None:
    from helpers import mic_tools
    uvc = await get_engine(msg)
    if not uvc:
        return
    relay = await session_manager.get_relay(uid)
    if relay is None:
        await mic_notify(msg, NO_SPARE)
        return
    note = await mic_notify(msg, "⏳ Mic Room ban raha hai…")
    try:
        await db.set_app_value(f"bridge_src_{uid}", "")
        src = await mic_tools.auto_room(uvc, relay, uid)
    except Exception as exc:
        text = f"❌ Mic Room nahi bana: <code>{str(exc)[:300]}</code>"
        try:
            await note.edit_text(text)
        except Exception:
            await mic_notify(msg, text)
        return
    tgt = await db.get_app_value(f"mic_chat_{uid}")
    if tgt:
        try:
            await mic_tools.ensure_member(uvc, relay, int(tgt))
        except Exception:
            pass
    text = room_ready_text(src) + (
        "" if tgt else "\n\nAb target set karo: <code>.mic chat -100xxxxxxxx</code>")
    try:
        await note.edit_text(text, reply_markup=room_kb(src))
    except Exception:
        await mic_notify(msg, text, reply_markup=room_kb(src))


async def boost_command(msg, uid: int) -> None:
    """Spare ID ko target me VC-admin banake 200% sabke liye lock karo."""
    from helpers import mic_tools, vc_bridge
    uvc = await get_engine(msg)
    if not uvc:
        return
    relay = await session_manager.get_relay(uid)
    tgt = await db.get_app_value(f"mic_chat_{uid}")
    if relay is None or not tgt:
        await mic_notify(msg, NO_SPARE if relay is None else "Pehle <code>.mic chat -100xxx</code>")
        return
    ok = await mic_tools.ensure_relay_admin(uvc, relay, int(tgt))
    b = vc_bridge.get_bridge(uid)
    if b:
        b.boost_all = ok
    await mic_notify(msg, (
        "🚀 <b>200% boost SABKE LIYE ON</b> — spare ID ab target VC me admin hai, "
        "uska volume har listener ko double milega." if ok else
        "⚠️ Spare ko admin nahi bana paye (main ID / bot ke paas \"Add admins\" right nahi).\n"
        "Group admin se bolo: spare ID ko sirf <b>Manage video chats</b> right de. "
        "Tab tak awaaz Drive se badhao: <code>.mic loud fight</code>"))
