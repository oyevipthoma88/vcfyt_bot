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
    "🎤 <b>Live Mic (VC Bridge)</b> — Telegram VC se live aawaz\n\n"
    "<b>Setup (ek baar, sab buttons se):</b>\n"
    "1. 🔐 Main ID login + 👥 Spare ID add karo\n"
    "2. <b>⚡ Auto bana do</b> — spare ID private group + VC khud banati hai, link bhejti hai\n"
    "3. <b>🎯 Target Group</b> dabao aur target ka ID / link bhejo (VC wahan chalu ho)\n"
    "4. <b>🟢 Mic ON</b> dabao, link se private VC me jao aur bolo\n\n"
    "<b>Controls:</b>\n"
    "• 🚀 <b>Volume</b> 0–30 — density/tezi (16+ FIGHT zone)\n"
    "• 💥 <b>Phatna</b> 0–10 — awaaz phaadna (overdrive), sabse cut-through\n"
    "• 📢 Presence • 🎵 Bass • 🌊 Echo 0–5\n"
    "• 🔇 <b>Hiss Cut</b> — shabdon ke beech ki khar-khar band\n"
    "• Presets: 🛡 Safe • 🔊 Loud • 💥 MAX • ⚔️ FIGHT • ☢️ ULTRA\n\n"
    "<i>Dheemi se dheemi awaaz bhi automatic 0 dB tak jaati hai.</i>\n"
    "<i>Text commands bhi chalte hain: <code>.mic on/off/leave/status</code>, "
    "<code>.mic src ID</code>, <code>.mic chat ID</code>, <code>.mic loud fight</code>, "
    "<code>.mic loud phatna 5</code>.</i>"
)


def _bar(n: int, top: int = 10) -> str:
    k = max(0, min(10, round(n * 10 / top)))
    return "█" * k + "░" * (10 - k)


def loud_text(c: dict, live: bool, src=None, tgt=None) -> str:
    from helpers.vc_bridge import drive_db
    d = c["drive"]
    k = c.get("crunch", 0)
    zone = ("☢️ ULTRA (sabse tez, phategi)" if d > 20 else
            "🔥 FIGHT zone (bohot tez + thodi phati)" if d > 15 else
            "✅ saaf + tez")
    return (
        "🎤 <b>Live Mic Control Panel</b>\n\n"
        f"Status: {'🟢 LIVE — har badlaav turant lagega' if live else '⚪ OFF — 🟢 Mic ON dabao'}\n"
        f"📥 Private: <code>{src or '— set nahi'}</code>\n"
        f"🎯 Target: <code>{tgt or '— set nahi'}</code>\n\n"
        f"🚀 <b>Volume:</b> <code>{_bar(d, 30)} {d}/30</code> (+{drive_db(d)} dB)\n"
        f"      {zone}\n"
        f"💥 <b>Phatna:</b> <code>{_bar(k)} {k}/10</code>{' (OFF)' if not k else ''}\n"
        f"📢 <b>Presence:</b> <code>{_bar(c['presence'])} {c['presence']}/10</code>\n"
        f"🎵 <b>Bass:</b> <code>{_bar(c['bass'])} {c['bass']}/10</code>\n"
        f"🌊 <b>Echo:</b> <code>{_bar(c.get('echo', 0), 5)} {c.get('echo', 0)}/5</code>\n"
        f"✂️ <b>Clip:</b> {'HARD (max tez)' if c['clip'] == 'hard' else 'SOFT (smooth)'}"
        f"  •  🔇 <b>Hiss Cut:</b> {'ON' if c.get('gate', 1) else 'OFF'}\n\n"
        "Dheemi awaaz bhi auto 0 dB par aati hai. Aur tagdi chahiye to "
        "<b>⚔️ FIGHT</b> / <b>☢️ ULTRA</b> ya <b>💥 Phatna +</b>."
    )


def loud_kb(live: bool = False, page: str = "main") -> K:
    nav = [B("🏠 Home", callback_data="menu:home", style="primary")]
    if page == "tune":
        return K([
            [B("💥 Phatna −", callback_data="brl:crunch:-1", style="primary"),
             B("💥 Phatna +", callback_data="brl:crunch:1", style="danger")],
            [B("📢 Presence −", callback_data="brl:presence:-1", style="primary"),
             B("📢 Presence +", callback_data="brl:presence:1", style="primary")],
            [B("🎵 Bass −", callback_data="brl:bass:-1", style="primary"),
             B("🎵 Bass +", callback_data="brl:bass:1", style="primary")],
            [B("🌊 Echo −", callback_data="brl:echo:-1", style="primary"),
             B("🌊 Echo +", callback_data="brl:echo:1", style="primary")],
            [B("✂️ Clip", callback_data="brl:clip:x", style="primary"),
             B("🔇 Hiss Cut", callback_data="brl:gate:x", style="primary"),
             B("♻️ Reset", callback_data="brl:reset:x", style="primary")],
            [B("⬅️ Back", callback_data="brc:loud", style="primary")] + nav,
        ])
    if page == "setup":
        return K([
            [B("📥 Private Group badlo", callback_data="brs:src", style="primary")],
            [B("🎯 Target Group badlo", callback_data="brs:tgt", style="primary")],
            [B("👥 Spare ID", callback_data="mic:acct", style="primary"),
             B("💬 Spare Chat", callback_data="mic:chat", style="primary")],
            [B("🚪 Leave VC", callback_data="brc:leave", style="danger")],
            [B("⬅️ Back", callback_data="brc:loud", style="primary")] + nav,
        ])
    return K([
        [B("🔴 Mic OFF", callback_data="brc:off", style="danger") if live else
         B("🟢 Mic ON", callback_data="brc:on", style="success"),
         B("📊 Status", callback_data="brc:status", style="primary")],
        [B("🔉 Volume −", callback_data="brl:drive:-2", style="primary"),
         B("🔊 Volume +", callback_data="brl:drive:2", style="success")],
        [B("🛡 Safe", callback_data="brl:p:safe", style="primary"),
         B("🔊 Loud", callback_data="brl:p:loud", style="success"),
         B("💥 MAX", callback_data="brl:p:max", style="danger")],
        [B("⚔️ FIGHT", callback_data="brl:p:fight", style="danger"),
         B("☢️ ULTRA", callback_data="brl:p:ultra", style="danger")],
        [B("🎛 Fine tune", callback_data="brv:tune", style="primary"),
         B("⚙️ Groups / ID", callback_data="brv:setup", style="primary")],
        [B("👥 Spare ID", callback_data="mic:acct", style="primary"),
         B("🚪 Leave VC", callback_data="brc:leave", style="danger")],
        [B("❓ Help", callback_data="brc:help", style="primary"),
         B("🔄 Refresh", callback_data="brc:loud", style="primary")] + nav,
    ])


@Client.on_callback_query(filters.regex(r"^brv:(tune|setup)$"))
async def cb_mic_view(bot: Client, cq):
    from helpers import vc_bridge
    await safe_answer(cq)
    text, _ = await panel(cq.from_user.id)
    live = vc_bridge.get_bridge(cq.from_user.id) is not None
    await edit_screen(cq.message, text, reply_markup=loud_kb(live, cq.data.split(":")[1]))


_STEP_SCREENS = {
    "login": ("Step 1/4 — 🔐 Apna account login karo",
              "Live Mic ke liye pehle main account connect karna zaroori hai.",
              [[B("🔐 Login karo", callback_data="menu:login", style="success")]]),
    "spare": ("Step 2/4 — 👥 Spare ID add karo",
              "Spare ID target VC me aapki aawaz bolegi. Uska session add karo.",
              [[B("👥 Spare ID add karo", callback_data="mic:acct", style="success")]]),
    "src": ("Step 3/4 — 📥 Private group (1 click)",
            "<b>⚡ Auto bana do</b> dabao — spare ID khud private group banayegi, VC start "
            "karegi, aapki main ID ko add karegi aur aapko VC ka link bhejegi.\n"
            "Pehle se group hai? To <b>Apna group do</b> dabao.",
            [[B("⚡ Auto bana do (easy)", callback_data="brs:auto", style="success")],
             [B("📥 Apna group do", callback_data="brs:src", style="primary")]]),
    "tgt": ("Step 4/4 — 🎯 Target group set karo",
            "Jis group ki VC me aawaz bhejni hai, uska ID / @username / link bhejo.\n"
            "Spare ID us group me honi chahiye aur VC chalu ho.",
            [[B("🎯 Target group set karo", callback_data="brs:tgt", style="success")]]),
}


async def _is_logged(uid: int) -> bool:
    from plugins.start import _is_logged_in
    return await _is_logged_in(uid)


async def resolve_group(msg, ref: str, uid: int, kind: str):
    """(chat_id, join_ref). Public @username / t.me link / invite link /
    numeric ID — bot, main ID ya spare ID me se jo bhi resolve kar sake.
    Target ke liye spare ID khud join bhi kar leti hai (public + invite)."""
    cid, jref = await target_chat(msg, ref)
    if cid:
        return cid, jref or (ref if not re.fullmatch(r"-?\d+", ref) else None)
    if re.fullmatch(r"-?\d+", ref):
        return 0, None
    clients = []
    try:
        relay = await session_manager.get_relay(uid)
        if relay and getattr(relay, "client", None):
            clients.append(relay.client)
    except Exception:
        pass
    if kind == "src":
        try:
            uvc = await session_manager.get(uid)
            if uvc and getattr(uvc, "client", None):
                clients.insert(0, uvc.client)
        except Exception:
            pass
    key = _join_key(ref)
    for c in clients:
        for fn in ("join_chat", "get_chat"):
            try:
                chat = await getattr(c, fn)(key)
                if getattr(chat, "id", None):
                    return chat.id, ref
            except Exception as exc:
                if "ALREADY" in str(exc).upper():
                    try:
                        chat = await c.get_chat(key)
                        if getattr(chat, "id", None):
                            return chat.id, ref
                    except Exception:
                        pass
                continue
    return 0, ref


def _join_key(ref: str) -> str:
    ref = (ref or "").strip()
    m = re.search(r"t\.me/(?!\+|joinchat/)([A-Za-z0-9_]{4,})", ref)
    if m:
        return m.group(1)
    return ref[1:] if ref.startswith("@") else ref


async def ensure_in_target(client, tgt: int, ref) -> None:
    """Spare ID target group me ho aur peer cache me ho — warna public /
    naye group pe Mic ON 'PEER_ID_INVALID' / 'not in chat' se fail hota tha."""
    if not client:
        return
    try:
        await client.get_chat(tgt)
        return
    except Exception:
        pass
    if ref:
        try:
            await client.join_chat(_join_key(ref))
        except Exception:
            pass
        try:
            await client.get_chat(_join_key(ref))
        except Exception:
            pass
    try:
        async for _ in client.get_dialogs(limit=200):
            pass
    except Exception:
        pass


async def panel(uid: int):
    """(text, keyboard) — guided setup until ready, then the Live Mic panel."""
    from helpers import vc_bridge
    from helpers.setup_flow import mic_setup_step
    src = await db.get_app_value(f"bridge_src_{uid}")
    tgt = await db.get_app_value(f"mic_chat_{uid}")
    try:
        spare = bool(await session_manager.assistant_string(uid, 1))
    except Exception:
        spare = False
    step = mic_setup_step(await _is_logged(uid), spare, src, tgt)
    if step != "ready":
        title, body, rows = _STEP_SCREENS[step]
        done = ["login", "spare", "src", "tgt"].index(step)
        bar = "🟢" * done + "⚪" * (4 - done)
        text = (f"🎤 <b>Live Mic Setup</b>  {bar}\n\n<b>{title}</b>\n{body}\n\n"
                "<i>Ek step poora hote hi agla khud dikhega.</i>")
        return text, K(rows + [[B("❓ Help", callback_data="brc:help", style="primary"),
                                B("🏠 Home", callback_data="menu:home", style="primary")]])
    c = await vc_bridge.load_loud(uid)
    live = vc_bridge.get_bridge(uid) is not None
    return loud_text(c, live, src, tgt), loud_kb(live)


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


async def open_panel_cb(cq) -> None:
    text, kb = await panel(cq.from_user.id)
    await edit_screen(cq.message, text, reply_markup=kb)


@Client.on_callback_query(filters.regex(r"^brc:"))
async def cb_mic_control(bot: Client, cq):
    action = cq.data.split(":", 1)[1]
    if action not in {"on", "off", "status", "leave", "loud", "help"}:
        return await safe_answer(cq, "?")
    await safe_answer(cq, {"on": "Mic ON ho raha hai…", "off": "Mic band…",
                           "leave": "VC se bahar…", "status": "Status…",
                           "loud": "Refresh", "help": "Help"}[action])
    if action == "loud":
        await open_panel_cb(cq)
        return
    if action == "help":
        await edit_screen(cq.message, HELP,
                          reply_markup=K([[B("⬅ Back", callback_data="brc:loud")]]))
        return
    await run_bridge(_CbMsg(cq), ["mic", action])
    if action in {"off", "leave"}:
        try:
            await open_panel_cb(cq)
        except Exception:
            pass


# uid -> "src" | "tgt": next plain DM message is the group ID / link.
_await_chat: dict = {}


@Client.on_callback_query(filters.regex(r"^brs:"))
async def cb_mic_setup(bot: Client, cq):
    what = cq.data.split(":", 1)[1]
    uid = cq.from_user.id
    if what == "cancel":
        _await_chat.pop(uid, None)
        await safe_answer(cq, "Cancel")
        await open_panel_cb(cq)
        return
    if what == "auto":
        await safe_answer(cq, "Group ban raha hai…")
        uvc = await session_manager.get(uid)
        relay = await session_manager.get_relay(uid)
        if relay is None:
            await edit_screen(cq.message, "❌ Pehle Spare ID add karo.",
                              reply_markup=K([[B("👥 Spare ID", callback_data="mic:acct", style="success")]]))
            return
        await edit_screen(cq.message, "⏳ Spare ID private group bana rahi hai aur VC start kar rahi hai…")
        try:
            cid, inv, vlink, me_in = await auto_private_group(uid, uvc, relay)
        except Exception as exc:
            await edit_screen(cq.message, f"❌ Auto group nahi bana:\n<code>{str(exc)[:300]}</code>\n\n"
                              "Spare ID nayi ho to Telegram thodi der group banane nahi deta — "
                              "<b>📥 Apna group do</b> try karo.",
                              reply_markup=K([[B("🔁 Dobara", callback_data="brs:auto", style="success")],
                                              [B("📥 Apna group do", callback_data="brs:src", style="primary")]]))
            return
        text, kb = await panel(uid)
        await edit_screen(cq.message, auto_group_text(cid, inv, vlink, me_in) + "\n\n" + text,
                          reply_markup=kb, disable_web_page_preview=True)
        return
    if what not in {"src", "tgt"}:
        return await safe_answer(cq, "?")
    _await_chat[uid] = what
    await safe_answer(cq)
    label = "📥 PRIVATE (source) group" if what == "src" else "🎯 TARGET group"
    await edit_screen(cq.message, (
        f"{label} set karna hai.\n\n"
        "Bot DM me abhi group ka <b>chat ID</b> (<code>-100…</code> ya bina -100), "
        "<b>@username</b> ya <b>invite link</b> bhejo.\n\n"
        "<i>Message turant delete ho jayega.</i>"),
        reply_markup=K([[B("❌ Cancel", callback_data="brs:cancel", style="danger")]]))


@Client.on_message(HAS_USER & filters.private & filters.text
                   & ~filters.regex(r"^[/.!]"), group=4)
async def mic_setup_listener(bot: Client, msg: Message):
    uid = msg.from_user.id
    what = _await_chat.get(uid)
    if not what:
        return
    ref = cmd_text(msg).strip().split()[0] if cmd_text(msg).strip() else ""
    try:
        await msg.delete()
    except Exception:
        pass
    cid, jref = await resolve_group(msg, ref, uid, what) if ref else (None, None)
    if not cid:
        await bot.send_message(
            msg.chat.id, "❌ Group nahi mila. Chat ID / @username / invite link dobara bhejo.",
            reply_markup=K([[B("❌ Cancel", callback_data="brs:cancel", style="danger")]]))
        return
    other = await db.get_app_value(f"mic_chat_{uid}" if what == "src" else f"bridge_src_{uid}")
    if other and str(other) == str(cid):
        await bot.send_message(
            msg.chat.id, "❌ Private aur target group alag hone chahiye. Dusra group bhejo.",
            reply_markup=K([[B("❌ Cancel", callback_data="brs:cancel", style="danger")]]))
        return
    _await_chat.pop(uid, None)
    await db.set_app_value(f"bridge_src_{uid}" if what == "src" else f"mic_chat_{uid}", str(cid))
    if what == "tgt":
        await db.set_app_value(f"mic_chat_ref_{uid}", jref or "")
    text, kb = await panel(uid)
    await bot.send_message(msg.chat.id, f"✅ {'Private' if what == 'src' else 'Target'} group set: "
                           f"<code>{cid}</code>\n\n" + text, reply_markup=kb)


async def _loud_change(uid: int, key: str, val: str) -> dict:
    from helpers import vc_bridge
    c = await vc_bridge.load_loud(uid)
    if key == "p" and val in vc_bridge.LOUD_PRESETS:
        c = vc_bridge.apply_preset(c, val)
    elif key == "clip":
        c["clip"] = val if val in ("hard", "soft") else ("soft" if c["clip"] == "hard" else "hard")
    elif key == "gate":
        c["gate"] = 0 if c.get("gate", 1) else 1
    elif key in vc_bridge.LOUD_LIMITS:
        try:
            c[key] = int(c[key]) + int(val)
        except ValueError:
            pass
    elif key == "reset":
        c = dict(vc_bridge.LOUD_DEFAULT)
    return await vc_bridge.save_loud(uid, c)


_TEXT_KEYS = {"drive": "drive", "volume": "drive", "vol": "drive",
              "bass": "bass", "presence": "presence",
              "phatna": "crunch", "crunch": "crunch", "fat": "crunch",
              "echo": "echo"}


async def loud_command(msg: Message, args):
    from helpers import vc_bridge
    uid = msg.from_user.id
    c = await vc_bridge.load_loud(uid)
    a = [x.lower() for x in args]
    try:
        if a:
            if a[0] in vc_bridge.LOUD_PRESETS:
                c = vc_bridge.apply_preset(c, a[0])
            elif a[0].isdigit():
                c["drive"] = int(a[0])
            elif a[0] in _TEXT_KEYS and len(a) > 1:
                c[_TEXT_KEYS[a[0]]] = int(a[1])
            elif a[0] == "clip" and len(a) > 1 and a[1] in ("hard", "soft"):
                c["clip"] = a[1]
            elif a[0] in ("gate", "hiss") and len(a) > 1:
                c["gate"] = 1 if a[1] in ("on", "1") else 0
            c = await vc_bridge.save_loud(uid, c)
    except ValueError:
        pass
    text, kb = await panel(uid)
    await mic_notify(msg, text, reply_markup=kb)


@Client.on_callback_query(filters.regex(r"^brl:"))
async def cb_loud(bot: Client, cq):
    _, key, val = (cq.data.split(":") + ["", ""])[:3]
    uid = cq.from_user.id
    c = await _loud_change(uid, key, val)
    await open_panel_cb(cq)
    await safe_answer(cq, f"Volume {c['drive']} · Phatna {c['crunch']} · Bass {c['bass']} · "
                          f"Presence {c['presence']} · Echo {c['echo']} · {c['clip'].upper()}")


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
        await mic_notify(msg, HELP, reply_markup=K([[B("🎛 Control Panel", callback_data="brc:loud", style="primary")]]))
        return

    if action in {"chat", "setchat", "target"}:
        if len(parts) < 3:
            await mic_notify(msg, "Target group do: <code>.mic chat -100xxxxxxxx</code>")
            return
        cid, jref = await resolve_group(msg, parts[2], uid, "tgt")
        if not cid:
            await mic_notify(msg, "❌ Target group nahi mila. Chat ID / @username / link do.")
            return
        await db.set_app_value(f"mic_chat_{uid}", str(cid))
        await db.set_app_value(f"mic_chat_ref_{uid}", jref or "")
        await mic_notify(msg, f"✅ Target group set: <code>{cid}</code>")
        return

    if action in {"loud", "power", "boost", "panel", "control", "ctrl"}:
        await loud_command(msg, parts[2:])
        return

    if action in {"src", "source"}:
        if len(parts) < 3:
            await mic_notify(msg, "Private group do: <code>.mic src -100xxxxxxxx</code>")
            return
        cid, _ = await resolve_group(msg, parts[2], uid, "src")
        if not cid:
            await mic_notify(msg, "❌ Private group nahi mila. Chat ID (-100 ke saath ya bina) / @username / link do.")
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
                                  f"Target: <code>{tgt or '—'}</code>",
                             reply_markup=K([[B("🎛 Control Panel", callback_data="brc:loud", style="primary")]]))
            return
        s = b.status()
        await mic_notify(msg, (
            f"🎤 Live Mic (Bridge) {'🟢 LIVE' if s['live'] else '🟡 starting'}\n"
            f"Source: <code>{s['source']}</code>\nTarget: <code>{s['target']}</code>\n"
            f"Preset: <b>{s['preset']}</b>\nAudio: {s['received_kb']} KB · {s['uptime_s']}s\n"
            f"🔥 Volume {s['loud']['drive']}/30 · Phatna {s['loud']['crunch']}/10 · "
            f"bass {s['loud']['bass']} · presence {s['loud']['presence']} · "
            f"echo {s['loud']['echo']} · {s['loud']['clip']}"),
            reply_markup=K([[B("🎛 Control Panel", callback_data="brc:loud", style="primary")]]))
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
        await mic_notify(msg, "❌ Spare ID set nahi hai. Neeche <b>👥 Spare ID</b> dabake login karo.",
                         reply_markup=K([[B("👥 Spare ID", callback_data="mic:acct", style="primary")]]))
        return
    preset = vc_bridge.DEFAULT_PRESET
    tgt_arg = None
    for p in parts[2:]:
        if p.lower() in vc_bridge.PRESETS:
            preset = p.lower()
        elif is_chat_ref(p):
            tgt_arg = p
    src = await db.get_app_value(f"bridge_src_{uid}")
    auto_note = None
    if not src:
        # Private group nahi hai -> spare ID khud bana degi (zero setup).
        try:
            cid_, inv_, vlink_, me_in_ = await auto_private_group(uid, uvc, relay)
            src = str(cid_)
            auto_note = auto_group_text(cid_, inv_, vlink_, me_in_)
            await mic_notify(msg, auto_note, disable_web_page_preview=True)
        except Exception as exc:
            await mic_notify(msg, f"❌ Private group auto nahi bana: <code>{str(exc)[:200]}</code>\n"
                                  "<b>📥 Private Group</b> button se apna group do.",
                             reply_markup=K([[B("⚡ Auto bana do", callback_data="brs:auto", style="success")],
                                             [B("📥 Private Group", callback_data="brs:src", style="primary")]]))
            return
    elif str(src) == str(await db.get_app_value(f"bridge_auto_src_{uid}") or ""):
        # Auto group ki VC band ho gayi ho to dobara chalu kar do.
        await _start_vc(relay.client, int(src))
    tgt_arg = tgt_arg or await db.get_app_value(f"mic_chat_{uid}")
    if not tgt_arg:
        await mic_notify(msg, "Target group set karo: <b>🎯 Target Group</b> button ya <code>.mic chat -100xxxxxxxx</code>",
                         reply_markup=K([[B("🎯 Target Group", callback_data="brs:tgt", style="primary")]]))
        return
    tgt_ref = await db.get_app_value(f"mic_chat_ref_{uid}") or None
    if tgt_arg and not re.fullmatch(r"-?\d+", str(tgt_arg)):
        tgt_ref = str(tgt_arg)
    tgt, jref = await resolve_group(msg, str(tgt_arg), uid, "tgt")
    if not tgt and tgt_ref:
        tgt, jref = await resolve_group(msg, tgt_ref, uid, "tgt")
    tgt_ref = jref or tgt_ref
    src_id = int(src)
    if not tgt:
        await mic_notify(msg, "❌ Target group nahi mila.")
        return
    if tgt == src_id:
        await mic_notify(msg, "❌ Private aur target group alag hone chahiye.")
        return
    await db.set_app_value(f"mic_chat_{uid}", str(tgt))
    if tgt_ref:
        await db.set_app_value(f"mic_chat_ref_{uid}", tgt_ref)
    await ensure_in_target(getattr(relay, "client", None), tgt, tgt_ref)
    try:
        await uvc.client.get_chat(src_id)
    except Exception:
        try:
            async for _ in uvc.client.get_dialogs(limit=200):
                pass
        except Exception:
            pass
    await reset_mic_state(uvc, uid)

    note = await mic_notify(msg, (
        f"⏳ Bridge start ho raha hai (preset <b>{preset}</b>)…\n"
        "Ab main ID se PRIVATE VC me mic ON karke bolo (30 sec ke andar)."))
    try:
        settings = await db.get_settings(uid)
        extras = []
        from helpers.access_control import can_multi_spare
        if await can_multi_spare(uid):
            extras = await session_manager.extra_relays(
                uid, exclude_ids=(getattr(relay, "account_id", 0),
                                  getattr(uvc, "account_id", 0)))
        for r in extras:
            await ensure_in_target(getattr(r, "client", None), tgt, tgt_ref)
        await vc_bridge.start_bridge(uid, uvc, relay, src_id, tgt, settings, preset,
                                     extra_relays=extras)
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
    text, kb = await panel(uid)
    try:
        if note:
            await note.edit_text(text, reply_markup=kb)
            return
    except Exception:
        pass
    await mic_notify(msg, text, reply_markup=kb)


# ---------------------------------------------------------------------------
# AUTO PRIVATE GROUP — user ko khud group banana / ID bhejna nahi padega.
# Spare ID ek private supergroup banati hai, VC start karti hai, main ID ko
# auto-join karati hai, aur bot user ko VC ka direct link deta hai.
# ---------------------------------------------------------------------------
async def _vc_running(client, chat_id: int) -> bool:
    from pyrogram.raw.functions.channels import GetFullChannel
    try:
        full = await client.invoke(GetFullChannel(channel=await client.resolve_peer(chat_id)))
        return getattr(full.full_chat, "call", None) is not None
    except Exception:
        return False


async def _start_vc(client, chat_id: int) -> bool:
    import asyncio
    import random
    from pyrogram.raw.functions.phone import CreateGroupCall
    if await _vc_running(client, chat_id):
        return True
    try:
        await client.invoke(CreateGroupCall(peer=await client.resolve_peer(chat_id),
                                            random_id=random.randint(1, 2**31 - 1)))
        await asyncio.sleep(1.5)
        return True
    except Exception:
        return await _vc_running(client, chat_id)


async def _vc_link(client, chat_id: int, fallback: str) -> str:
    """Direct 'VC kholo' link (tap karte hi voice chat open)."""
    from pyrogram.raw.functions.channels import GetFullChannel
    from pyrogram.raw.functions.phone import ExportGroupCallInvite
    try:
        full = await client.invoke(GetFullChannel(channel=await client.resolve_peer(chat_id)))
        call = getattr(full.full_chat, "call", None)
        if call:
            res = await client.invoke(ExportGroupCallInvite(call=call, can_self_unmute=True))
            if getattr(res, "link", None):
                return res.link
    except Exception:
        pass
    return fallback


async def auto_private_group(uid: int, uvc, relay):
    """(chat_id, invite_link, vc_link) — reuse ya naya private group + VC."""
    rc = getattr(relay, "client", None)
    if rc is None:
        raise RuntimeError("Spare ID connected nahi hai.")
    cid = None
    saved = await db.get_app_value(f"bridge_auto_src_{uid}")
    if saved:
        try:
            await rc.get_chat(int(saved))
            cid = int(saved)
        except Exception:
            cid = None
    if cid is None:
        chat = await rc.create_supergroup("🎤 My Live Mic", "Auto-created by bot — sirf aap aur spare ID")
        cid = chat.id
        await db.set_app_value(f"bridge_auto_src_{uid}", str(cid))
    invite = (await rc.create_chat_invite_link(cid)).invite_link
    # Main ID ko khud join karao — user ko kuch nahi karna.
    me_in = False
    if uvc is not None and getattr(uvc, "client", None) is not None:
        try:
            await uvc.client.get_chat_member(cid, "me")
            me_in = True
        except Exception:
            try:
                await uvc.client.join_chat(invite)
                me_in = True
            except Exception:
                me_in = False
    if not await _start_vc(rc, cid):
        raise RuntimeError("Private group me VC start nahi hui.")
    await db.set_app_value(f"bridge_src_{uid}", str(cid))
    return cid, invite, await _vc_link(rc, cid, invite), me_in


def auto_group_text(cid: int, invite: str, vc_link: str, me_in: bool) -> str:
    return ("✅ <b>Private group + VC ready!</b>\n\n"
            f"Group: <code>{cid}</code>\n"
            + ("Aapki main ID group me auto-add ho gayi.\n" if me_in else
               f"Group join karo: {invite}\n") +
            f"\n👉 <b>Bas ye link dabao aur VC me mic ON karo:</b>\n{vc_link}\n\n"
            "<i>Isi VC me jo bologe, spare ID target VC me loud bolegi.</i>")
